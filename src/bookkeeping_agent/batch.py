"""Batch orchestration reuses the single-expense validation and write boundary."""

import asyncio
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .formatting import bill_label
from .models import Decision
from .tracing import current_trace, emit, require


@dataclass
class BatchItem:
    item_id: str
    text: str
    sent_at: str
    state: object
    status: str = "pending"
    feedback: str = ""
    draft: dict = field(default_factory=dict)

    def facts(self):
        values = self.state.last_expense.model_dump(mode="json") if self.state.last_expense else {}
        return {**values, **(self.state.pending or {}).get("expense", {}), **self.draft}


@dataclass
class BatchState:
    items: dict[str, BatchItem] = field(default_factory=dict)
    display: list[str] = field(default_factory=list)
    active_ids: set[str] = field(default_factory=set)

    def add(self, session, text, sent_at):
        item = BatchItem(f"item_{len(self.items) + 1}", text, sent_at, session)
        self.items[item.item_id] = item
        return item


def split_expenses(text):
    return [part.strip() for part in text.split("。") if part.strip()]


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


def numbered_target(text):
    """Find explicit bill ordinals; the model handles remaining natural references."""
    numerals = r"[0-9０-９零〇一二两三四五六七八九十百千]+"
    # An ordinal can appear anywhere; a bare reference must start the instruction.
    matches = list(
        re.finditer(
            rf"第\s*(?P<number>{numerals})\s*(?:笔(?:账单)?|个(?:账单)?|条(?:账单)?|号)", text
        )
    )
    shorthand = re.match(
        rf"^\s*(?:请把|请将|把|将)?\s*(?P<number>{numerals})\s*(?:笔(?:账单)?|号|个账单|条账单)",
        text,
    )
    if shorthand:
        matches.append(shorthand)
    bare = re.match(rf"^\s*(?P<number>{numerals})\s*(?=的|是|改|[,，:：])", text)
    numbers = {ordinal_number(m["number"]) for m in matches}
    if bare:
        numbers.add(ordinal_number(bare["number"]))
    if numbers and re.search(r"(?:之前|此前|原来|最初|上一张|上次).*(?:列表|卡片|第)", text):
        return None, "编号只对应最新一次回复的列表，请按最新列表说明要处理的账单。"
    if len(numbers) > 1:
        return None, "每个片段只处理一笔账单，请用中文句号「。」分隔不同账单的操作。"
    return (next(iter(numbers)), None) if numbers else (None, None)


def resolve_number(number, display, batch):
    if number is None:
        return None, None
    if not 0 < number <= len(display):
        return None, f"最新列表没有第 {number} 笔账单，请按最新列表编号说明要处理的账单。"
    return batch.items[display[number - 1]], None


def item_context(context, item, number, timezone):
    result = dict(context)
    state = item.state
    anchor = datetime.fromisoformat(item.sent_at).astimezone(ZoneInfo(timezone))
    result.update(
        selected_number=number,
        pending=state.pending,
        pending_default_date=anchor.date().isoformat(),
        pending_relative_date_base=anchor.isoformat(),
        last_record={
            "record_id": state.last_record_id,
            "expense": state.last_expense.model_dump(mode="json"),
        }
        if state.last_expense
        else None,
        recent_turns=state.turns,
    )
    return result


def render_batch(batch, touched, notes, *, test_mode=False):
    visible = [
        item for item in batch.items.values() if item.item_id in touched or item.status != "success"
    ]
    visible.sort(key=lambda item: item.status != "success")
    # A final reply replaces the mapping, including replies without any bill rows.
    batch.display = [item.item_id for item in visible]
    counts = {}
    labels = {
        "success": "准备记账" if test_mode else "成功",
        "pending": "待补充",
        "failed": "待处理",
        "uncertain": "待核对",
    }
    for item in visible:
        counts[item.status] = counts.get(item.status, 0) + 1
    summary = (
        "结果："
        + "，".join(
            f"{label}{counts[status]}笔" for status, label in labels.items() if counts.get(status)
        )
        if visible
        else "本次未新增或修改账单。"
    )
    lines = [*dict.fromkeys(notes), summary]
    for index, item in enumerate(visible, 1):
        facts = item.facts()
        if not facts.get("amount"):
            # A missing model field is not evidence that the original message lacked an amount.
            lines.append(f"{index}. {item.text}。{labels[item.status]}：{item.feedback}")
            continue
        row = bill_label(
            facts.get("date") or item.sent_at[:10],
            facts.get("category") or {},
            facts.get("description") or item.text,
            facts["amount"],
        )
        if item.status != "success":
            row += f"。{labels[item.status]}：{item.feedback}"
        lines.append(f"{index}. {row}")
    # Keep exactly one plain-text element per result, even when user data contains newlines.
    return "\n".join(" ".join(line.splitlines()) for line in lines), counts


