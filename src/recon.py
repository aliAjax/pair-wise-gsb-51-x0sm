"""对账纯领域逻辑：账期生成、银行回执分类、履约状态推导。

本模块不接触数据库，所有函数都是纯函数，便于在单事务内重放，
从而支撑“写入失败后从检查点重试且不重复审计”。
"""
import calendar
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Set

from .domain import ValidationError

# 金额比对容差（分以下四舍五入）
AMOUNT_TOLERANCE = 0.005

# 分类结果
SETTLE = "settle"
DIFFERENCE = "difference"

# 差异类型
DUP_SERIAL = "duplicate"            # 同一流水重复核销
OUT_OF_ORDER = "out_of_order"       # 乱序：前面仍有未核销应收
AMOUNT_MISMATCH = "amount_mismatch"  # 金额不符
NO_PAYABLE = "no_payable"           # 找不到可核销应收（期号无效/已失效）
NO_ACTIVE_PLAN = "no_active_plan"   # 每期应收没有对应有效方案

PAYABLE = "payable"
SETTLED = "settled"
VOID = "void"


def today_iso() -> str:
    return date.today().isoformat()


def parse_iso_date(value: Any, key: str = "date") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key)
    text_value = value.strip()
    try:
        date.fromisoformat(text_value)
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key) from exc
    return text_value


def optional_iso_date(data: Dict[str, Any], key: str) -> Optional[str]:
    value = data.get(key)
    if value is None:
        return None
    return parse_iso_date(value, key)


def add_months(iso_day: str, months: int) -> str:
    year, month, day = (int(part) for part in iso_day.split("-"))
    index = year * 12 + (month - 1) + months
    target_year, target_month = index // 12, index % 12 + 1
    last_day = calendar.monthrange(target_year, target_month)[1]
    return "%04d-%02d-%02d" % (target_year, target_month, min(day, last_day))


def monthly_schedule(first_due_date: str, periods: int, installment_amount: float) -> List[Dict[str, Any]]:
    """按等额月供生成应收账期，每期只属于一个方案版本。"""
    amount = round(float(installment_amount), 2)
    return [
        {
            "period_no": index + 1,
            "due_date": add_months(first_due_date, index),
            "amount_due": amount,
        }
        for index in range(int(periods))
    ]


@dataclass(frozen=True)
class Decision:
    kind: str                          # SETTLE / DIFFERENCE
    reason: str
    receivable_id: Optional[int] = None
    difference_type: Optional[str] = None
    expected_amount: Optional[float] = None


def _difference(difference_type: str, reason: str, expected_amount: Optional[float] = None) -> Decision:
    return Decision(kind=DIFFERENCE, reason=reason, difference_type=difference_type, expected_amount=expected_amount)


def classify_receipt(
    receipt: Dict[str, Any],
    active_plan: Optional[Dict[str, Any]],
    receivables: List[Dict[str, Any]],
    used_serials: Set[str],
) -> Decision:
    """对一笔银行回执给出核销判定。

    - 同一流水只能核销一笔应收（used_serials 覆盖已核销流水和同批次先前流水）；
    - 重复、乱序、金额不符、无可核销应收一律落入待核差异，不做任何核销。
    """
    serial = str(receipt["bank_serial"])
    amount = round(float(receipt["amount"]), 2)
    period_no = receipt.get("period_no")

    if serial in used_serials:
        return _difference(DUP_SERIAL, "流水号%s已核销或已登记，同一流水只能核销一笔应收" % serial)

    if active_plan is None or active_plan.get("status") != "active":
        return _difference(NO_ACTIVE_PLAN, "当前没有有效纾困方案，应收暂不能核销")

    if period_no is not None:
        target = next((item for item in receivables if int(item["period_no"]) == int(period_no)), None)
        if target is None:
            return _difference(NO_PAYABLE, "有效方案中不存在第%s期应收" % period_no)
        if target["status"] == SETTLED:
            return _difference(DUP_SERIAL, "第%s期应收已被核销" % period_no)
        if target["status"] == VOID:
            return _difference(NO_PAYABLE, "第%s期应收随旧方案失效，应按新方案重算" % period_no)
    else:
        payable = [item for item in receivables if item["status"] == PAYABLE]
        if not payable:
            return _difference(NO_PAYABLE, "有效方案已无待核销应收")
        target = min(payable, key=lambda item: int(item["period_no"]))

    earlier_unpaid = [
        item for item in receivables
        if item["status"] == PAYABLE and int(item["period_no"]) < int(target["period_no"])
    ]
    if earlier_unpaid:
        gap = ",".join(str(int(item["period_no"])) for item in earlier_unpaid)
        return _difference(
            OUT_OF_ORDER,
            "回执先于第%s期到账，仍有第%s期应收未核销，按乱序挂账待核" % (target["period_no"], gap),
            expected_amount=float(target["amount_due"]),
        )

    expected = round(float(target["amount_due"]), 2)
    if abs(amount - expected) > AMOUNT_TOLERANCE:
        return _difference(
            AMOUNT_MISMATCH,
            "回执金额%s与第%s期应收%s不符" % (amount, target["period_no"], expected),
            expected_amount=expected,
        )

    return Decision(kind=SETTLE, reason="核销第%s期应收" % target["period_no"], receivable_id=int(target["id"]))


def performance_summary(
    active_plan: Optional[Dict[str, Any]],
    receivables: List[Dict[str, Any]],
    today: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """履约状态由应收明细推导，银行回执本身不直接改履约状态。"""
    if active_plan is None:
        return None
    today = today or today_iso()
    plan_receivables = [item for item in receivables if int(item["plan_id"]) == int(active_plan["id"])]
    settled = [item for item in plan_receivables if item["status"] == SETTLED]
    payable = [item for item in plan_receivables if item["status"] == PAYABLE]
    overdue = [item for item in payable if str(item["due_date"]) < today]
    next_due = min((str(item["due_date"]) for item in payable), default=None)
    if plan_receivables and len(settled) == len(plan_receivables):
        status = "completed"
    elif overdue:
        status = "overdue"
    else:
        status = "current"
    return {
        "status": status,
        "plan_id": int(active_plan["id"]),
        "total_periods": len(plan_receivables),
        "settled_periods": len(settled),
        "overdue_periods": [int(item["period_no"]) for item in overdue],
        "next_due_date": next_due,
        "paid_total": round(sum(float(item.get("paid_amount") or 0) for item in settled), 2),
        "outstanding_total": round(sum(float(item["amount_due"]) for item in payable), 2),
    }
