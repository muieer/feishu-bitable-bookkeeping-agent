import json
import sys
from pathlib import Path

import pytest

from bookkeeping_agent.config import Settings


@pytest.fixture
def settings(tmp_path):
    categories = {
        "version": "1.0.0",
        "table_id": "tbl_test",
        "tree": [
            {
                "name": "饮食",
                "children": [
                    {"name": name, "children": []} for name in ["咖啡", "食品", "外卖", "堂食"]
                ],
            },
            {"name": "人情往来", "children": [{"name": "父母", "children": []}]},
            {
                "name": "交通通信",
                "children": [
                    {
                        "name": "汽车",
                        "children": [{"name": name, "children": []} for name in ["停车费", "充电"]],
                    }
                ],
            },
        ],
    }
    rules = {
        "compatible_categories_version": "1.0.0",
        "rules": [
            {"condition": "咖啡饮品", "category": ["饮食", "咖啡", None]},
            {"condition": "给父母购买", "category": ["人情往来", "父母", None]},
        ],
    }
    fields = [
        {"name": name, "type": kind}
        for name, kind in [("记账日期", "datetime"), ("金额", "number"), ("具体", "text")]
    ]
    for level, names in enumerate(
        [
            ["饮食", "人情往来", "交通通信"],
            ["咖啡", "食品", "外卖", "堂食", "父母", "汽车"],
            ["停车费", "充电"],
        ]
    ):
        fields.append(
            {
                "id": "fld" + str(level),
                "name": ["一级分类", "二级分类", "三级分类"][level],
                "type": "select",
                "multiple": False,
                "options": [{"name": name} for name in names],
            }
        )
    for name, data in [
        ("categories", categories),
        ("rules", rules),
        ("schema", {"tables": [{"id": "tbl_test", "fields": fields}]}),
    ]:
        (tmp_path / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False))
    return Settings(
        _env_file=None,
        lark_app_id="cli_test",
        lark_app_secret="test-secret",
        ark_api_key="test-key",
        ark_model="test-model",
        bitable_base_token="base_test",
        bitable_table_id="tbl_test",
        recognition_prompt_path=Path(__file__).resolve().parents[1] / "prompts/recognition.md",
        decision_prompt_path=Path(__file__).resolve().parents[1] / "prompts/decision.md",
        categories_path=tmp_path / "categories.json",
        rules_path=tmp_path / "rules.json",
        schema_path=tmp_path / "schema.json",
        operation_journal_path=tmp_path / "operations.json",
        message_log_dir=tmp_path / "logs",
        log_file_path=tmp_path / "logs/service.log",
        llm_http_request_limit=0,
        llm_http_budget_path=tmp_path / "llm-budget.json",
    )


@pytest.fixture
def fake_cli(tmp_path, settings):
    """Real subprocess and CLI artifact contract; no Feishu I/O."""
    executable = tmp_path / "lark-cli"
    capture = tmp_path / "args.json"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json
import os
from pathlib import Path
import sys
from pathlib import Path
import time

root = Path(__file__).parent
args = sys.argv[1:]
(root / "args.json").write_text(json.dumps({"args": args, "cwd": os.getcwd()}))
with (root / "calls.jsonl").open("a") as out:
    out.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\\n")
mode = os.environ.get("FAKE_CLI_MODE", "ok")
command = args[1]
assert args[0] == "base"
assert args[args.index("--as") + 1] == "user"
if mode == "timeout" or (mode == "write_timeout" and "batch" in command):
    if "batch" in command:
        (root / "write_started").touch()
    time.sleep(20)
if mode == "failure":
    print('{"ok":false,"error":{"message":"secret must not leak"}}', file=sys.stderr)
    sys.exit(1)
if mode == "false_success":
    print('{"ok":false}')
    sys.exit(0)

def success(data):
    print(json.dumps({"ok": True, "data": data}))

if command == "+field-list":
    fields = json.loads((root / "schema.json").read_text())["tables"][0]["fields"]
    if mode == "schema_drift":
        fields[1]["type"] = "text"
    if mode == "truncated":
        fields[4]["truncated"] = True
        fields[4]["options"] = fields[4]["options"][:1]
    success({"fields": fields, "total": len(fields)})
    sys.exit(0)
if command == "+field-search-options":
    fields = json.loads((root / "schema.json").read_text())["tables"][0]["fields"]
    success({"options": fields[4]["options"], "has_more": False})
    sys.exit(0)
