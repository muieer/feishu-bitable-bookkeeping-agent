"""Model understanding is a plan, never permission to mutate the ledger."""

from datetime import date as ExpenseDate
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Money = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)]
Label = Annotated[str, Field(min_length=1, max_length=200)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CategoryPath(StrictModel):
    level1: Label
    level2: Label
    level3: Label | None = None


class ExpensePatch(StrictModel):
    date: ExpenseDate | None = Field(description="消费日期；未知或本次未要求修改时为 null。")
    # Business validity is checked per bill, so one bad amount cannot discard other bills.
    amount: Decimal | None = Field(
        allow_inf_nan=False, description="实际支付金额；未知或本次未要求修改时为 null。"
    )
    description: Annotated[str, Field(min_length=1, max_length=500)] | None = Field(
        description="消费描述；未知或本次未要求修改时为 null。"
    )
    category: CategoryPath | None = Field(
        description="合法完整分类路径；尚未确定或本次未要求修改时为 null。"
    )

    @field_validator("amount", mode="before")
    @classmethod
    def reject_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("boolean is not money")  # noqa: TRY004 -- Pydantic validation error.
        return value


class Expense(ExpensePatch):
    date: ExpenseDate
    amount: Money
    description: Annotated[str, Field(min_length=1, max_length=500)]
    category: CategoryPath


class Preference(StrictModel):
    subject: Annotated[str, Field(min_length=1, max_length=100)]
    condition: Annotated[str, Field(min_length=1, max_length=500)]
    category: CategoryPath
    evidence: Annotated[str, Field(min_length=1, max_length=500)]


class Decision(StrictModel):
    decision_id: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[\w-]+$")]
    source_text: Annotated[str, Field(min_length=1)] = Field(
        description="本消息中表达这一独立意图的连续原文片段，不重写，不按标点拆交易。"
    )
    source_occurrence: Annotated[int, Field(gt=0, strict=True)] = Field(
        default=1, description="相同原文片段在消息中第几次出现，区分真实的相同消费。"
    )
    action: Literal["ignore", "create", "update", "preference", "clarify"]
    expense: ExpensePatch | None = Field(
        description="必填。create/update 必须提供消费对象及四个字段；clarify 保留已知事实；ignore/preference 为 null。"
    )
    preference: Preference | None = Field(
        description="必填。preference 必须提供长期偏好对象；其他操作为 null。"
    )
    question: Annotated[str, Field(min_length=1, max_length=500)] | None = Field(
        description="必填。clarify 必须提供具体问题；无需追问时为 null。"
    )
    unresolved: list[str] = Field(default_factory=list)
    needs_history: bool = False
    history_keywords: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=2
    )
    continues_pending: bool = Field(description="回答待补充账单为 true；新消费为 false。")
    target_id: Label | None = Field(
        description="自然语言指向上下文中的稳定 bill_id；新消费为 null。"
    )
    target_reference: Label | None = Field(
        default=None, description="自然语言目标的原文，如晚饭；须唯一匹配已有账单描述或原文。"
    )
    target_number: Annotated[int, Field(gt=0, strict=True)] | None = Field(
        description="仅指向最新最终回复 displayed_items 的编号；没有编号时为 null。"
    )
    clarification_scope: Literal["expense", "target"] = "expense"
    cancel_pending: bool = Field(default=False, description="明确取消未完成账单，用 ignore。")

    @model_validator(mode="after")
    def coherent_action(self):
        if self.action == "ignore" and (
            self.expense or self.preference or self.question or self.needs_history
        ):
            raise ValueError("ignore cannot carry business operations")
        if self.action in {"create", "update"} and self.expense is None:
            raise ValueError(
                "create/update 的 expense 不能为 null；必须返回 date、amount、description、category，未知或未修改字段为 null。"
            )
        if self.action == "clarify" and self.question is None:
            raise ValueError("question is required")
        if (self.action == "preference") != (self.preference is not None):
            raise ValueError("preference only belongs to a preference decision")
        if self.action == "preference" and (
            self.expense or self.target_id or self.target_number or self.continues_pending
        ):
            raise ValueError("preference cannot implicitly modify a bill")
        if self.cancel_pending and self.action != "ignore":
            raise ValueError("cancellation uses ignore")
        if self.needs_history and (
            self.action not in {"create", "update", "clarify"}
            or self.clarification_scope == "target"
            or not self.history_keywords
        ):
            raise ValueError("history requires an established expense and keywords")
        return self


class MessagePlan(StrictModel):
    decisions: list[Decision] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [d.decision_id for d in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("decision IDs must be unique within the message")
        return self


class IncomingMessage(StrictModel):
    message_id: Label
    user_id: Label
    chat_id: Label
    sent_at: datetime
    text: Annotated[str, Field(min_length=1)]

    @field_validator("sent_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("message time must have timezone")
        return value
