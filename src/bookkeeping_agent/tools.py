"""Only this module can invoke lark-cli; its command is always read-only."""

import asyncio
import json
import logging
import shutil
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from .config import Settings

logger = logging.getLogger(__name__)
RecordCount = Annotated[int, Field(strict=True, ge=1, le=100)]
COUNT = TypeAdapter(RecordCount)


class QueryError(Exception):
    """A safe message suitable for returning to the user."""


class ExpenseRecord(BaseModel):
    record_id: str = Field(min_length=1)
    date: datetime
    amount: Decimal | None = Field(default=None, allow_inf_nan=False)
    description: str | None = None


class BitableQueryTool:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def recent_expenses(self, n: int) -> list[ExpenseRecord]:
        n = COUNT.validate_python(n)
        s = self.settings
        executable = shutil.which(s.lark_cli_path)
        if executable is None:
            raise QueryError("无法启动 lark-cli，请检查安装位置和执行权限。")
        executable = str(Path(executable).resolve())
        # Both the records and CLI-generated manifest are removed on every exit path.
        with TemporaryDirectory(prefix="bookkeeping-") as directory:
            output = Path(directory) / "records.ndjson"
            args = [
                executable,
                "base",
                "+record-list",
                "--base-token",
                s.bitable_base_token,
                "--table-id",
                s.bitable_table_id,
                "--as",
                s.lark_cli_identity,
                "--sort-json",
                json.dumps([{"field": s.bitable_date_field, "desc": True}]),
                "--limit",
                str(n),
                "--format",
                "ndjson",
                "--output",
                output.name,
            ]
            if s.bitable_view_id:
                # --filter-json replaces view filters; preserve the configured scope.
                args.extend(["--view-id", s.bitable_view_id])
            else:
                args.extend(
                    [
                        "--filter-json",
                        json.dumps(
                            {"logic": "and", "conditions": [[s.bitable_date_field, "non_empty"]]}
                        ),
                    ]
                )
            for field in (
                s.bitable_date_field,
                s.bitable_amount_field,
                s.bitable_description_field,
            ):
                args.extend(["--field-id", field])
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=directory,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise QueryError("无法启动 lark-cli，请检查安装位置和执行权限。") from exc
            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(), timeout=s.cli_timeout_seconds
                )
            except (TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise QueryError("多维表格查询超时，请稍后重试。") from exc
            if process.returncode:
                logger.warning("cli_failed exit_code=%s", process.returncode)
                raise QueryError("多维表格查询失败，请检查 CLI 登录态、表格标识和读取权限。")
            try:
                summary = json.loads(stdout)
                if not isinstance(summary, dict) or summary.get("ok") is False:
                    raise ValueError("CLI reported failure")
                rows = [
                    json.loads(line, parse_float=Decimal)
                    for line in output.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if len(rows) > n:
                    raise ValueError("CLI exceeded requested limit")
                records = []
                for row in rows:
                    if s.bitable_view_id and row.get(s.bitable_date_field) is None:
                        raise QueryError(
                            "指定视图返回了空记账日期，无法确定最近记录。"
                            "请在该视图中设置记账日期非空的筛选条件后重试。"
                        )
                    record = ExpenseRecord(
                        record_id=row["record_id"],
                        date=row[s.bitable_date_field],
                        amount=row[s.bitable_amount_field],
                        description=row[s.bitable_description_field],
                    )
                    if record.date.tzinfo is None:
                        raise ValueError("CLI date must have timezone offset")
                    records.append(record)
                # Remote sorting selects the top N within the configured table/view scope.
                return sorted(records, key=lambda record: record.date, reverse=True)
            except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
                raise QueryError(
                    "查询返回的数据格式异常，请检查字段名称、日期和金额类型。"
                ) from exc
