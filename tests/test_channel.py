import asyncio
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lark_channel import SendResult
from test_agent import query_model

from bookkeeping_agent.agent import QueryService
from bookkeeping_agent.channel import build_channel, register_handlers
from bookkeeping_agent.tools import BitableQueryTool


def event(text="查询最近两条消费记录", message_id="om_test", sender_type="user", kind="text"):
    return SimpleNamespace(
        header=SimpleNamespace(event_id="evt_" + message_id),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type=sender_type, sender_id=SimpleNamespace(open_id="ou_test")
            ),
            message=SimpleNamespace(
                message_id=message_id,
                chat_id="oc_test",
                chat_type="p2p",
                create_time=str(int(time.time() * 1000)),
                message_type=kind,
                content=json.dumps({"text": text}),
                mentions=[],
            ),
        ),
    )


async def test_sdk_event_agent_subprocess_reply_and_dedup(settings, fake_cli, monkeypatch):
    """Uses SDK normalization and dedup, real Agent and real child process; no remote I/O."""
    settings.lark_cli_path, recorded = fake_cli
    service = QueryService(settings, BitableQueryTool(settings), query_model(2))
    channel = build_channel(settings)
    send = AsyncMock(return_value=SendResult(success=True, message_id="om_reply"))
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, service, 5)
    incoming = event()
    await channel._handle_message_event(incoming)
    await channel._handle_message_event(incoming)
    send.assert_awaited_once()
    chat_id, payload, opts = send.call_args.args
    assert chat_id == "oc_test" and opts == {"reply_to": "om_test"}
    assert "实际返回 2 条" in payload["text"]
    assert "18.10" in payload["text"]
    assert "25.00" in payload["text"]
    assert recorded()["args"][:2] == ["base", "+record-list"]


@pytest.mark.parametrize("mode, expected", [("failure", "查询失败"), ("bad_date", "格式异常")])
async def test_tool_errors_become_feishu_replies(settings, fake_cli, monkeypatch, mode, expected):
    settings.lark_cli_path, _ = fake_cli
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    service = QueryService(settings, BitableQueryTool(settings), query_model())
    channel = build_channel(settings)
    send = AsyncMock(return_value=SendResult(success=True))
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, service, 5)
    await channel._handle_message_event(event())
    text = send.call_args.args[1]["text"]
    assert expected in text
    assert "secret" not in text and "实际返回" not in text


async def test_llm_error_and_send_failure_do_not_leak_or_log_success(settings, monkeypatch, caplog):
    service = SimpleNamespace(reply=AsyncMock(side_effect=RuntimeError("private-key")))
    channel = build_channel(settings)
    send = AsyncMock(return_value=SendResult(success=False))
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, service, 5)
    with caplog.at_level(logging.INFO):
        await channel._handle_message_event(event())
    assert "查询未完成" in send.call_args.args[1]["text"]
    assert "private-key" not in caplog.text
    assert "reply_failed" in caplog.text and "reply_sent" not in caplog.text


async def test_model_timeout_replies(settings, monkeypatch):
    async def slow_reply(text):
        await asyncio.sleep(10)

    channel = build_channel(settings)
    send = AsyncMock(return_value=SendResult(success=True))
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, SimpleNamespace(reply=slow_reply), 0.01)
    await channel._handle_message_event(event())
    assert "超时" in send.call_args.args[1]["text"]


async def test_bot_message_ignored(settings, monkeypatch):
    service = SimpleNamespace(reply=AsyncMock())
    channel = build_channel(settings)
    send = AsyncMock()
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, service, 5)
    await channel._handle_message_event(event(sender_type="bot"))
    service.reply.assert_not_called()
    send.assert_not_called()
