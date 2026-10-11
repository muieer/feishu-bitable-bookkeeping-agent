"""Complete-message planning and independent validated operations in one pipeline."""

import asyncio
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior

from .agent import DecisionEngine, source_span
from .config import Settings
from .formatting import bill_label
from .knowledge import Knowledge, KnowledgeError
from .models import Expense, IncomingMessage, MessagePlan
from .state import Session, explicit_numbers, has_update_intent, historical_number, render
from .storage import OperationJournal
from .tools import BitableTool, QueryError, same_field
from .tracing import (
    LOG_FAILED,
    LOG_FAILED_SUFFIX,
    TraceLogError,
    TraceStore,
    current_trace,
    emit,
    require,
)

UNCERTAIN = "上次写入结果待核对，请先检查表格；本次未再次执行记账。"
logger = logging.getLogger(__name__)


class BookkeepingService:
    def __init__(self, settings: Settings, tool: BitableTool, model=None, *, engine=None):
        self.settings, self.tool = settings, tool
        self.traces = TraceStore(settings)
        self.knowledge = Knowledge(settings)
        self.engine = engine or DecisionEngine(settings, tool, self.knowledge, model)
        self.journal = OperationJournal(
            settings.operation_journal_path,
            f"{settings.bitable_base_token}/{settings.bitable_table_id}"
            + ("/test" if settings.test_mode else ""),
        )
        self.sessions: dict[tuple[str, str], Session] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.timezone = ZoneInfo(settings.display_timezone)

    async def aclose(self):
        if hasattr(self.engine, "aclose"):
            await self.engine.aclose()

    async def reply(self, message: IncomingMessage) -> str | None:
        trace = current_trace() or self.traces.claim(message.message_id)
        with self.traces.scope(trace):
            logger.info("message_trace message_id=%s path=%s", message.message_id, trace.path)
            emit("message.normalized", message=message)
            response = None
            try:
                require()
                response = await self._reply(message)
            except TraceLogError:
                response = trace.response or (UNCERTAIN if trace.mutation_submitted else LOG_FAILED)
            except BaseException as exc:
                emit("message.error", status="error", error=exc)
                raise
            finally:
                emit("session.after", session=self.sessions.get((message.user_id, message.chat_id)))
            emit(
                "message.result",
                response=response,
                silent=response is None,
                mutation_submitted=trace.mutation_submitted,
            )
            if trace.failed and response is not None:
                if trace.mutation_submitted:
                    if LOG_FAILED_SUFFIX not in response:
                        response += LOG_FAILED_SUFFIX
                else:
                    response = LOG_FAILED
            trace.response = response
            return response

    async def _reply(self, message):
        lock = self.locks.setdefault(message.chat_id, asyncio.Lock())
        emit("session.queue", chat_id=message.chat_id, locked=lock.locked())
        require()
        queued = time.monotonic()
        async with lock:
            emit("session.lock_acquired", wait_ms=(time.monotonic() - queued) * 1000)
            require()
            previous = self.journal.get(message.message_id)
            emit("dedup.checked", previous=previous)
            require()
            if previous:
                emit("message.deduplicated", reason=previous["status"])
                return UNCERTAIN if previous["status"] in {"started", "uncertain"} else None
            key = (message.user_id, message.chat_id)
            session = self.sessions.get(key)
            expired = session is not None and (
                time.monotonic() - session.touched > self.settings.context_ttl_seconds
            )
            emit("session.before", session=session, expired=expired)
            if session is None or expired:
                session = Session()
                self.sessions[key] = session
            session.touched = time.monotonic()
            context = session.context(message, self.timezone)
            emit("decision.context", context=context)
            require()
            try:
                plan = await asyncio.wait_for(
                    self.engine.decide(message.text, context),
                    self.settings.agent_timeout_seconds + 1,
                )
                # The complete envelope is validated before any state or write is changed.
                plan = MessagePlan.model_validate(plan)
                if any(source_span(message.text, d) is None for d in plan.decisions):
                    raise ValueError("ungrounded decision source")
            except TimeoutError:
                emit("decision.timeout", status="error")
                session.display = []
                return "理解消息超时，未执行写入，请稍后重试。"
            except Exception as exc:  # noqa: BLE001 -- Invalid understanding never authorizes writes.
                emit("decision.error", status="error", error=exc)
                require()
                session.display = []
                if isinstance(exc, (UnexpectedModelBehavior, ValidationError, ValueError)):
                    return "消息理解失败：模型返回的记账信息不完整或格式不正确，本次未执行写入。"
                return "消息理解失败，未执行写入，请检查模型配置和网络连接。"
            emit("message.plan", plan=plan)
            require()
            if all(d.action == "ignore" and not d.cancel_pending for d in plan.decisions):
                session.display = []
                emit("message.ignored", reason="decision_ignore")
                return "这条消息未涉及实际消费，本次未记账。"
            try:
                self.journal.save(message.message_id, "started", action="message")
            except OSError:
                return "无法保存去重状态，未执行写入，请检查本地文件权限。"
            touched, notes = set(), []
            total = len(plan.decisions)
            if total > 1:
                emit("batch.started", total=total)
            try:
                await self._execute(session, message, plan, touched, notes)
            except BaseException:
                # Remaining bills are retained as unexecuted, not inferred to have succeeded.
                response, counts = render(
                    session,
                    touched,
                    [*notes, "处理已中断，请核对以下结果。"],
                    test_mode=self.settings.test_mode,
                )
                current_trace().response = response
                emit("batch.interrupted", response=response, counts=counts)
                raise
            response, counts = render(session, touched, notes, test_mode=self.settings.test_mode)
            current_trace().response = response
            try:
                self.journal.save(
                    message.message_id, "success", action="message", response=response
                )
            except OSError:
                response += "\n本地去重状态保存失败，请勿重复发送这条消息中的消费。"
            session.turns.extend([{"user": message.text}, {"result": response}])
            session.turns[:] = session.turns[-6:]
            emit(
                "message.completed",
                response=response,
                counts=counts,
                attention=bool(notes) or bool(counts["pending"]),
                visible=len(session.display),
                listed=len(session.display) > 1 or bool(notes and session.display),
            )
            current_trace().response = response
            return response

    def _route(self, session, decision, display, pending):
        numbers = explicit_numbers(decision.source_text)
        if historical_number(decision.source_text):
            return None, "编号只对应最新一次回复的列表，请按最新列表说明要处理的账单。"
        if len(numbers) > 1:
            return None, "这一决策涉及多个目标，请分别明确各账单需要修改或补充的内容。"
        number = next(iter(numbers), decision.target_number)
        if numbers and decision.target_number is not None and number != decision.target_number:
            return None, "账单编号识别不一致，请确认要操作的最新列表编号。"
        target = None
        if number is not None:
            if not 0 < number <= len(display):
                return None, f"最新列表没有第 {number} 笔账单，请按最新列表编号说明。"
            target = session.bills[display[number - 1]]
        if decision.target_id:
            named = session.bills.get(decision.target_id)
            if named is None or named.status == "cancelled":
                return None, "目标不属于当前用户、当前会话允许操作的账单。"
            if target and named.bill_id != target.bill_id:
                return None, "账单编号与目标不一致，请确认要操作的账单。"
            if target is None or target.status == "cancelled":
                reference = decision.target_reference
                if not reference or reference not in decision.source_text:
                    return None, "请用最新列表编号或明确消费内容指定目标。"
                matches = [
                    b
                    for b in session.bills.values()
                    if b.status != "cancelled"
                    and (reference in b.text or reference in b.facts.get("description", ""))
                ]
                if len(matches) != 1 or matches[0].bill_id != named.bill_id:
                    return None, "有多个可能的目标，请用最新列表编号指定账单。"
            target = named
        if target is None and (decision.continues_pending or decision.cancel_pending):
            if len(pending) != 1:
                return None, "有多笔账单待补充，请说明最新列表编号或消费内容。" if pending else (
                    "当前没有待补充账单，请提供新的消费信息或明确纠错目标。"
                )
            target = pending[0]
        if target is None and decision.action == "update":
            target = session.bills.get(session.last_success_id)
            if target is None:
                return None, "当前会话没有可纠错的最近账单，请提供完整消费信息重新记账。"
        return target, None

    async def _execute(self, session, message, plan, touched, notes):
        display, pending = tuple(session.display), tuple(session.pending())
        routed = [(d, *self._route(session, d, display, pending)) for d in plan.decisions]
        # Conflicting operations on the same business object cannot be resolved by order.
        targets = [
            b.bill_id
            for d, b, error in routed
            if b and not error and (d.action != "ignore" or d.cancel_pending)
        ]
        spans = [
            source_span(message.text, d)
            for d, b, error in routed
            if not b
            and not error
            and d.action in {"create", "clarify"}
            and d.clarification_scope == "expense"
        ]
        creates, updates, preferences = [], [], []
        for decision, target, error in routed:
            emit("decision.branch", decision=decision)
            require()
            if decision.action == "ignore" and not decision.cancel_pending:
                continue
            if error:
                notes.append(error)
                continue
            if target and targets.count(target.bill_id) > 1:
                notes.append("同一账单出现多个操作，请合并说明最终需要修改或补充的内容。")
                continue
            if decision.action == "clarify" and decision.clarification_scope == "target":
                notes.append(decision.question)
                emit("business.clarify", question=decision.question, decision=decision)
                continue
            if decision.cancel_pending:
                if "取消" not in decision.source_text and "不记" not in decision.source_text:
                    notes.append("请明确是否取消这笔未完成记账。")
                elif target.status in {"pending", "failed"} and not self._reserved(target):
                    if target.confirmed:
                        target.status = "success"
                        target.facts, target.patch = target.confirmed.model_dump(mode="json"), {}
                    else:
                        target.status = "cancelled"
                    notes.append("已取消这笔未完成记账：" + target.text)
                    emit("business.cancelled", bill_id=target.bill_id)
                else:
                    notes.append("只能取消尚未提交的未完成操作；已记账或待核对账单请先核对表格。")
                continue
            if decision.action == "preference":
                preferences.append(decision)
                continue
            span = source_span(message.text, decision)
            if (
                target is None
                and decision.action in {"create", "clarify"}
                and (
                    any(
                        span != other and max(span[0], other[0]) < min(span[1], other[1])
                        for other in spans
                    )
                    or spans.count(span) > 1
                )
            ):
                notes.append("消费边界有重叠或重复，请明确每笔独立交易及实付金额。")
                continue
            if target and target.status == "uncertain":
                touched.add(target.bill_id)
                target.feedback = UNCERTAIN
                continue
            if target and target.confirmed and decision.action == "create":
                notes.append("这笔账单已记入，请说明是否修改，以及需要修改的内容。")
                continue
            continuing_update = bool(
                target
                and target.confirmed
                and target.status in {"pending", "failed"}
                and target.pending_action == "update"
                and decision.continues_pending
            )
            if (
                target
                and target.confirmed
                and not (has_update_intent(decision.source_text) or continuing_update)
            ):
                notes.append("仅提及账单不会修改记录，请明确需要修改的字段。")
                continue
            if target is None:
                target = session.add(message, decision)
                target.sent_at = message.sent_at.astimezone(self.timezone).isoformat()
            touched.add(target.bill_id)
            if self._reserved(target):
                target.status, target.feedback = "uncertain", UNCERTAIN
                continue
            patch = (
                decision.expense.model_dump(mode="json", exclude_none=True)
                if decision.expense
                else {}
            )
            if target.confirmed:
                if not continuing_update:
                    target.patch = {}
                target.pending_action = "update"
                target.patch.update(patch)
                target.facts = {**target.confirmed.model_dump(mode="json"), **target.patch}
            else:
                target.pending_action = "create"
                if (
                    patch.get("amount") is not None
                    and not re.search(
                        r"[\d零〇一二两三四五六七八九十百千万亿]", decision.source_text
                    )
                    and not (
                        decision.continues_pending or decision.target_id or decision.target_number
                    )
                ):
                    patch.pop("amount")
                target.facts.update(patch)
            if decision.action == "clarify" or decision.question or decision.unresolved:
                self._ask(target, decision.question or "请补充：" + "、".join(decision.unresolved))
                continue
            expense = self._validate(target, message)
            if expense is not None:
                target.status, target.feedback = "failed", "尚未执行写入。"
                operation_id = (
                    f"bill/{target.bill_id}/create"
                    if target.pending_action == "create"
                    else f"{message.message_id}/decision/{decision.decision_id}"
                )
                target.operation_id = operation_id
                entry = (target, expense, operation_id)
                (creates if target.pending_action == "create" else updates).append(entry)
        if creates or updates:
            async with self.tool.write_lock:
                emit("table.lock_acquired")
                require()
                try:
                    schema = await self.tool.field_schema()
                    emit("table.schema", schema=schema)
                    require()
                    self.knowledge.validate_schema(schema)
                    emit("table.schema_validated", schema=schema)
                    require()
                except (QueryError, KnowledgeError) as exc:
                    emit("table.preflight_failed", status="error", error=exc)
                    for bill, _, _ in [*creates, *updates]:
                        bill.feedback = str(exc)
                except Exception as exc:  # noqa: BLE001 -- Read failure cannot authorize a write.
                    emit("table.preflight_failed", status="error", error=exc)
                    require()
                    for bill, _, _ in [*creates, *updates]:
                        bill.feedback = "表格检查失败，本次未执行这笔操作。"
                else:
                    await self._create(session, creates)
                    for entry in updates:
                        await self._update(session, entry)
        for decision in preferences:
            notes.append(await self._preference(message, decision))

    def _reserved(self, bill):
        entry = self.journal.get(bill.operation_id) if bill.operation_id else None
        return bool(
            bill.status != "success" and entry and entry["status"] in {"started", "uncertain"}
        )

    def _ask(self, bill, question):
        bill.status, bill.feedback = "pending", question
        emit("business.clarify", question=question, bill_id=bill.bill_id)

    def _validate(self, bill, message):
        emit("expense.merged", values=bill.facts, action=bill.pending_action)
        require()
        try:
            default_date = datetime.fromisoformat(bill.sent_at).astimezone(self.timezone).date()
            expense = Expense.model_validate({"date": default_date, **bill.facts})
            self.knowledge.validate_category(expense.category)
            if expense.date > message.sent_at.astimezone(self.timezone).date():
                raise KnowledgeError("消费日期在未来，请确认实际消费日期。")
            if bill.pending_action == "update" and not bill.patch:
                raise KnowledgeError("请说明需要修改的金额、日期、描述或分类。")
        except KnowledgeError as exc:
            emit("expense.validation_failed", status="error", error=exc)
            self._ask(bill, str(exc))
            return None
        except ValidationError as exc:
            emit("expense.validation_failed", status="error", error=exc)
            labels = {
                "amount": "有效金额（正数，最多两位小数）",
                "description": "消费内容",
                "category": "合法分类",
                "date": "有效消费日期",
            }
            missing = list(dict.fromkeys(labels.get(e["loc"][0], "账单信息") for e in exc.errors()))
            self._ask(bill, "请补充或确认：" + "、".join(missing) + "。")
            return None
        emit("expense.validated", expense=expense)
        require()
        return expense

    def _confirm(self, session, bill, expense, operation_id, record_id):
        bill.confirmed, bill.record_id = expense, record_id
        bill.facts, bill.patch = expense.model_dump(mode="json"), {}
        bill.status = "success"
        session.last_success_id = bill.bill_id
        action = bill.pending_action
        label = (
            "准备记账" if self.settings.test_mode else "已修改" if action == "update" else "已记账"
        )
        bill.feedback = (
            label
            + "："
            + bill_label(
                expense.date, expense.category.model_dump(), expense.description, expense.amount
            )
        )
        emit(
            "table.prepared" if self.settings.test_mode else "table.confirmed",
            record_id=record_id,
            expense=expense,
            action=action,
            bill_id=bill.bill_id,
        )
        try:
            self.journal.save(
                operation_id, "success", action=action, record_id=record_id, bill_id=bill.bill_id
            )
        except OSError:
            # Remote confirmation is still true. Protect the live bill and retain started on disk.
            bill.feedback += "。表格写入已成功，但本地去重状态保存失败；请勿重复发送这笔消费。"
        emit("business.completed", response=bill.feedback, bill_id=bill.bill_id)

    def _uncertain(self, bill, error=UNCERTAIN):
        bill.status, bill.feedback = "uncertain", error

    async def _create(self, session, entries):
        if not entries:
            return
        require()
        if self.settings.test_mode:
            for bill, expense, operation_id in entries:
                self._confirm(session, bill, expense, operation_id, None)
            return
        operations = {op: {"action": "create", "bill_id": bill.bill_id} for bill, _, op in entries}
        emit(
            "table.write_planned",
            operations=operations,
            fields=[self.tool.expense_fields(expense) for _, expense, _ in entries],
            durable=True,
        )
        require()
        try:
            self.journal.reserve_operations(operations)
        except (OSError, ValueError):
            for bill, _, _ in entries:
                bill.feedback = "无法保存逐笔去重状态，未执行写入，请检查本地文件权限。"
            return
        try:
            require()
            ids = await self.tool.create_records([expense for _, expense, _ in entries])
            if (
                not isinstance(ids, list)
                or len(ids) != len(entries)
                or any(i is not None and (not isinstance(i, str) or not i.strip()) for i in ids)
                or len([i for i in ids if i]) != len({i for i in ids if i})
            ):
                raise QueryError("批量写入结果无法逐笔对应，请核对表格。")
        except asyncio.CancelledError:
            for bill, _, _ in entries:
                self._uncertain(bill)
            emit("table.cancelled", status="error")
            raise
        except Exception as exc:  # noqa: BLE001 -- Any failure after reservation prohibits retries.
            emit("table.write_failed", status="error", error=exc)
            for bill, _, _ in entries:
                self._uncertain(bill, str(exc) if isinstance(exc, QueryError) else UNCERTAIN)
            return
        for (bill, expense, operation_id), record_id in zip(entries, ids, strict=True):
            if record_id:
                self._confirm(session, bill, expense, operation_id, record_id)
            else:
                self._uncertain(
                    bill, "新增已提交，但这笔账单尚未核对到完整结果；请检查表格，勿重复发送。"
                )

    async def _update(self, session, entry):
        bill, expense, operation_id = entry
        require()
        try:
            if not self.settings.test_mode:
                if not bill.record_id or bill.confirmed is None:
                    raise QueryError("当前会话没有可纠错的成功账单。")
                emit("table.correction_check", record_id=bill.record_id)
                actual = await self.tool.get_record(bill.record_id)
                original = self.tool.expense_fields(bill.confirmed)
                require()
                if not all(
                    same_field(k, actual.get(k), v, self.settings) for k, v in original.items()
                ):
                    raise QueryError("账单已被外部修改，请核对表格后再纠错。")
                emit("table.correction_preflight", actual=actual, original=original)
            fields = self.tool.expense_fields(expense)
            before = self.tool.expense_fields(bill.confirmed)
            changed = {k: v for k, v in fields.items() if v != before[k]}
        except QueryError as exc:
            emit("table.preflight_failed", status="error", error=exc)
            bill.feedback = str(exc)
            return
        except Exception as exc:  # noqa: BLE001 -- Read failure stays unexecuted.
            emit("table.preflight_failed", status="error", error=exc)
            require()
            bill.feedback = "无法核对远端账单，本次未修改。"
            return
        if self.settings.test_mode or not changed:
            self._confirm(session, bill, expense, operation_id, bill.record_id)
            return
        emit("table.write_planned", record_id=bill.record_id, fields=changed, durable=True)
        require()
        try:
            self.journal.save(operation_id, "started", action="update", bill_id=bill.bill_id)
        except OSError:
            bill.feedback = "无法保存去重状态，未执行修改，请检查本地文件权限。"
            return
        try:
            require()
            await self.tool.update_record(bill.record_id, changed)
        except asyncio.CancelledError:
            self._uncertain(bill)
            emit("table.cancelled", status="error")
            raise
        except Exception as exc:  # noqa: BLE001 -- Post-reservation outcomes are uncertain.
            emit("table.write_failed", status="error", error=exc)
            self._uncertain(bill, str(exc) if isinstance(exc, QueryError) else UNCERTAIN)
            return
        self._confirm(session, bill, expense, operation_id, bill.record_id)

    async def _preference(self, message, decision):
        preference = decision.preference
        if preference.evidence not in decision.source_text or not any(
            marker in preference.evidence
            for marker in ("以后", "今后", "始终", "每次", "一律", "都归", "总是")
        ):
            return "这是只修改本次账单，还是以后都按这个分类记账？分类偏好尚未保存。"
        try:
            self.knowledge.validate_category(preference.category)
        except KnowledgeError as exc:
            return str(exc) + "分类偏好尚未保存。"
        operation_id = f"{message.message_id}/decision/{decision.decision_id}"
        async with self.tool.write_lock:
            emit("preference.write_planned", preference=preference, durable=True)
            require()
            try:
                self.journal.save(operation_id, "started", action="preference")
                require()
                current_trace().mutation_submitted = True
                self.knowledge.update_preference(preference)
                emit("preference.saved", preference=preference)
                self.journal.save(operation_id, "success", action="preference")
            except OSError:
                return "分类偏好保存未确认成功，请检查本地文件权限；本次未修改历史账单。"
            except Exception as exc:  # noqa: BLE001 -- One preference failure cannot abort siblings.
                emit("preference.save_failed", status="error", error=exc)
                require()
                return "分类偏好保存未确认成功，请核对偏好文件；本次未修改历史账单。"
        response = (
            f"已保存分类偏好：{preference.condition} → {category_label(preference.category)}。"
        )
        emit("business.completed", response=response)
        return response


def category_label(category):
    return " / ".join(filter(None, (category.level1, category.level2, category.level3)))
