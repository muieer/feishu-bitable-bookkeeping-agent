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
    bitable_date_field: str = Field(default="记账日期", min_length=1)
    bitable_amount_field: str = Field(default="金额", min_length=1)
    bitable_description_field: str = Field(default="具体", min_length=1)
    lark_cli_path: str = Field(default="lark-cli", min_length=1)
    lark_cli_identity: Literal["user", "bot"] = "user"
    cli_timeout_seconds: float = Field(default=30, gt=0)
    agent_timeout_seconds: float = Field(default=90, gt=0)
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
