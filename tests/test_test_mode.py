from pathlib import Path
from unittest.mock import AsyncMock

from test_service import Decision, create, message, service, written


async def test_preview_create_and_update_no_remote_write(settings, fake_cli):
    settings.test_mode = True
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "28"}))
    s.tool.create_records = AsyncMock(side_effect=AssertionError("unexpected create"))
    s.tool.update_record = AsyncMock(side_effect=AssertionError("unexpected update"))
    s.tool.get_record = AsyncMock(side_effect=AssertionError("preview has no remote record"))
    assert "准备新增消费记录" in await s.reply(message())
    response = await s.reply(message("B1改为28元", message_id="om_2"))
    assert "准备更新消费记录" in response and "28元" in response
    s.tool.create_records.assert_not_awaited()
    s.tool.update_record.assert_not_awaited()
    s.tool.get_record.assert_not_awaited()
    assert not Path(fake_cli[0]).with_name("written.json").exists()
    assert await s.reply(message()) is None
    settings.test_mode = False
    assert "已记账" in await service(settings, fake_cli, create()).reply(message())
    assert len(written(fake_cli)) == 1


async def test_preview_preserves_business_and_schema_validation(settings, fake_cli, monkeypatch):
    settings.test_mode = True
    s = service(settings, fake_cli, create(amount=None), create())
    assert "执行失败" in await s.reply(message())
    monkeypatch.setenv("FAKE_CLI_MODE", "schema_drift")
    assert "字段不存在或类型不符" in await s.reply(message(message_id="om_2"))
    assert not written(fake_cli)
