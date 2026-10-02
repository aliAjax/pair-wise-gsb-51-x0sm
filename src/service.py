"""业务用例编排、权限检查、双人复核与对账检查点。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, integer, number, optional_text, text
from .recon import optional_iso_date, performance_summary, today_iso
from .repository import Repository
from .rules import PLAN_CHANGE_ROLES, REVIEWER_ROLE, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _require_role(self, actor: Actor, roles: set) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("角色无权执行该操作")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        if action == "activate":
            # 方案生效同时生成唯一有效方案与每期应收，避免“回执先到、计划缺失”的乱序
            terms = self.rules.plan_terms(new_payload)
            first_due_date = optional_iso_date(data or {}, "first_due_date")
            if first_due_date is None:
                first_due_date = today_iso()
            result, _plan = self.repository.activate_with_plan(
                record_id=record_id,
                expected_version=int(expected_version),
                payload=new_payload,
                actor_id=actor.user_id,
                terms=terms,
                first_due_date=first_due_date,
                details=details,
            )
            return result
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
        )

    # ---- 纾困方案变更：经办提交、另一人复核 ------------------------------

    def submit_plan_change(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, PLAN_CHANGE_ROLES)
        self.repository.get(record_id)
        change_input = self.rules.validate_plan_change(data or {})
        return self.repository.create_plan_change(record_id, change_input, actor.user_id)

    def list_plan_changes(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_plan_changes(record_id)

    def review_plan_change(self, actor: Actor, change_id: int, approved: bool, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {REVIEWER_ROLE})
        change = self.repository.get_plan_change(change_id)
        # 双人控制：复核人不能是提交变更的经办人本人
        if actor.user_id == str(change["submitted_by"]):
            raise PermissionDenied("方案变更必须由提交人之外的另一名复核人确认")
        review_note = optional_text(data or {}, "review_note", "确认通过")
        if approved:
            confirmed, new_plan = self.repository.confirm_plan_change(change_id, actor.user_id, review_note)
            return {"change": confirmed, "new_plan": new_plan}
        review_note = optional_text(data or {}, "review_note", "复核驳回")
        return {"change": self.repository.reject_plan_change(change_id, actor.user_id, review_note)}
    # ---- 银行回执与可恢复对账 -------------------------------------------

    def intake_receipt(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {"servicer"})
        payload = data or {}
        receipt = {
            "bank_serial": text(payload, "bank_serial"),
            "amount": number(payload, "amount", 0.0001),
            "period_no": self._optional_period(payload),
            "received_at": optional_iso_date(payload, "received_at") or today_iso(),
        }
        saved = self.repository.insert_receipt(record_id, receipt, actor.user_id)
        # 回执进箱即推动检查点处理；处理本身是系统行为，可独立重试
        totals = self.repository.process_receipt_queue()
        return {"receipt": saved, "reconciliation": totals}

    @staticmethod
    def _optional_period(payload: Dict[str, Any]) -> Optional[int]:
        if payload.get("period_no") is None:
            return None
        return integer(payload, "period_no", 1)

    def reconcile(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {"servicer"})
        return self.repository.process_receipt_queue()

    def list_discrepancies(self, actor: Actor, status: Optional[str] = None, record_id: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_discrepancies(status=status, record_id=record_id)

    def recheck_discrepancy(self, actor: Actor, discrepancy_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {"servicer"})
        return self.repository.recheck_discrepancy(discrepancy_id, actor.user_id)

    def ignore_discrepancy(self, actor: Actor, discrepancy_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {"servicer"})
        note = optional_text(data or {}, "note", "人工确认无需核销")
        return self.repository.ignore_discrepancy(discrepancy_id, actor.user_id, note)

    def recon_view(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        active_plan = self.repository.get_active_plan(record_id)
        receivables = self.repository.list_receivables(record_id)
        return {
            "record_id": record_id,
            "state": record["state"],
            "active_plan": active_plan,
            "plans": self.repository.list_plans(record_id),
            "receivables": receivables,
            "receipts": self.repository.list_receipts(record_id),
            "discrepancies": self.repository.list_discrepancies(record_id=record_id),
            "performance": performance_summary(active_plan, receivables),
        }

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
