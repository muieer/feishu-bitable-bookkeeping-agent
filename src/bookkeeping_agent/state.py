"""Short-lived bill facts, execution state and the last final reply's numbering."""

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .formatting import bill_label
from .models import Expense

Status = Literal["success", "pending", "failed", "uncertain", "cancelled"]


@dataclass
class Bill:
    bill_id: str
    text: str
    sent_at: str
    facts: dict = field(default_factory=dict)
    status: Status = "pending"
    feedback: str = ""
    record_id: str | None = None
    confirmed: Expense | None = None
    pending_action: str = "create"
    patch: dict = field(default_factory=dict)
    operation_id: str | None = None

    def context(self, number=None):
        return {
            "bill_id": self.bill_id,
            "number": number,
            "text": self.text,
            "sent_at": self.sent_at,
            "expense": self.facts,
            "status": self.status,
            "question": self.feedback,
            "pending_action": self.pending_action,
            "pending_patch": self.patch,
            "record_id": self.record_id,
            "confirmed": self.confirmed.model_dump(mode="json") if self.confirmed else None,
        }


@dataclass
class Session:
    bills: dict[str, Bill] = field(default_factory=dict)
    display: list[str] = field(default_factory=list)
    last_success_id: str | None = None
    turns: list[dict] = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)

    def add(self, message, decision):
        # Deterministic for redelivery; pending followups keep the original bill identity.
        identity = (
            f"{message.user_id}/{message.chat_id}/{message.message_id}/{decision.decision_id}"
        )
        bill_id = "bill_" + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        bill = Bill(bill_id, decision.source_text, message.sent_at.isoformat())
        self.bills[bill_id] = bill
        return bill

    def pending(self):
        return [b for b in self.bills.values() if b.status in {"pending", "failed"}]

    def context(self, message, timezone):
        sent = message.sent_at.astimezone(timezone)
        pending = self.pending()
        last = self.bills.get(self.last_success_id)
        return {
            "message_sent_at": sent.isoformat(),
            "timezone": str(timezone),
            "default_date": sent.date().isoformat(),
            "relative_date_base": sent.isoformat(),
            "displayed_items": [self.bills[k].context(n) for n, k in enumerate(self.display, 1)],
            "pending_items": [b.context() for b in pending],
            "allowed_items": [b.context() for b in self.bills.values() if b.status != "cancelled"],
            "last_record": last.context() if last else None,
            "recent_turns": self.turns,
        }


def ordinal_number(value):
    if value.isdecimal():
        return int(value)
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    total, digit = 0, 0
    for char in value:
        if char in digits:
            digit = digits[char]
        else:
            total += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
    return total + digit


def explicit_numbers(text):
    numerals = r"[0-9０-９零〇一二两三四五六七八九十百千]+"
    matches = list(
        re.finditer(
            rf"第\s*(?P<number>{numerals})\s*(?:笔(?:账单)?|个(?:账单)?|条(?:账单)?|号)", text
        )
    )
    for pattern in (
        rf"^\s*(?:请把|请将|把|将)?\s*(?P<number>{numerals})\s*(?:笔(?:账单)?|号|个账单|条账单)",
        rf"^\s*(?P<number>{numerals})\s*(?=的|是|改|[,，:：])",
    ):
        match = re.match(pattern, text)
        if match:
            matches.append(match)
    return {ordinal_number(m["number"]) for m in matches}


def historical_number(text):
    return bool(
        explicit_numbers(text)
        and re.search(r"(?:之前|此前|原来|最初|上一张|上次).*(?:列表|卡片|第)", text)
    )


def has_update_intent(text):
    return bool(
        re.search(
            r"改|更正|纠正|不对|实际|(?:金额|日期|分类|描述|具体).{0,6}(?:是|为)|"
            r"(?:刚才|这笔|那笔|第.{1,6}(?:笔|个)).{0,10}是\s*[\d零〇一二两三四五六七八九]",
            text,
        )
    )


def render(session, touched, notes, *, test_mode=False, ignored=False):
    visible = (
        []
        if ignored
        else [
            b
            for b in session.bills.values()
            if b.status != "cancelled" and (b.bill_id in touched or b.status != "success")
        ]
    )
    visible.sort(key=lambda b: b.status != "success")
    session.display = [b.bill_id for b in visible]
    labels = {
        "success": "准备记账" if test_mode else "成功",
        "pending": "待补充",
        "failed": "待处理",
        "uncertain": "待核对",
    }
    counts = {s: sum(b.status == s for b in visible) for s in labels}
    notes = list(dict.fromkeys(notes))
    if len(visible) == 1 and not notes:
        bill = visible[0]
        lines = [bill.feedback]
    elif not visible:
        lines = notes or ["这条消息未涉及实际消费，本次未记账。"]
    else:
        lines = [
            *notes,
            "结果："
            + "，".join(
                f"{label}{counts[status]}笔" for status, label in labels.items() if counts[status]
            ),
        ]
        for n, bill in enumerate(visible, 1):
            facts = bill.facts
            if facts.get("amount") is not None:
                row = bill_label(
                    facts.get("date") or datetime.fromisoformat(bill.sent_at).date(),
                    facts.get("category") or {},
                    facts.get("description") or bill.text,
                    facts["amount"],
                )
            else:
                row = bill.text
            if bill.status != "success":
                row += f"。{labels[bill.status]}：{bill.feedback}"
            lines.append(f"{n}. {row}")
    return "\n".join(" ".join(line.splitlines()) for line in lines), counts
