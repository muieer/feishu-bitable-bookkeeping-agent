import asyncio
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from lark_channel import SendResult
from test_agent import decision_model
from test_service import create

from bookkeeping_agent.channel import build_channel, register_handlers
from bookkeeping_agent.service import BookkeepingService
from bookkeeping_agent.tools import BitableTool
from bookkeeping_agent.tracing import TraceStore


def event(text="咖啡18元", message_id="om_test", sender_type="user", kind="text"):
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


async def test_sdk_event_agent_subprocess_reply_and_dedup(
    settings, fake_cli, monkeypatch, mock_reactions, mock_card_updates
):
    settings.lark_cli_path, recorded = fake_cli
    service = BookkeepingService(
        settings, BitableTool(settings), decision_model(create().model_dump(mode="json"))
    )
    channel = build_channel(settings)
    send = AsyncMock(return_value=SendResult(success=True, message_id="om_reply"))
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, service)
    incoming = event()
    await channel._handle_message_event(incoming)
    await channel._handle_message_event(incoming)
    send.assert_awaited_once()
    assert mock_reactions.await_args_list == [call("om_test", "OneSecond")]
    chat_id, payload, opts = send.call_args.args
    assert chat_id == "oc_test" and opts == {"reply_to": "om_test"}
    assert "已收到" in json.dumps(payload, ensure_ascii=False)
    final = json.dumps(mock_card_updates.call_args.args[1], ensure_ascii=False)
    assert "已记账" in final and "18.1元" in final
    assert recorded()["args"][:2] == ["base", "+record-batch-create"]


async def test_nonexpense_replies_without_calling_cli(
    settings, fake_cli, monkeypatch, mock_card_updates
):
    settings.lark_cli_path, recorded = fake_cli
    service = BookkeepingService(
        settings, BitableTool(settings), decision_model({"action": "ignore"})
    )
    channel = build_channel(settings)
    send = AsyncMock(return_value=SendResult(success=True, message_id="om_reply"))
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, service)
    await channel._handle_message_event(event("明天准备买咖啡"))
    send.assert_awaited_once()
    final = json.dumps(mock_card_updates.call_args.args[1], ensure_ascii=False)
    assert "未涉及实际消费" in final and "本次未记账" in final
    with pytest.raises(FileNotFoundError):
        recorded()


async def test_exception_and_reply_failure_do_not_leak_or_log_success(
    settings, monkeypatch, caplog
):
    service = SimpleNamespace(
        reply=AsyncMock(side_effect=RuntimeError("private-key")), traces=TraceStore(settings)
    )
    channel = build_channel(settings)
    send = AsyncMock(return_value=SendResult(success=False))
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, service)
    with caplog.at_level(logging.INFO):
        await channel._handle_message_event(event())
    assert "未完成" in send.call_args.args[1]["text"]
    assert "private-key" not in caplog.text
    assert "reply_failed" in caplog.text and "reply_sent" not in caplog.text


@pytest.mark.parametrize("sender,kind", [("bot", "text"), ("user", "image"), ("user", "audio")])
async def test_unsupported_message_ignored(settings, monkeypatch, sender, kind):
    service = SimpleNamespace(reply=AsyncMock(), traces=TraceStore(settings))
    channel = build_channel(settings)
    send = AsyncMock()
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, service)
    await channel._handle_message_event(event(sender_type=sender, kind=kind))
    service.reply.assert_not_called()
    send.assert_not_called()


async def test_send_failure_after_create_does_not_create_again(settings, fake_cli, monkeypatch):
    settings.lark_cli_path, _ = fake_cli
    service = BookkeepingService(
        settings, BitableTool(settings), decision_model(create().model_dump(mode="json"))
    )
    channel = build_channel(settings)
    monkeypatch.setattr(channel, "send", AsyncMock(return_value=SendResult(success=False)))
    register_handlers(channel, service)
    await channel._handle_message_event(event())
    # A fresh SDK instance has lost its in-memory dedup. The business journal still prevents writes.
    restarted = BookkeepingService(
        settings, BitableTool(settings), decision_model(create().model_dump(mode="json"))
    )
    fresh = build_channel(settings)
    send = AsyncMock(return_value=SendResult(success=True))
    monkeypatch.setattr(fresh, "send", send)
    register_handlers(fresh, restarted)
    await fresh._handle_message_event(event())
    send.assert_not_called()
    from test_service import written

    assert len(written(fake_cli)) == 1


