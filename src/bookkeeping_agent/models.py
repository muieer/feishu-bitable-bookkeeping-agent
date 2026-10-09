"""Structured understanding is separate from permission to execute."""

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
    date: ExpenseDate | None = None
    amount: Money | None = None
    description: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    category: CategoryPath | None = None

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
    evidence: Annotated[str, Field(min_length=1, max_length=500)] = Field(
        description="用户明确表达长期偏好的原文片段；不得由单次纠错推断。"
    )


class Decision(StrictModel):
    action: Literal["ignore", "create", "update", "preference", "clarify"] = Field(
        description="计划、价格讨论、查询统计、闲聊均为 ignore；实际消费才可 create。"
    )
    expense: ExpensePatch | None = None
    record_id: str | None = Field(default=None, description="仅可修改本会话最近成功账单。")
    preference: Preference | None = None
    question: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    unresolved: list[str] = Field(default_factory=list)
    needs_history: bool = Field(default=False, description="已确认记账意图后，是否需要历史案例。")
    multiple_expenses: bool = Field(default=False, description="多笔独立消费禁止合并或只记一笔。")
    continues_pending: bool = Field(
        default=False, description="本条消息是否在回答已有追问。新消费为 false。"
    )

    @model_validator(mode="after")
    def coherent_action(self):
        if self.action == "ignore" and (
            self.expense or self.preference or self.question or self.needs_history or self.record_id
        ):
            raise ValueError("ignore cannot carry operations")
        if self.action in {"create", "update"} and self.expense is None:
            raise ValueError("expense is required")
        if self.action == "clarify" and self.question is None:
            raise ValueError("question is required")
        if self.action == "preference" and self.preference is None:
            raise ValueError("preference is required")
        if self.action == "preference" and self.expense is not None:
            raise ValueError("mixed expense and preference operation")
        if self.action != "preference" and self.preference is not None:
            raise ValueError("mixed preference operation")
        if self.action != "update" and self.record_id is not None:
            raise ValueError("record target only applies to updates")
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
