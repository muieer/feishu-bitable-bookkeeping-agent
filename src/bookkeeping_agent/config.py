from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    lark_app_id: str = Field(min_length=1)
    lark_app_secret: SecretStr = Field(min_length=1)
    ark_api_key: SecretStr = Field(min_length=1)
    ark_model: str = Field(min_length=1)
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    bitable_base_token: str = Field(min_length=1)
    bitable_table_id: str = Field(min_length=1)
    bitable_view_id: str = ""
    test_mode: bool = False
    bitable_date_field: str = Field(default="记账日期", min_length=1)
    bitable_amount_field: str = Field(default="金额", min_length=1)
    bitable_description_field: str = Field(default="具体", min_length=1)
    bitable_category1_field: str = "一级分类"
    bitable_category2_field: str = "二级分类"
    bitable_category3_field: str = "三级分类"
    recognition_prompt_path: Path = Path("prompts/recognition.md")
    decision_prompt_path: Path = Path("prompts/decision.md")
    categories_path: Path = Path("knowledge/categories.json")
    rules_path: Path = Path("knowledge/rules.json")
    schema_path: Path = Path("knowledge/schema.json")
    operation_journal_path: Path = Path(".runtime/operations.json")
    log_file_path: Path = Path("logs/service.log")
    message_log_dir: Path = Path("logs")
    context_ttl_seconds: float = Field(default=1800, gt=0)
    lark_cli_path: str = Field(default="lark-cli", min_length=1)
    lark_cli_identity: Literal["user", "bot"] = "user"
    cli_timeout_seconds: float = Field(default=30, gt=0)
    agent_timeout_seconds: float = Field(default=90, gt=0)
    llm_message_request_limit: int = Field(default=10, ge=1, le=10)
    llm_http_request_limit: int | None = Field(default=None, ge=0)
    llm_http_budget_path: Path = Path(".runtime/llm-http-budget.json")
    display_timezone: str = "Asia/Shanghai"

    @field_validator("bitable_view_id")
    @classmethod
    def normalize_view_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("display_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid display timezone") from exc
        return value

    @property
    def category_fields(self) -> tuple[str, str, str]:
        return (
            self.bitable_category1_field,
            self.bitable_category2_field,
            self.bitable_category3_field,
        )
