"""业务用例编排、权限检查、方案/计划/回执对账与审计。"""
from datetime import date
from typing import Any, Dict, List, Optional

from . import reconcile
from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, integer, number, optional_text, text
from .repository import Repository
from .rules import DomainRules


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
            # 方案生效时首期还款计划同事务生成，保证每期应收只挂在唯一有效方案下。
            payment = float(new_payload["approved_payment"])
            months = int(new_payload["approved_months"])
            terms = reconcile.build_terms(1, months, payment, date.today())
            return self.repository.activate_with_plan(
                record_id, int(expected_version), new_payload, actor.user_id, action, details, terms
            )
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
        )

    # ---- 方案变更：经办提交，另一名复核人确认 ---------------------------

    def submit_plan_change(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_submit_plan_change(actor.role):
            raise PermissionDenied("角色无权提交方案变更")
        self.repository.get(record_id)
        change = self.rules.validate_plan_change(data or {})
        return self.repository.submit_plan_change(
            record_id, change["payment_amount"], change["months"], change["reason"], actor.user_id
        )

    def review_plan_change(self, actor: Actor, change_id: int, decision: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_plan_change(actor.role):
            raise PermissionDenied("角色无权复核方案变更")
        decision = text({"decision": decision}, "decision")
        if decision not in {"confirm", "reject"}:
            raise ValidationError("decision只能是confirm/reject")
        change = self.repository.get_plan_change(change_id)
        # 双人控制：经办本人不能复核自己的变更，管理员也不例外。
        if change["submitted_by"] == actor.user_id:
            raise PermissionDenied("提交人与复核人不能是同一人")
        review_note = optional_text(data or {}, "review_note", "")
        if decision == "reject":
            return self.repository.reject_plan_change(change_id, actor.user_id, review_note)
        # 确认后才重算：旧计划未核销应收失效，新计划从已核销期之后续期。
        settled = [item for item in self.repository.list_installments(change["record_id"])
                   if item["status"] == "settled"]
        terms = reconcile.build_terms(
            len(settled) + 1, int(change["months"]), float(change["payment_amount"]), date.today()
        )
        return self.repository.confirm_plan_change(change_id, actor.user_id, review_note, terms)

    def installments(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_installments(record_id)

    def plan_changes(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_plan_changes(record_id)

    # ---- 扣款回执入库与对账 --------------------------------------------

    def _process_receipt(self, connection, receipt: Dict[str, Any], actor_id: str,
                         difference: Dict[str, Any] = None) -> Dict[str, Any]:
        """对一笔已锁定的回执执行判定与核销/挂差异，全部操作使用同一事务。"""
        active_plan = self.repository.get_active_plan(receipt["record_id"], connection)
        installments = self.repository.list_installments(receipt["record_id"], connection)
        kind, target, detail = reconcile.classify(receipt, active_plan, installments)
        if kind == reconcile.MATCH:
            self.repository.settle_match(connection, receipt, target, actor_id, difference=difference)
            return {"outcome": "matched", "period_no": target["period_no"]}
        if difference is not None:
            # 差异重试仍不符：留在待核队列，不新增差异、不重复审计。
            self.repository.requeue_difference(connection, difference, target, detail)
            return {"outcome": "requeued", "reason": target}
        diff_id = self.repository.open_difference_for_receipt(connection, receipt, target, detail, actor_id)
        return {"outcome": "difference", "difference_id": diff_id, "reason": target}

    def ingest_receipt(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_reconcile(actor.role):
            raise PermissionDenied("角色无权登记扣款回执")
        self.repository.get(record_id)
        payload = data or {}
        bank_serial = text(payload, "bank_serial")
        amount = number(payload, "amount", 0)
        period_no = payload.get("period_no")
        if period_no is not None:
            period_no = integer(payload, "period_no", 1)

        with self.repository.immediate() as connection:
            existing = self.repository.get_receipt_by_serial(bank_serial, connection)
            if existing is not None:
                # 同一流水只能核销一笔应收：重复流水直接进待核差异，绝不二次核销。
                difference = self.repository.register_duplicate(existing, actor.user_id, connection)
                return {"outcome": "duplicate", "reason": reconcile.REASON_DUPLICATE,
                        "difference_id": difference["id"]}
            receipt = self.repository.insert_receipt(record_id, bank_serial, amount, period_no,
                                                     actor.user_id, connection)
            outcome = self._process_receipt(connection, receipt, actor.user_id)
        result = {"receipt_id": receipt["id"], "bank_serial": bank_serial}
        result.update(outcome)
        return result

    def run_reconciliation(self, actor: Actor, record_id: Optional[int] = None,
                           fail_after: Optional[int] = None) -> Dict[str, Any]:
        """从检查点恢复的对账：先重试待核差异，再处理水位之后的新回执。

        每笔回执独立事务提交，检查点随后推进；中途崩溃后重跑：已提交的回执
        靠状态与审计幂等键跳过，不产生重复核销或重复审计。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_reconcile(actor.role):
            raise PermissionDenied("角色无权执行对账")

        matched = differences = retried = 0

        # 第一遍：待核差异重判（乱序补齐前序、金额改账后可自动转正）。
        for difference in self.repository.open_differences(record_id):
            if difference["reason"] not in reconcile.RETRYABLE_REASONS:
                continue
            receipt = self.repository.get_receipt(difference["receipt_id"])
            if receipt is None or receipt["status"] != "difference":
                continue
            with self.repository.immediate() as connection:
                outcome = self._process_receipt(connection, receipt, actor.user_id, difference=difference)
            if outcome["outcome"] == "matched":
                matched += 1
            retried += 1

        # 第二遍：检查点之后的新回执。
        scope = "global" if record_id is None else "record:%s" % record_id
        watermark = self.repository.get_checkpoint(scope)
        processed = 0
        while True:
            receipt = self.repository.next_received_receipt(watermark, record_id)
            if receipt is None:
                break
            with self.repository.immediate() as connection:
                outcome = self._process_receipt(connection, receipt, actor.user_id)
            # 事务已提交再推进检查点；两者之间崩溃也安全——重跑按回执状态跳过。
            processed += 1
            self.repository.advance_checkpoint(scope, receipt["id"])
            watermark = max(watermark, receipt["id"])
            if outcome["outcome"] == "matched":
                matched += 1
            else:
                differences += 1
            if fail_after is not None and processed >= int(fail_after):
                raise RuntimeError("对账在检查点前提注入失败")
            if processed >= 10000:
                break

        return {"retried": retried, "processed": processed, "matched": matched,
                "differences": differences, "checkpoint": watermark}

    def list_receipts(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_receipts(record_id)

    def list_differences(self, actor: Actor, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_differences(status=status, limit=limit)

    def resolve_difference(self, actor: Actor, difference_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_plan_change(actor.role):
            raise PermissionDenied("角色无权核销待核差异")
        payload = data or {}
        resolution = text(payload, "resolution")
        if resolution not in {"write_off", "ignore"}:
            raise ValidationError("resolution只能是write_off/ignore")
        return self.repository.resolve_difference(difference_id, resolution, actor.user_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
