import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from bookkeeping_agent.tools import BitableQueryTool, QueryError


async def test_top_n_command_and_artifact_cleanup(settings, fake_cli):
    executable, recorded = fake_cli
    settings.lark_cli_path = executable
    records = await BitableQueryTool(settings).recent_expenses(2)
    assert [record.record_id for record in records] == ["r1", "r2"]
    assert str(records[0].amount) == "18.1"
    invocation = recorded()
    assert invocation["args"].count("--field-id") == 3
    assert invocation["args"][invocation["args"].index("--limit") + 1] == "2"
    assert not Path(invocation["cwd"]).exists()


async def test_legacy_artifact_envelope_still_readable(settings, fake_cli, monkeypatch):
    settings.lark_cli_path, _ = fake_cli
    monkeypatch.setenv("FAKE_CLI_MODE", "legacy_artifact_envelope")
    assert len(await BitableQueryTool(settings).recent_expenses(2)) == 2


@pytest.mark.parametrize(
    "mode",
    [
        "bad_manifest_count",
        "bad_manifest_size",
        "bad_manifest_path",
        "bad_manifest_table",
        "bad_manifest_base",
        "bad_manifest_format",
        "bad_manifest_version",
        "bad_manifest_missing_count",
        "false_manifest_success",
    ],
)
async def test_invalid_record_manifest_is_rejected(settings, fake_cli, monkeypatch, mode):
    settings.lark_cli_path, recorded = fake_cli
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    with pytest.raises(QueryError):
        await BitableQueryTool(settings).recent_expenses(2)
    assert not Path(recorded()["cwd"]).exists()


@pytest.mark.parametrize("n", [0, -1, 101, 1.5, "10", True])
async def test_invalid_count_never_launches_cli(settings, monkeypatch, n):
    launch = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with pytest.raises(ValidationError):
        await BitableQueryTool(settings).recent_expenses(n)
    launch.assert_not_called()


@pytest.mark.parametrize(
    "mode",
    [
        "failure",
        "false_success",
        "bad_date",
        "missing_field",
        "bad_amount",
        "missing_file",
    ],
)
async def test_bad_cli_results_are_not_empty_success(settings, fake_cli, monkeypatch, mode):
    settings.lark_cli_path, recorded = fake_cli
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    with pytest.raises(QueryError) as error:
        await BitableQueryTool(settings).recent_expenses(10)
    assert "secret" not in str(error.value)
    assert not Path(recorded()["cwd"]).exists()


async def test_timeout_kills_child_and_cleans_files(settings, fake_cli, monkeypatch):
    settings.lark_cli_path, _ = fake_cli
    settings.cli_timeout_seconds = 0.3
    monkeypatch.setenv("FAKE_CLI_MODE", "timeout")
    original = asyncio.create_subprocess_exec
    children, directories = [], []

    async def launch(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        directories.append(kwargs["cwd"])
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with pytest.raises(QueryError, match="超时"):
        await BitableQueryTool(settings).recent_expenses(10)
    assert children[0].returncode is not None
    assert not Path(directories[0]).exists()


async def test_cancellation_kills_child(settings, fake_cli, monkeypatch):
    settings.lark_cli_path, _ = fake_cli
    monkeypatch.setenv("FAKE_CLI_MODE", "timeout")
    original = asyncio.create_subprocess_exec
    children = []
    started = asyncio.Event()

    async def launch(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(BitableQueryTool(settings).recent_expenses(10))
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].returncode is not None


async def test_view_with_empty_date_fails_without_widening_scope(settings, fake_cli, monkeypatch):
    settings.lark_cli_path, recorded = fake_cli
    settings.bitable_view_id = "vew_test"
    monkeypatch.setenv("FAKE_CLI_MODE", "empty_date")
    with pytest.raises(QueryError, match="空记账日期"):
        await BitableQueryTool(settings).recent_expenses(10)
    assert "--filter-json" not in recorded()["args"]
