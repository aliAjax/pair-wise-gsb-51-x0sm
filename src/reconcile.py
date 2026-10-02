"""扣款回执对账的纯领域逻辑：期次生成、匹配判定与差异分类。

判定结果只有两种：
- ("match", installment, None)：足额、按序，核销该期应收；
- ("difference", reason, detail)：进入待核差异，不改动应收、不改履约状态。

差异原因：
- duplicate：流水号重复（入库阶段拦截，同一流水最多核销一笔应收）；
- out_of_order：乱序，包括提前还未到期、已核销期、已随旧方案失效的期；
- amount_mismatch：回执金额与当期应收不符；
- no_active_plan：尚无生效方案或生效方案已无未核销应收。

后三类在后续对账中可自动重试（差额仍在待核队列，补齐前序款项后可转正）。
"""
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

MATCH = "match"
DIFFERENCE = "difference"

REASON_DUPLICATE = "duplicate"
REASON_OUT_OF_ORDER = "out_of_order"
REASON_AMOUNT_MISMATCH = "amount_mismatch"
REASON_NO_ACTIVE_PLAN = "no_active_plan"
RETRYABLE_REASONS = (REASON_OUT_OF_ORDER, REASON_AMOUNT_MISMATCH, REASON_NO_ACTIVE_PLAN)

AMOUNT_TOLERANCE = 0.01


def add_months(day: date, months: int) -> date:
    """按月顺延，月底（如31日）自动钳制到目标月最后一天。"""
    total = (day.year * 12 + day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last_day))


def build_terms(start_period: int, months: int, amount: float, base: date) -> List[Dict[str, Any]]:
    """生成连续期次：期号在案件维度延续，首期到期日为基准日次月。"""
    terms = []
    for offset in range(months):
        period = start_period + offset
        terms.append({
            "period_no": period,
            "amount_due": round(float(amount), 2),
            "due_date": add_months(base, offset + 1).isoformat(),
        })
    return terms


def _difference(reason: str, **detail: Any) -> Tuple[str, str, Dict[str, Any]]:
    return DIFFERENCE, reason, detail


def classify(receipt: Dict[str, Any], active_plan: Optional[Dict[str, Any]],
             installments: List[Dict[str, Any]]) -> Tuple[str, Any, Optional[Dict[str, Any]]]:
    """对单笔回执做匹配判定，不产生任何副作用。

     installments 为该案件全部期次（含 settled/void），用于识别乱序与失效期。
    """
    if active_plan is None:
        return _difference(REASON_NO_ACTIVE_PLAN, message="没有生效中的纾困方案")
    active = sorted(
        (item for item in installments if item["status"] == "active" and item["plan_id"] == active_plan["id"]),
        key=lambda item: item["period_no"],
    )
    if not active:
        return _difference(REASON_NO_ACTIVE_PLAN, message="生效方案已无未核销应收")
    expected = active[0]

    attempted_period = receipt.get("period_no")
    target = expected
    if attempted_period is not None:
        # 同一期号可能存在旧方案的 void 行，优先取当前有效行。
        candidates = sorted(
            (item for item in installments if item["period_no"] == attempted_period),
            key=lambda item: 0 if item["status"] != "void" else 1,
        )
        if not candidates:
            return _difference(
                REASON_OUT_OF_ORDER,
                attempted_period=attempted_period,
                expected_period=expected["period_no"],
                message="回执指向不存在的期次",
            )
        indicated = candidates[0]
        if indicated["status"] == "void":
            return _difference(
                REASON_OUT_OF_ORDER,
                attempted_period=attempted_period,
                expected_period=expected["period_no"],
                message="对应应收已随旧方案失效",
            )
        if indicated["status"] == "settled":
            return _difference(
                REASON_OUT_OF_ORDER,
                attempted_period=attempted_period,
                expected_period=expected["period_no"],
                message="该期应收已核销",
            )
        if indicated["id"] != expected["id"]:
            return _difference(
                REASON_OUT_OF_ORDER,
                attempted_period=attempted_period,
                expected_period=expected["period_no"],
                message="前序期次尚未核销",
            )
        target = indicated

    actual = round(float(receipt["amount"]), 2)
    if abs(actual - float(target["amount_due"])) > AMOUNT_TOLERANCE:
        return _difference(
            REASON_AMOUNT_MISMATCH,
            period_no=target["period_no"],
            expected_amount=float(target["amount_due"]),
            actual_amount=actual,
        )
    return MATCH, target, None