async def reply_batch(service, session, message, context, fragments):
    try:
        return await _reply_batch(service, session, message, context, fragments)
    except BaseException:
        if session.batch:
            response, counts = render_batch(
                session.batch,
                session.batch.active_ids,
                ["批量处理已中断；尚未处理的片段未记账，请先核对以下结果。"],
                test_mode=service.settings.test_mode,
            )
            trace = current_trace()
            if trace:
                trace.response = response
            emit("batch.interrupted", response=response, counts=counts)
        raise


async def _reply_batch(service, session, message, context, fragments):
    if session.batch is None:
        session.batch = BatchState()
        if session.last_expense:
            child = type(session)(
                last_record_id=session.last_record_id,
                last_expense=session.last_expense,
                uncertain=session.uncertain,
            )
            previous = session.batch.add(
                child, session.last_expense.description, context["message_sent_at"]
            )
            previous.status = "uncertain" if session.uncertain else "success"
        if session.pending:
            child = type(session)(pending=deepcopy(session.pending), turns=list(session.turns))
            session.batch.add(child, session.pending["text"], session.pending["sent_at"])
            session.pending = None
    batch = session.batch
    batch.active_ids = set()
    display = tuple(batch.display)
    attention = False
    emit("batch.started", total=len(fragments))
    require()
    # Reserve the envelope as well as individual writes, so redelivery/restart cannot
    # replay an earlier successful part of this message.
    try:
        service.journal.save(message.message_id, "started", action="batch")
    except OSError:
        return "无法保存去重状态，未执行写入，请检查本地文件权限。"
    touched, notes = batch.active_ids, []
    for index, text in enumerate(fragments):
        emit("batch.item_started", processed=index, total=len(fragments))
        operation = message.model_copy(
            update={"text": text, "message_id": f"{message.message_id}/part/{index + 1}"}
        )
        number, error = numbered_target(text)
        target, target_error = resolve_number(number, display, batch)
        error = error or target_error
        if error:
            attention = True
            notes.append(error)
            continue
        runtime = {
            **context,
            "last_record": None,
            "pending": session.pending,
            # Historical numbered results must not compete with the current display.
            "recent_turns": [],
            "displayed_items": [
                {
                    "number": n,
                    "text": batch.items[item_id].text,
                    "status": batch.items[item_id].status,
                    "expense": batch.items[item_id].facts(),
                    "question": batch.items[item_id].feedback,
                    "sent_at": batch.items[item_id].sent_at,
                }
                for n, item_id in enumerate(display, 1)
            ],
        }
        # Only provide a unique pending candidate when the user did not name another row.
        pending = [item for item in batch.items.values() if item.status in {"pending", "failed"}]
        if target:
            runtime = item_context(runtime, target, number, service.settings.display_timezone)
        elif len(pending) == 1:
            pending_context = item_context(
                runtime, pending[0], None, service.settings.display_timezone
            )
            for key in ("pending", "pending_default_date", "pending_relative_date_base"):
                runtime[key] = pending_context[key]
        emit("decision.context", context=runtime)
        require()
        try:
            decision = await asyncio.wait_for(
                service.engine.decide(text, runtime), service.settings.agent_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 -- Provider errors never authorize a write.
            emit("decision.error", status="error", error=exc)
            if target and target.status == "success":
                attention = True
                notes.append("消息理解未完成，本次未修改账单，请说明最新列表编号和需要修改的内容。")
                continue
            if target is None:
                target = batch.add(type(session)(), text, context["message_sent_at"])
            target.feedback = "消息理解未完成，请补充这笔消费的信息后再试。"
            if target.status != "uncertain":
                target.status = "failed"
            touched.add(target.item_id)
            continue
        emit("decision.branch", decision=decision)
        require()
        # Referencing a row is not permission to edit it.
        if decision.action == "ignore":
            notes.append(
                f"片段「{text}」未涉及实际消费或明确修改，该片段未记账。"
                if len(fragments) > 1
                else "这条消息未涉及实际消费或明确修改，本次未记账。"
            )
            continue
        if decision.action == "preference":
            notes.append(await service._preference(session, operation, decision))
            attention |= session.pending is not None
            continue
        if decision.target_number is not None:
            selected, error = resolve_number(decision.target_number, display, batch)
            if error or (number is not None and number != decision.target_number):
                attention = True
                notes.append(error or "账单编号识别不一致，请确认要操作的最新列表编号。")
                continue
            target = selected
        if decision.action == "clarify" and decision.clarification_scope == "target":
            attention = True
            notes.append(decision.question)
            continue
        if target is None and decision.continues_pending:
            if len(pending) == 1:
                target = pending[0]
            else:
                attention = True
                notes.append(
                    "当前没有待补充账单，请说明要修改的最新列表编号或提供新的消费信息。"
                    if not pending
                    else "有多笔账单待补充，请说明最新列表编号或消费内容。"
                )
                continue
        if target is None and decision.action == "update":
            attention = True
            notes.append("请说明要修改的最新列表编号或消费内容。")
            continue
        if target and target.state.last_expense and decision.action == "create":
            attention = True
            notes.append("这笔账单已记入，请说明是否修改，以及需要修改的内容。")
            continue
        if target is None:
            target = batch.add(type(session)(), text, context["message_sent_at"])
        if target.status == "uncertain" or target.state.uncertain:
            target.status = "uncertain"
            target.feedback = "上次写入结果待核对，请先检查表格；本次未重复写入。"
            continue
        child = target.state
        if decision.expense:
            target.draft = {
                **target.facts(),
                **decision.expense.model_dump(mode="json", exclude_none=True),
            }
        if not child.last_expense and decision.action == "update":
            decision = decision.model_copy(update={"action": "create", "record_id": None})
        if child.pending:
            decision = decision.model_copy(update={"continues_pending": True})
        if decision.multiple_expenses:
            decision = Decision(
                action="clarify",
                expense=decision.expense,
                question="请用中文句号「。」分隔这几笔消费。",
            )
        if decision.action == "clarify" or decision.unresolved or decision.question:
            target.feedback = service._ask(
                child,
                operation,
                decision,
                decision.question or "请补充：" + "、".join(decision.unresolved),
            )
            target.status = "pending"
        else:
            default_date = (
                datetime.fromisoformat(target.sent_at)
                .astimezone(ZoneInfo(service.settings.display_timezone))
                .date()
                .isoformat()
            )
            touched.add(target.item_id)
            try:
                target.feedback = await service._expense(child, operation, decision, default_date)
            except BaseException:
                saved = service.journal.get(operation.message_id)
                if child.uncertain or (saved and saved["status"] in {"started", "uncertain"}):
                    target.status = "uncertain"
                    child.uncertain = True
                    target.feedback = "处理已中断，写入结果待核对；请勿重复发送。"
                raise
            saved = service.journal.get(operation.message_id)
            if saved and saved["status"] == "success":
                target.status = "success"
                target.draft = {}
                session.last_record_id, session.last_expense = (
                    child.last_record_id,
                    child.last_expense,
                )
                session.uncertain = False
            elif child.uncertain or (saved and saved["status"] in {"started", "uncertain"}):
                target.status = "uncertain"
                child.uncertain = True
            elif child.pending:
                target.status = "pending"
            else:
                target.status = "failed"
                # Preserve parsed facts and the original anchor even on a preflight failure.
                service._ask(child, operation, decision, target.feedback)
        if child.pending:
            child.pending["sent_at"] = target.sent_at
        emit(
            "batch.item_completed",
            processed=index + 1,
            total=len(fragments),
            item_id=target.item_id,
            item_status=target.status,
        )
    response, counts = render_batch(batch, touched, notes, test_mode=service.settings.test_mode)
    trace = current_trace()
    if trace:
        trace.response = response
    try:
        service.journal.save(message.message_id, "success", action="batch", response=response)
    except OSError:
        response += "\n本地去重状态保存失败，请勿重复发送这批消费。"
    session.turns.extend([{"user": message.text}, {"result": response}])
    session.turns[:] = session.turns[-6:]
    emit("batch.completed", response=response, counts=counts, attention=attention)
    if trace:
        trace.response = response
    return response