if "batch" in command:
    if mode == "write_failure":
        print('secret must not leak', file=sys.stderr)
        sys.exit(1)
    payload = Path(args[args.index("--json") + 1][1:])
    data = json.loads(payload.read_text())
    (root / "payload-capture.json").write_text(payload.read_text())
    rows_path = root / "written.json"
    rows = json.loads(rows_path.read_text()) if rows_path.exists() else []
    if command == "+record-batch-create":
        ids = []
        for fields in data["create_records"]:
            record_id = "rec_" + str(len(rows) + 1)
            ids.append(record_id)
            rows.append({"record_id": record_id, **fields})
        rows_path.write_text(json.dumps(rows))
        if mode == "duplicate_ids":
            ids = [ids[0]] * len(ids)
        if mode == "partial_ids":
            ids = ids[:1]
        result = {"record_id_list": ids} if mode != "missing_id" else {}
        if mode == "ignored_fields":
            result["ignored_fields"] = ["金额"]
        success(result)
    else:
        for row in rows:
            if mode != "update_not_applied":
                row.update(data["update_records"].get(row["record_id"], {}))
        rows_path.write_text(json.dumps(rows))
        ids = list(data["update_records"])
        if mode == "wrong_id":
            ids = ["rec_other"]
        if mode == "empty_ids":
            ids = []
        if mode == "duplicate_ids":
            ids *= 2
        if mode == "extra_ids":
            ids.append("rec_other")
        if mode == "invalid_ids":
            ids = ids[0]
        result = {"record_id_list": ids} if mode != "missing_id" else {}
        if mode == "ignored_fields":
            result["ignored_fields"] = ["金额"]
        success(result)
    sys.exit(0)

output = Path(args[args.index("--output") + 1])
assert not output.is_absolute()
if command == "+record-get":
    rows_path = root / "written.json"
    rows = json.loads(rows_path.read_text()) if rows_path.exists() else []
    requested_ids = [args[n + 1] for n, value in enumerate(args) if value == "--record-id"]
    rows = [r for r in rows if r["record_id"] in requested_ids]
    if mode == "partial_visible":
        rows = rows[:1]
    if mode == "duplicate_rows" and rows:
        rows.append(rows[0])
    if mode == "external_edit" and rows:
        rows[0]["金额"] = 999
else:
    assert command == "+record-list"
    n = int(args[args.index("--limit") + 1])
    assert json.loads(args[args.index("--sort-json") + 1]) == [{"field": "记账日期", "desc": True}]
    if "--view-id" in args:
        assert "--filter-json" not in args
    rows = [
        {"record_id": "r1", "记账日期": "2026-10-09T00:00:00+08:00", "金额": 18.10,
         "具体": "咖啡", "一级分类": ["饮食"], "二级分类": ["咖啡"], "三级分类": None},
        {"record_id": "r2", "记账日期": "2026-10-08T00:00:00+08:00", "金额": 25.00, "具体": "午饭"},
        {"record_id": "r3", "记账日期": "2026-10-07T00:00:00+08:00", "金额": None, "具体": None},
    ][:n]
    if mode == "empty":
        rows = []
    if mode == "empty_date":
        rows[0]["记账日期"] = None
    if mode == "bad_date":
        rows[0]["记账日期"] = "not a date"
    if mode == "missing_field":
        del rows[0]["金额"]
    if mode == "bad_amount":
        rows[0]["金额"] = "NaN"
if mode != "missing_file":
    output.write_text("\\n".join(json.dumps(row, ensure_ascii=False) for row in rows))
manifest = {
    "manifest_version": "v1", "format": "ndjson",
    "base_token": args[args.index("--base-token") + 1],
    "table_id": args[args.index("--table-id") + 1],
    "record_file": str(output.resolve()),
    "record_file_size_bytes": output.stat().st_size if output.exists() else 0,
    "records_count": len(rows), "has_more": False, "columns": {},
}
if mode == "bad_manifest_count":
    manifest["records_count"] += 1
if mode == "bad_manifest_size":
    manifest["record_file_size_bytes"] += 1
if mode == "bad_manifest_path":
    manifest["record_file"] = "other.ndjson"
if mode == "bad_manifest_table":
    manifest["table_id"] = "tbl_other"
if mode == "bad_manifest_base":
    manifest["base_token"] = "other_base"
if mode == "bad_manifest_format":
    manifest["format"] = "json"
if mode == "bad_manifest_version":
    manifest["manifest_version"] = "v2"
if mode == "bad_manifest_missing_count":
    del manifest["records_count"]
if mode == "false_manifest_success":
    manifest["ok"] = False
output.with_suffix(".manifest.json").write_text(json.dumps(manifest))
if mode == "legacy_artifact_envelope":
    success({"records_count": len(rows), "has_more": False})
else:
    print(json.dumps(manifest))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    def recorded():
        return json.loads(capture.read_text())

    return str(executable), recorded


@pytest.fixture(autouse=True)
def mock_reactions(monkeypatch):
    from unittest.mock import AsyncMock

    from lark_channel import FeishuChannel, SendResult

    reaction = AsyncMock(return_value=SendResult(success=True))
    monkeypatch.setattr(FeishuChannel, "add_reaction", reaction)
    return reaction


@pytest.fixture(autouse=True)
def mock_card_updates(monkeypatch):
    from unittest.mock import AsyncMock

    from lark_channel import FeishuChannel, SendResult

    update = AsyncMock(return_value=SendResult(success=True, message_id="om_reply"))
    monkeypatch.setattr(FeishuChannel, "update_card", update)
    return update


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    """Even accidentally constructed real providers cannot contact an endpoint in tests."""
    import httpx2

    async def blocked(*args, **kwargs):
        raise AssertionError("real HTTP requests are prohibited in offline tests")

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", blocked)
