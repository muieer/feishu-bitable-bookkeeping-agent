"""Window-scoped identities and confirmed facts; no pending conversation state."""

import time
from dataclasses import dataclass, field
from typing import Literal

from .formatting import bill_label
from .models import Expense, IdentifiedItem

Status = Literal["success", "clarified", "failed", "uncertain"]


@dataclass
class Bill:
    bill_id: str
    text: str
    sent_at: str
    facts: dict = field(default_factory=dict)
    status: Status = "failed"
    feedback: str = "尚未执行写入。"
    notice: str = ""
    record_id: str | None = None
    confirmed: Expense | None = None
    action: str = "create"
    operation_id: str | None = None

    def context(self):
        return {
            "bill_id": self.bill_id,
            "raw": self.text,
            "sent_at": self.sent_at,
            "status": self.status,
            "expense": self.confirmed.model_dump(mode="json") if self.confirmed else None,
        }


@dataclass
class Session:
    bills: dict[str, Bill] = field(default_factory=dict)
    next_number: int = 1
    touched: float = field(default_factory=time.monotonic)

    def identify(self, message, recognition):
        items = []
        for item in recognition.items:
            if item.type == "new":
                bill_id = f"B{self.next_number}"
                self.next_number += 1
                self.bills[bill_id] = Bill(bill_id, item.raw, message.sent_at.isoformat())
            else:
                bill_id = item.target_id
            items.append(IdentifiedItem(bill_id=bill_id, type=item.type, raw=item.raw))
        return items

    def context(self, message, timezone):
        sent = message.sent_at.astimezone(timezone)
        return {
            "message_sent_at": sent.isoformat(),
            "timezone": str(timezone),
            "default_date": sent.date().isoformat(),
            "allowed_items": [bill.context() for bill in self.bills.values()],
        }


def render(session, touched, notes, *, test_mode=False):
    visible = [session.bills[key] for key in touched]
    visible.sort(key=lambda bill: int(bill.bill_id[1:]))
    groups = [
        ("success", "create", "准备新增消费记录：" if test_mode else "已记账消费记录："),
        ("success", "update", "准备更新消费记录：" if test_mode else "已更新消费记录："),
        ("clarified", None, "以下消费记录未写入："),
        ("failed", None, "以下消费记录执行失败："),
        ("uncertain", None, "以下消费记录待核对："),
    ]
    lines = list(dict.fromkeys(notes))
    counts = {
        status: sum(b.status == status for b in visible)
        for status in ("success", "clarified", "failed", "uncertain")
    }
    for status, action, title in groups:
        rows = [b for b in visible if b.status == status and (action is None or b.action == action)]
        if not rows:
            continue
        if lines:
            lines.append("")
        lines.append(title)
        for number, bill in enumerate(rows, 1):
            if status == "success":
                expense = bill.confirmed
                row = bill_label(
                    expense.date, expense.category.model_dump(), expense.description, expense.amount
                )
                # Retain local journal-save warnings even after a confirmed remote success.
                if bill.notice:
                    row += "。" + bill.notice
            else:
                row = (
                    " ".join(bill.text.splitlines())
                    + "。原因："
                    + " ".join(bill.feedback.splitlines())
                )
            lines.append(f"{number}. {bill.bill_id}：{row}")
    return "\n".join(lines) if lines else "此次不包含消费行为。", counts
