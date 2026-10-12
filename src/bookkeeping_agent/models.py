"""Separate, closed contracts for recognition, decisions and executable expenses."""

import re
from datetime import date as ExpenseDate
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BillId = Annotated[str, Field(pattern=r"^B[1-9][0-9]*$", strict=True)]
Label = Annotated[str, Field(min_length=1, max_length=200, strict=True)]
Raw = Annotated[str, Field(min_length=1, strict=True)]
Money = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, revalidate_instances="always"
    )


class NewRecognitionItem(StrictModel):
    type: Literal["new"]
    target_id: None = Field(description="新消费没有正式编号，必须为 null。")
    raw: Raw = Field(description="本次消费原文，仅规范化空白和货币符号，不推断事实。")


class ExistingRecognitionItem(StrictModel):
    type: Literal["existing"]
    target_id: BillId = Field(description="必须关联输入中已有消费的 B 编号。")
    raw: Raw = Field(description="本次修改原文，仅规范化空白和货币符号，不推断事实。")


RecognitionItem = Annotated[
    NewRecognitionItem | ExistingRecognitionItem, Field(discriminator="type")
]


class RecognitionResult(StrictModel):
    items: list[RecognitionItem] = Field(
        max_length=200, description="按消息顺序识别；无消费返回 []。"
    )


class IdentifiedItem(StrictModel):
    bill_id: BillId
    type: Literal["new", "existing"]
    raw: Raw


class CategoryPath(StrictModel):
    level1: Label
    level2: Label
    level3: Label | None = Field(description="合法三级分类；二级无子分类时必须为 null。")


class ExpensePatch(StrictModel):
    date: ExpenseDate | None = Field(description="ISO 日期；未知或本次未要求修改时为 null。")
    amount: Decimal | None = Field(
        allow_inf_nan=False,
        description="人民币实付金额；未知或未修改为 null。业务校验正数及两位小数。",
    )
    description: Annotated[str, Field(min_length=1, max_length=500, strict=True)] | None = Field(
        description="具体消费内容；未知或未修改为 null。"
    )
    category: CategoryPath | None = Field(description="完整合法分类路径；未知或未修改为 null。")

    @field_validator("amount", mode="before")
    @classmethod
    def money_type(cls, value):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal))
        ):
            raise ValueError("amount 必须为十进制金额，不能为布尔值或其他类型")
        return value

    @field_validator("date", mode="before")
    @classmethod
    def date_type(cls, value):
        if value is not None and (
            isinstance(value, datetime) or not isinstance(value, (str, ExpenseDate))
        ):
            raise ValueError("date 必须为 ISO 日期，不能为时间戳")
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            raise ValueError("date 必须严格使用 YYYY-MM-DD 格式")
        return value


class Expense(ExpensePatch):
    date: ExpenseDate
    amount: Money
    description: Annotated[str, Field(min_length=1, max_length=500, strict=True)]
    category: CategoryPath


class CreateDecision(StrictModel):
    bill_id: BillId
    action: Literal["create"]
    raw: Raw
    expense: ExpensePatch
    reason: None = Field(description="create 的 reason 必须为 null。")


class UpdateDecision(StrictModel):
    bill_id: BillId
    action: Literal["update"]
    raw: Raw
    expense: ExpensePatch
    reason: None = Field(description="update 的 reason 必须为 null。")


class ClarifyDecision(StrictModel):
    bill_id: BillId
    action: Literal["clarify"]
    raw: Raw
    expense: None = Field(description="clarify 不携带待补充账单，必须为 null。")
    reason: Annotated[str, Field(min_length=1, max_length=500, strict=True)]


Decision = Annotated[
    CreateDecision | UpdateDecision | ClarifyDecision, Field(discriminator="action")
]


class MessagePlan(StrictModel):
    decisions: list[Decision] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [d.bill_id for d in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("每个消费编号只能有一个决策")
        return self


class IncomingMessage(StrictModel):
    message_id: Label
    user_id: Label
    chat_id: Label
    sent_at: datetime
    text: Raw

    @field_validator("sent_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("message time must have timezone")
        return value
