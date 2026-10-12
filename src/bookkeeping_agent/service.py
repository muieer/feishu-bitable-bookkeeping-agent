"""Recognize, allocate IDs, decide, validate and independently execute each bill."""

import asyncio
import logging
import time
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior

from .agent import DecisionEngine, validate_plan, validate_recognition
from .config import Settings
from .formatting import bill_label
from .knowledge import Knowledge, KnowledgeError
from .models import Expense, IncomingMessage, MessagePlan, RecognitionResult
from .request_budget import budget_exceeded, message_request_scope
from .state import Session, render
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
        self.engine = engine or DecisionEngine(settings, self.knowledge, model)
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
        with (
            self.traces.scope(trace),
            message_request_scope(self.settings.llm_message_request_limit) as budget,
        ):
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
                emit("llm.message_budget", requests=budget.requests, limit=budget.limit)
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
            previous = self.journal.get(message.message_id)
            emit("dedup.checked", previous=previous)
            require()
            if previous:
                emit("message.deduplicated", reason=previous["status"])
                return UNCERTAIN if previous["status"] in {"started", "uncertain"} else None
            key = (message.user_id, message.chat_id)
            session = self.sessions.get(key)
            expired = (
                session is not None
                and time.monotonic() - session.touched > self.settings.context_ttl_seconds
            )
            emit("session.before", session=session, expired=expired)
            if session is None or expired:
                session = Session()
                self.sessions[key] = session
            session.touched = time.monotonic()
            context = session.context(message, self.timezone)
            emit("decision.context", context=context)
            require()
            items = []
            try:
                async with asyncio.timeout(self.settings.agent_timeout_seconds):
                    recognition = RecognitionResult.model_validate(
                        await self.engine.recognize(message.text, context)
                    )
                    validate_recognition(recognition, message.text, context)
                    emit("recognition.validated", recognition=recognition)
                    require()
                    if not recognition.items:
                        self.journal.save(
                            message.message_id,
                            "success",
                            action="message",
                            response="此次不包含消费行为。",
                        )
                        emit("message.ignored", reason="no_expenses")
                        return "此次不包含消费行为。"
                    items = session.identify(message, recognition)
                    emit("recognition.identified", items=items)
                    require()
                    plan = MessagePlan.model_validate(await self.engine.decide(items, context))
                    validate_plan(plan, items)
            except TimeoutError:
                for item in items:
                    if item.type == "new":
                        session.bills[item.bill_id].feedback = "模型理解超时，未执行写入。"
                emit("decision.timeout", status="error")
                return "理解消息超时，未执行写入，请稍后重试。"
            except Exception as exc:  # noqa: BLE001 -- No invalid envelope can authorize writes.
                emit("decision.error", status="error", error=exc)
                require()
                for item in items:
                    if item.type == "new":
                        session.bills[item.bill_id].feedback = "模型输出无效，未执行写入。"
                if budget_exceeded(exc):
                    return "本条消息的模型调用次数已达上限，本次未执行写入。"
                if isinstance(exc, (UnexpectedModelBehavior, ValidationError, ValueError)):
                    return "消息理解失败：模型返回的记账信息不完整或格式不正确，本次未执行写入。"
                return "消息理解失败，未执行写入，请检查模型配置和网络连接。"
            emit("message.plan", plan=plan)
            require()
            try:
                self.journal.save(message.message_id, "started", action="message")
            except OSError:
                return "无法保存去重状态，未执行写入，请检查本地文件权限。"
            touched, notes = set(), []
            emit("batch.started", total=len(items))
            try:
                await self._execute(session, message, plan, items, touched)
            except BaseException:
                response, counts = render(
                    session,
                    touched,
                    ["处理已中断，请核对以下结果。"],
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
            emit(
                "message.completed",
                response=response,
                counts=counts,
                attention=bool(counts["failed"] or counts["uncertain"]),
                visible=len(touched),
            )
            current_trace().response = response
            return response

    async def _execute(self, session, message, plan, items, touched):
        decisions = {decision.bill_id: decision for decision in plan.decisions}
        creates, updates = [], []
        # Prepare every outcome before I/O so interruption preserves all known statuses.
        for item in items:
            decision = decisions[item.bill_id]
            bill = session.bills[item.bill_id]
            touched.add(bill.bill_id)
            emit("decision.branch", decision=decision)
            require()
            if bill.status == "uncertain" or self._reserved(bill):
                bill.status, bill.feedback = "uncertain", UNCERTAIN
                continue
            bill.text, bill.action = decision.raw, decision.action
            if decision.action == "clarify":
                bill.status, bill.feedback = "clarified", decision.reason
                bill.facts = bill.confirmed.model_dump(mode="json") if bill.confirmed else {}
                emit("business.clarify", reason=decision.reason, bill_id=bill.bill_id)
                continue
            bill.status, bill.feedback = "failed", "尚未执行写入。"
            if decision.action == "update" and bill.confirmed is None:
                bill.feedback = "目标账单尚未成功写入，不能更新。"
                continue
            patch = decision.expense.model_dump(mode="json", exclude_none=True)
            if decision.action == "update":
                if not patch:
                    bill.feedback = "未提供需要修改的字段，本次未更新。"
                    continue
                facts = {**bill.confirmed.model_dump(mode="json"), **patch}
            else:
                facts = decision.expense.model_dump(mode="json")
            expense = self._validate(facts, message, bill)
            if expense is None:
                continue
            bill.facts = expense.model_dump(mode="json")
            # Window IDs reset; operation identity must remain message-scoped for durable dedup.
            operation_id = f"{message.message_id}/bill/{bill.bill_id}/{decision.action}"
            bill.operation_id = operation_id
            (creates if decision.action == "create" else updates).append(
                (bill, expense, operation_id)
            )
        if not creates and not updates:
            return
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
            except Exception as exc:  # noqa: BLE001 -- Read failures never authorize writes.
                emit("table.preflight_failed", status="error", error=exc)
                require()
                for bill, _, _ in [*creates, *updates]:
                    bill.feedback = (
                        str(exc)
                        if isinstance(exc, (QueryError, KnowledgeError))
                        else "表格检查失败，本次未执行这笔操作。"
                    )
                return
            await self._create(session, creates)
            for entry in updates:
                await self._update(session, entry)

    def _reserved(self, bill):
        entry = self.journal.get(bill.operation_id) if bill.operation_id else None
        return bool(entry and entry["status"] in {"started", "uncertain"})

    def _validate(self, facts, message, bill):
        emit("expense.merged", values=facts, action=bill.action)
        require()
        try:
            expense = Expense.model_validate(facts)
            self.knowledge.validate_category(expense.category)
            if expense.date > message.sent_at.astimezone(self.timezone).date():
                raise KnowledgeError("消费日期在未来，本次未写入。")
        except KnowledgeError as exc:
            bill.feedback = str(exc)
            emit("expense.validation_failed", status="error", error=exc)
            return None
        except ValidationError as exc:
            labels = {
                "amount": "有效金额（正数，最多两位小数）",
                "description": "消费内容",
                "category": "合法分类",
                "date": "有效消费日期",
            }
            missing = list(dict.fromkeys(labels.get(e["loc"][0], "账单信息") for e in exc.errors()))
            bill.feedback = "账单缺少或包含无效信息：" + "、".join(missing) + "。本次未写入。"
            emit("expense.validation_failed", status="error", error=exc)
            return None
        emit("expense.validated", expense=expense)
        require()
        return expense

    def _confirm(self, session, bill, expense, operation_id, record_id):
        bill.confirmed, bill.record_id = expense, record_id
        bill.facts = expense.model_dump(mode="json")
        bill.status = "success"
        bill.notice = ""
        action = bill.action
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
            bill.notice = "表格写入已成功，但本地去重状态保存失败；请勿重复发送这笔消费。"
            bill.feedback += "。" + bill.notice
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
