"""Session isolation, business validation, deduplication and deterministic feedback."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from pydantic_ai.models import Model

from .agent import DecisionEngine
from .config import Settings
from .knowledge import Knowledge, KnowledgeError
from .models import Decision, Expense, IncomingMessage
from .storage import OperationJournal
from .tools import BitableTool, QueryError

UNCERTAIN = "上次写入结果待核对，请先检查表格；本次未再次执行记账。"
logger = logging.getLogger(__name__)


@dataclass
class Session:
    last_record_id: str | None = None
    last_expense: Expense | None = None
    pending: dict | None = None
    turns: list[dict] = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)
    uncertain: bool = False


class BookkeepingService:
    def __init__(
        self, settings: Settings, tool: BitableTool, model: Model | None = None, *, engine=None
    ):
        self.settings = settings
        self.tool = tool
        self.knowledge = Knowledge(settings)
        self.engine = engine or DecisionEngine(settings, tool, self.knowledge, model)
        self.journal = OperationJournal(
            settings.operation_journal_path,
            f"{settings.bitable_base_token}/{settings.bitable_table_id}",
        )
        self.sessions: dict[tuple[str, str], Session] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def reply(self, message: IncomingMessage) -> str | None:
        # Queue the entire operation, not just the CLI call. SDK queue is an additional layer.
        lock = self.locks.setdefault(message.chat_id, asyncio.Lock())
        async with lock:
            previous = self.journal.get(message.message_id)
            if previous:
                return UNCERTAIN if previous["status"] in {"started", "uncertain"} else None
            key = (message.user_id, message.chat_id)
            session = self.sessions.get(key)
            if (
                session is None
                or time.monotonic() - session.touched > self.settings.context_ttl_seconds
            ):
                session = Session()
                self.sessions[key] = session
            session.touched = time.monotonic()
            original_time = (
                datetime.fromisoformat(session.pending["sent_at"])
                if session.pending
                else message.sent_at
            )
            context = {
                "message_sent_at": message.sent_at.astimezone(
                    ZoneInfo(self.settings.display_timezone)
                ).isoformat(),
                "timezone": self.settings.display_timezone,
                "message_date": message.sent_at.astimezone(ZoneInfo(self.settings.display_timezone))
                .date()
                .isoformat(),
                "default_date": message.sent_at.astimezone(ZoneInfo(self.settings.display_timezone))
                .date()
                .isoformat(),
                "relative_date_base": message.sent_at.astimezone(
                    ZoneInfo(self.settings.display_timezone)
                ).isoformat(),
                "pending_default_date": original_time.astimezone(
                    ZoneInfo(self.settings.display_timezone)
                )
                .date()
                .isoformat()
                if session.pending
                else None,
                "pending_relative_date_base": original_time.astimezone(
                    ZoneInfo(self.settings.display_timezone)
                ).isoformat()
                if session.pending
                else None,
                "pending": session.pending,
                "last_record": {
                    "record_id": session.last_record_id,
                    "expense": session.last_expense.model_dump(mode="json"),
                }
                if session.last_expense
                else None,
                "recent_turns": session.turns,
            }
            try:
                decision = await asyncio.wait_for(
                    self.engine.decide(message.text, context),
                    self.settings.agent_timeout_seconds,
                )
            except TimeoutError:
                return "理解消息超时，未执行写入，请稍后重试。"
            except Exception as exc:  # noqa: BLE001 -- Provider messages may contain secrets.
                logger.warning("agent_failed kind=%s", type(exc).__name__)
                return "消息理解失败，未执行写入，请检查模型配置和网络连接。"
            if decision.action == "ignore":
                return None
            if not decision.continues_pending:
                session.pending = None
            elif session.pending:
                context["default_date"] = context["pending_default_date"]
            if decision.multiple_expenses:
                return self._ask(
                    session, message, decision, "当前一次只处理一笔消费，请将多笔消费分别发送。"
                )
            if decision.action == "clarify" or decision.unresolved or decision.question:
                return self._ask(
                    session,
                    message,
                    decision,
                    decision.question or "请补充：" + "、".join(decision.unresolved),
                )
            if decision.action == "preference":
                return await self._preference(session, message, decision)
            return await self._expense(session, message, decision, context["default_date"])

    def _ask(
        self, session: Session, message: IncomingMessage, decision: Decision, question: str
    ) -> str:
        # Keep all facts in the bounded dialogue, and preserve the original date anchor.
        if session.pending is None:
            session.pending = {
                "text": message.text,
                "sent_at": message.sent_at.isoformat(),
                "action": decision.action,
            }
        if decision.expense:
            session.pending["expense"] = {
                **session.pending.get("expense", {}),
                **decision.expense.model_dump(mode="json", exclude_none=True),
            }
        session.turns.extend([{"user": message.text}, {"question": question}])
        session.turns[:] = session.turns[-6:]
        return question

    def _done(self, session: Session, message: IncomingMessage, response: str):
        session.pending = None
        session.turns.extend([{"user": message.text}, {"result": response}])
        session.turns[:] = session.turns[-6:]

    async def _preference(
        self, session: Session, message: IncomingMessage, decision: Decision
    ) -> str:
        preference = decision.preference
        source = message.text + "\n" + (session.pending["text"] if session.pending else "")
        # Evidence must be a verbatim user statement of an ongoing preference.
        if preference.evidence not in source or not any(
            marker in preference.evidence
            for marker in ("以后", "今后", "始终", "每次", "一律", "都归", "总是")
        ):
            return self._ask(
                session, message, decision, "这是只修改本次账单，还是以后都按这个分类记账？"
            )
        try:
            async with self.tool.write_lock:
                self.knowledge.validate_category(preference.category)
                self.journal.save(message.message_id, "started", action="preference")
                self.knowledge.update_preference(preference)
                self.journal.save(message.message_id, "success", action="preference")
        except KnowledgeError as exc:
            return self._ask(session, message, decision, str(exc))
        except OSError:
            return "分类偏好保存未确认成功，请检查本地文件权限；本次未修改账单。"
        response = (
            f"已保存分类偏好：{preference.condition} → {category_label(preference.category)}。"
        )
        self._done(session, message, response)
        return response

    async def _expense(
        self, session: Session, message: IncomingMessage, decision: Decision, default_date: str
    ) -> str:
        patch = decision.expense.model_dump(exclude_none=True)
        if decision.continues_pending and session.pending:
            patch = {**session.pending.get("expense", {}), **patch}
        if decision.action == "update":
            if session.uncertain:
                return UNCERTAIN
            if not session.last_record_id or not session.last_expense:
                return self._ask(
                    session,
                    message,
                    decision,
                    "当前会话没有可纠错的最近账单，请提供完整消费信息重新记账。",
                )
            if decision.record_id and decision.record_id != session.last_record_id:
                return self._ask(
                    session,
                    message,
                    decision,
                    "只能纠正当前会话最近成功记入的账单，请明确是否修改这笔。",
                )
            if not patch:
                return self._ask(session, message, decision, "请说明需要修改的金额、日期或分类。")
            values = session.last_expense.model_dump()
            values.update(patch)
        else:
            values = {"date": default_date, **patch}
        try:
            expense = Expense.model_validate(values)
            self.knowledge.validate_category(expense.category)
            if (
                expense.date
                > message.sent_at.astimezone(ZoneInfo(self.settings.display_timezone)).date()
            ):
                raise KnowledgeError("消费日期在未来，请确认实际消费日期。")
        except KnowledgeError as exc:
            return self._ask(session, message, decision, str(exc))
        except ValidationError as exc:
            labels = {
                "amount": "有效金额（正数，最多两位小数）",
                "description": "消费内容",
                "category": "合法分类",
                "date": "有效消费日期",
            }
            missing = list(dict.fromkeys(labels.get(e["loc"][0], "账单信息") for e in exc.errors()))
            return self._ask(
                session, message, decision, "请补充或确认：" + "、".join(missing) + "。"
            )
        record_id = session.last_record_id if decision.action == "update" else None
        async with self.tool.write_lock:
            # Failures here are read-only: no journal reservation or mutation yet.
            try:
                self.knowledge.validate_schema(await self.tool.field_schema())
                if record_id:
                    actual = await self.tool.get_record(record_id)
                    original = self.tool.expense_fields(session.last_expense)
                    # Never overwrite data edited outside this conversation.
                    for name in self.tool.expense_fields(expense):
                        if not same_field(name, actual.get(name), original[name], self.settings):
                            raise QueryError("最近账单已被外部修改，请核对表格后再纠错。")
            except (QueryError, KnowledgeError) as exc:
                return str(exc)
            try:
                self.journal.save(message.message_id, "started", action=decision.action)
            except OSError:
                return "无法保存去重状态，未执行写入，请检查本地文件权限。"
            try:
                if record_id:
                    fields = self.tool.expense_fields(expense)
                    before = self.tool.expense_fields(session.last_expense)
                    changed = {
                        name: value for name, value in fields.items() if value != before[name]
                    }
                    if changed:
                        await self.tool.update_record(record_id, changed)
                else:
                    record_id = await self.tool.create_record(expense)
            except asyncio.CancelledError:
                # Reservation remains on disk even if the process exits before cleanup.
                session.uncertain = True
                raise
            except Exception as exc:  # noqa: BLE001 -- Every post-reservation failure is uncertain.
                session.uncertain = True
                return str(exc) if isinstance(exc, QueryError) else UNCERTAIN
            # If saving fails after remote success, the durable 'started' prevents another create.
            response = (
                f"{'已修改' if decision.action == 'update' else '已记账'}："
                f"{expense.date.isoformat()} | {expense.amount:.2f} 元 | "
                f"{expense.description} | {category_label(expense.category)}。"
            )
            try:
                self.journal.save(
                    message.message_id, "success", action=decision.action, record_id=record_id
                )
            except OSError:
                return "表格写入已成功，但本地去重状态保存失败；请勿重复发送这笔消费。"
            session.last_record_id = record_id
            session.last_expense = expense
            session.uncertain = False
            self._done(session, message, response)
            return response


def category_label(category) -> str:
    return " / ".join(filter(None, (category.level1, category.level2, category.level3)))


def same_field(name, actual, expected, settings: Settings) -> bool:
    try:
        if name == settings.bitable_date_field:
            return datetime.fromisoformat(actual) == datetime.fromisoformat(expected)
        if name == settings.bitable_amount_field:
            return Decimal(str(actual)) == expected
        return actual == expected or (expected is None and actual == [])
    except (ValueError, TypeError):
        return False
