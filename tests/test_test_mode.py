from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_progress import run, text
from test_service import Decision, create, message, service, written

from bookkeeping_agent.config import Settings


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), (None, False)])
def test_mode_from_env_file(settings, tmp_path, monkeypatch, value, expected):
    monkeypatch.delenv("TEST_MODE", raising=False)
    config = tmp_path / ".env"
    config.write_text(f"TEST_MODE={value}\n" if value else "")
    values = settings.model_dump(exclude={"test_mode"})
    assert Settings(_env_file=config, **values).test_mode is expected
    config.write_text("TEST_MODE=invalid\n")
    with pytest.raises(ValidationError):
        Settings(_env_file=config, **values)


async def test_mode_previews_create_and_correction_without_writes(settings, fake_cli):
    settings.test_mode = True
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "28"}))
    s.tool.create_records = AsyncMock(side_effect=AssertionError("unexpected create"))
    s.tool.update_record = AsyncMock(side_effect=AssertionError("unexpected update"))
    s.tool.get_record = AsyncMock(side_effect=AssertionError("preview has no remote record"))
    for incoming, amount in [
        (message(), "18.1"),
        (message("刚才改为28元", message_id="om_2"), "28"),
    ]:
        state, snapshots = await run(s, incoming)
        assert state.response.startswith("准备记账：")
        assert state.title == "准备记账" and not state.confirmed
        assert f"准备记账：2026/10/09｜饮食｜咖啡｜咖啡｜{amount}元" in text(state.snapshot())
        assert not any(step.key in {"write", "verify"} for step in state.steps)
        assert not any(stage == "table.confirmed" for stage, _ in snapshots)
    s.tool.create_records.assert_not_awaited()
    s.tool.update_record.assert_not_awaited()
    s.tool.get_record.assert_not_awaited()
    assert not Path(fake_cli[0]).with_name("written.json").exists()
    assert await s.reply(message()) is None
    assert await service(settings, fake_cli).reply(message()) is None

    # Test deduplication must not suppress a real write after switching back.
    settings.test_mode = False
    production = service(settings, fake_cli, create())
    assert "已记账" in await production.reply(message())
    assert len(written(fake_cli)) == 1


async def test_mode_preserves_validation_and_pending_clarification(settings, fake_cli):
    settings.test_mode = True
    s = service(settings, fake_cli, create(amount=None), create(amount="25"))
    response = await s.reply(message())
    assert "请补充" in response and "准备记账" not in response
    assert not Path(fake_cli[0]).with_name("written.json").exists()
    assert "准备记账" in await s.reply(message("刚才改为28元", message_id="om_2"))


async def test_mode_preserves_remote_schema_validation(settings, fake_cli, monkeypatch):
    settings.test_mode = True
    monkeypatch.setenv("FAKE_CLI_MODE", "schema_drift")
    s = service(settings, fake_cli, create())
    response = await s.reply(message())
    assert "准备记账" not in response
    assert s.journal.get("om_1")["status"] == "success"
    assert not Path(fake_cli[0]).with_name("written.json").exists()
