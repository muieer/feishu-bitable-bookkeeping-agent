import json
import sys

import pytest

from bookkeeping_agent.config import Settings


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        lark_app_id="cli_test",
        lark_app_secret="test-secret",
        ark_api_key="test-key",
        ark_model="test-model",
        bitable_base_token="base_test",
        bitable_table_id="tbl_test",
    )


@pytest.fixture
def fake_cli(tmp_path):
    """A real subprocess with the documented CLI artifact contract; never accesses Feishu."""
    executable = tmp_path / "lark-cli"
    capture = tmp_path / "args.json"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
Path(__file__).with_name("args.json").write_text(json.dumps({"args": args, "cwd": os.getcwd()}))
assert args[:2] == ["base", "+record-list"]
assert json.loads(args[args.index("--sort-json") + 1]) == [{"field": "记账日期", "desc": True}]
if "--view-id" in args:
    assert "--filter-json" not in args
else:
    assert json.loads(args[args.index("--filter-json") + 1])["conditions"] == [["记账日期", "non_empty"]]
assert args[args.index("--as") + 1] == "user"
output = Path(args[args.index("--output") + 1])
assert not output.is_absolute()
n = int(args[args.index("--limit") + 1])
mode = os.environ.get("FAKE_CLI_MODE", "ok")
if mode == "timeout":
    time.sleep(20)
if mode == "failure":
    print('{"ok":false,"error":{"message":"secret must not leak"}}', file=sys.stderr)
    sys.exit(1)
if mode == "false_success":
    print('{"ok":false}')
    sys.exit(0)
rows = [
    {"record_id": "r1", "记账日期": "2026-10-09T00:00:00+08:00", "金额": 18.10, "具体": "咖啡"},
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
output.with_suffix(".manifest.json").write_text(json.dumps({"records_count": len(rows), "has_more": n < 3}))
print(json.dumps({"ok": True, "data": {"records_count": len(rows), "has_more": n < 3}}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    def recorded():
        return json.loads(capture.read_text())

    return str(executable), recorded
