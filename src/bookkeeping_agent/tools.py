"""The only CLI boundary: fixed commands, async subprocesses and verified results."""

import asyncio
import json
import logging
import shutil
import time as monotonic_time
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from .config import Settings
from .models import Expense
from .tracing import current_trace, emit, require

logger = logging.getLogger(__name__)
RecordCount = Annotated[int, Field(strict=True, ge=1, le=100)]
COUNT = TypeAdapter(RecordCount)


class QueryError(Exception):
    """Safe read/preflight failure, suitable for returning to the user."""


class WriteUncertain(QueryError):
    """A mutation may have reached Feishu; never automatically repeat it."""


class ExpenseRecord(BaseModel):
    record_id: str = Field(min_length=1)
    date: datetime
    amount: Decimal | None = Field(default=None, allow_inf_nan=False)
    description: str | None = None


def exact_json(value: object) -> str:
    """Emit Decimal as a JSON number without a binary-float conversion."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + exact_json(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(exact_json(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class BitableTool:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.write_lock = asyncio.Lock()

    async def _run(self, command: str, args: list[str], directory: str, *, write=False) -> dict:
        require(reconciliation=not write)
        s = self.settings
        executable = shutil.which(s.lark_cli_path)
        if executable is None:
            raise QueryError("无法启动 lark-cli，请检查安装位置和执行权限。")
        argv = [
            str(Path(executable).resolve()),
            "base",
            command,
            "--base-token",
            s.bitable_base_token,
            "--table-id",
            s.bitable_table_id,
            "--as",
            s.lark_cli_identity,
            *args,
        ]
        emit("cli.start", argv=argv, directory=directory, write=write, durable=write)
        require(reconciliation=not write)
        started = monotonic_time.monotonic()
        trace = current_trace()
        already_submitted = trace.mutation_submitted if trace else False
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=directory,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            if write and trace:
                trace.mutation_submitted = True
        except OSError as exc:
            if trace:
                trace.mutation_submitted = already_submitted
            emit("cli.spawn_error", status="error", error=exc)
            raise QueryError("无法启动 lark-cli，请检查安装位置和执行权限。") from exc
        communication = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication), s.cli_timeout_seconds
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            stdout, stderr = await communication
            emit(
                "cli.interrupted",
                status="error",
                error=exc,
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode,
                duration_ms=(monotonic_time.monotonic() - started) * 1000,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            if write:
                raise WriteUncertain(
                    "写入超时，结果待核对；请先检查表格，勿重复发送记账。"
                ) from exc
            raise QueryError("多维表格查询超时，请稍后重试。") from exc
        emit(
            "cli.result",
            stdout=stdout,
            stderr=stderr,
            exit_code=process.returncode,
            duration_ms=(monotonic_time.monotonic() - started) * 1000,
        )
        require(reconciliation=not write or bool(trace and trace.mutation_submitted))
        if process.returncode:
            logger.warning("cli_failed command=%s exit_code=%s", command, process.returncode)
            if write:
                raise WriteUncertain("写入未确认成功，请检查 CLI 登录态、权限和表格；勿重复记账。")
            raise QueryError("多维表格查询失败，请检查 CLI 登录态、表格标识和读取权限。")
        try:
            summary = json.loads(stdout)
            if not isinstance(summary, dict) or summary.get("ok") is not True:
                raise ValueError("CLI did not confirm success")
            emit("cli.parsed", summary=summary)
            return summary
        except (ValueError, TypeError) as exc:
            if write:
                raise WriteUncertain(
                    "写入返回异常，结果待核对；请先检查表格，勿重复记账。"
                ) from exc
            raise QueryError("查询返回的数据格式异常，请检查 CLI 版本。") from exc

    def projected_fields(self, *, categories=False) -> list[str]:
        s = self.settings
        fields = [s.bitable_date_field, s.bitable_amount_field, s.bitable_description_field]
        if categories:
            fields.extend(s.category_fields)
        return [item for name in fields for item in ("--field-id", name)]

    async def _rows(self, command: str, args: list[str], *, categories=False) -> list[dict]:
        with TemporaryDirectory(prefix="bookkeeping-") as directory:
            output = Path(directory) / "records.ndjson"
            try:
                await self._run(
                    command,
                    [
                        *args,
                        *self.projected_fields(categories=categories),
                        "--format",
                        "ndjson",
                        "--output",
                        output.name,
                    ],
                    directory,
                )
            finally:
                if output.exists():
                    try:
                        emit("cli.output_file", filename=output.name, content=output.read_bytes())
                    except OSError as exc:
                        emit("cli.output_file_error", status="error", error=exc)
            try:
                contents = output.read_text(encoding="utf-8")
                require(reconciliation=True)
                return [
                    json.loads(line, parse_float=Decimal)
                    for line in contents.splitlines()
                    if line.strip()
                ]
            except (OSError, ValueError, TypeError) as exc:
                raise QueryError("查询返回的数据格式异常，请检查 CLI 输出文件。") from exc

    async def recent_expenses(self, n: int) -> list[ExpenseRecord]:
        n = COUNT.validate_python(n)
        s = self.settings
        args = [
            "--sort-json",
            json.dumps([{"field": s.bitable_date_field, "desc": True}]),
            "--limit",
            str(n),
        ]
        if s.bitable_view_id:
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
        rows = await self._rows("+record-list", args)
        try:
            if len(rows) > n:
                raise ValueError("too many records")
            records = []
            for row in rows:
                if s.bitable_view_id and row.get(s.bitable_date_field) is None:
                    raise QueryError("指定视图返回了空记账日期，请设置日期非空筛选后重试。")
                record = ExpenseRecord(
                    record_id=row["record_id"],
                    date=row[s.bitable_date_field],
                    amount=row[s.bitable_amount_field],
                    description=row[s.bitable_description_field],
                )
                if record.date.tzinfo is None:
                    raise ValueError("timezone missing")
                records.append(record)
            return sorted(records, key=lambda record: record.date, reverse=True)
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            raise QueryError("查询返回的数据格式异常，请检查字段名称、日期和金额类型。") from exc

    async def field_schema(self) -> list[dict]:
        with TemporaryDirectory(prefix="bookkeeping-") as directory:
            result = await self._run("+field-list", ["--limit", "200"], directory)
            try:
                data = result["data"]
                if data.get("has_more"):
                    raise ValueError("incomplete schema")
                fields = data["fields"]
                if not isinstance(fields, list):
                    raise TypeError("invalid fields")
                if data.get("total", len(fields)) != len(fields):
                    raise ValueError("incomplete schema")
                for field in fields:
                    if field.get("truncated") and field["type"] == "select":
                        options = await self._run(
                            "+field-search-options",
                            [
                                "--field-id",
                                field["id"],
                                "--limit",
                                "200",
                                "--offset",
                                "0",
                            ],
                            directory,
                        )
                        details = options["data"]
                        if details.get("has_more") or details.get("truncated"):
                            raise ValueError("incomplete options")
                        if details.get("total", len(details["options"])) != len(details["options"]):
                            raise ValueError("incomplete options")
                        field["options"] = details["options"]
                        field.pop("truncated", None)
                return fields
            except (KeyError, TypeError, AttributeError, ValueError) as exc:
                raise QueryError("无法确认远端字段结构，不执行写入。") from exc

    async def history(self, keyword: str, n: int = 10) -> list[dict]:
        n = COUNT.validate_python(n)
        keyword = keyword.strip()
        if not keyword or len(keyword) > 100:
            raise ValueError("invalid history keyword")
        s = self.settings
        # Personal habits belong to the entire table, not the demo's selected view.
        return await self._rows(
            "+record-list",
            [
                "--filter-json",
                json.dumps(
                    {
                        "logic": "and",
                        "conditions": [[s.bitable_description_field, "intersects", keyword]],
                    },
                    ensure_ascii=False,
                ),
                "--sort-json",
                json.dumps([{"field": s.bitable_date_field, "desc": True}]),
                "--limit",
                str(n),
            ],
            categories=True,
        )

    async def get_record(self, record_id: str) -> dict:
        rows = await self._rows("+record-get", ["--record-id", record_id], categories=True)
        if len(rows) != 1 or rows[0].get("record_id") != record_id:
            raise QueryError("最近账单不存在或无法读取，不执行纠错。")
        return rows[0]

    def expense_fields(self, expense: Expense) -> dict:
        s = self.settings
        c = expense.category
        moment = datetime.combine(expense.date, time.min, ZoneInfo(s.display_timezone))
        return {
            s.bitable_date_field: moment.isoformat(),
            s.bitable_amount_field: expense.amount,
            s.bitable_description_field: expense.description,
            s.bitable_category1_field: [c.level1],
            s.bitable_category2_field: [c.level2],
            s.bitable_category3_field: [c.level3] if c.level3 else None,
        }

    async def _write(self, command: str, payload: dict) -> dict:
        emit("cli.write_payload", command=command, payload=payload, body=exact_json(payload))
        require()
        with TemporaryDirectory(prefix="bookkeeping-") as directory:
            path = Path(directory) / "payload.json"
            path.write_text(exact_json(payload), encoding="utf-8")
            result = await self._run(command, ["--json", "@payload.json"], directory, write=True)
            data = result.get("data")
            if not isinstance(data, dict) or data.get("ignored_fields"):
                raise WriteUncertain("写入字段未被完整接受，结果待核对；请检查表格。")
            return data

    async def create_record(self, expense: Expense) -> str:
        data = await self._write(
            "+record-batch-create",
            {
                "create_records": [self.expense_fields(expense)],
            },
        )
        ids = data.get("record_id_list")
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or not ids[0]:
            raise WriteUncertain("未取得唯一账单 ID，写入结果待核对；请先检查表格。")
        return ids[0]

    async def update_record(self, record_id: str, fields: dict) -> None:
        await self._write("+record-batch-update", {"update_records": {record_id: fields}})
        try:
            actual = await self.get_record(record_id)
            emit("table.update_verification", record_id=record_id, expected=fields, actual=actual)
            for name, expected in fields.items():
                value = actual.get(name)
                if name == self.settings.bitable_date_field:
                    matches = datetime.fromisoformat(value) == datetime.fromisoformat(expected)
                elif name == self.settings.bitable_amount_field:
                    matches = Decimal(str(value)) == expected
                else:
                    matches = value == expected or (expected is None and value == [])
                if not matches:
                    raise ValueError("update not visible")
        except (QueryError, ValueError, TypeError) as exc:
            raise WriteUncertain("纠错已提交，但尚未核对到完整修改结果；请检查表格。") from exc


# Preserve the demo's direct read diagnostic; it is not exposed to the Agent.
BitableQueryTool = BitableTool