@pytest.mark.parametrize("failure", ["result", "exception", "timeout"])
async def test_reaction_failure_does_not_block_processing(
    settings, monkeypatch, mock_reactions, caplog, failure
):
    monkeypatch.setattr("bookkeeping_agent.channel.REACTION_TIMEOUT_SECONDS", 0.01)

    async def react(message_id, emoji):
        if emoji == "OneSecond":
            if failure == "exception":
                raise RuntimeError("private-secret")
            if failure == "timeout":
                await asyncio.sleep(10)
            return SendResult(success=False)
        return SendResult(success=True)

    mock_reactions.side_effect = react

    async def reply(incoming):
        assert mock_reactions.await_count == 1
        return "处理完成"

    service = SimpleNamespace(reply=AsyncMock(side_effect=reply), traces=TraceStore(settings))
    channel = build_channel(settings)
    send = AsyncMock(return_value=SendResult(success=True, message_id="om_reply"))
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, service)
    await channel._handle_message_event(event())
    service.reply.assert_awaited_once()
    send.assert_awaited_once()
    assert send.call_args.args[2] == {"reply_to": "om_test"}
    assert "reaction_failed" in caplog.text and "private-secret" not in caplog.text


async def test_queued_message_acknowledged_before_previous_finishes(
    settings, monkeypatch, mock_reactions
):
    from lark_channel.channel.safety.pipeline import SafetyPipeline

    first_started = asyncio.Event()
    second_acknowledged = asyncio.Event()
    release_first = asyncio.Event()
    processed = []

    async def react(message_id, emoji):
        if message_id == "om_second" and emoji == "OneSecond":
            second_acknowledged.set()
        return SendResult(success=True)

    mock_reactions.side_effect = react

    async def reply(incoming):
        processed.append(incoming.message_id)
        if incoming.message_id == "om_first":
            first_started.set()
            await release_first.wait()
        return "处理完成"

    service = SimpleNamespace(reply=AsyncMock(side_effect=reply), traces=TraceStore(settings))
    channel = build_channel(settings)
    monkeypatch.setattr(channel, "send", AsyncMock(return_value=SendResult(success=True)))
    register_handlers(channel, service)
    channel._safety = SafetyPipeline(
        loop=asyncio.get_running_loop(),
        on_message=channel._dispatch_inbound_to_user,
        policy=channel._config.policy,
        batch_config=channel._config.safety.text_batch,
        queue_config=channel._config.safety.chat_queue,
    )
    first = asyncio.create_task(channel._handle_message_event(event(message_id="om_first")))
    second = None
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        second = asyncio.create_task(channel._handle_message_event(event(message_id="om_second")))
        await asyncio.wait_for(second_acknowledged.wait(), 1)
        assert processed == ["om_first"]
        release_first.set()
        await asyncio.gather(first, second)
        assert processed == ["om_first", "om_second"]
    finally:
        release_first.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_slow_first_reactions_do_not_reorder_processing(
    settings, monkeypatch, mock_reactions
):
    first_reacting = asyncio.Event()
    release_reactions = asyncio.Event()
    second_reacting = asyncio.Event()
    processed = []

    async def react(message_id, emoji):
        if message_id == "om_first":
            first_reacting.set()
            await release_reactions.wait()
        else:
            second_reacting.set()
        return SendResult(success=True)

    mock_reactions.side_effect = react

    async def reply(incoming):
        processed.append(incoming.message_id)
        return "处理完成"

    service = SimpleNamespace(reply=AsyncMock(side_effect=reply), traces=TraceStore(settings))
    channel = build_channel(settings)
    monkeypatch.setattr(channel, "send", AsyncMock(return_value=SendResult(success=True)))
    register_handlers(channel, service)
    first = asyncio.create_task(channel._handle_message_event(event(message_id="om_first")))
    second = None
    try:
        await asyncio.wait_for(first_reacting.wait(), 1)
        second = asyncio.create_task(channel._handle_message_event(event(message_id="om_second")))
        await asyncio.wait_for(second_reacting.wait(), 1)
        # A slow reaction must not delay either bookkeeping request.
        assert processed == ["om_first", "om_second"]
        release_reactions.set()
        await asyncio.gather(first, second)
        assert processed == ["om_first", "om_second"]
    finally:
        release_reactions.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
