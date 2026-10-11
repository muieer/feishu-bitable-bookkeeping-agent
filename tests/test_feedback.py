import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lark_channel import SendResult
from test_channel import event
from test_service import create, service, written

from bookkeeping_agent.channel import build_channel, register_handlers
from bookkeeping_agent.feedback import CardFeedback


def text(card):
    return json.dumps(card, ensure_ascii=False)


@pytest.fixture(autouse=True)
def fast_feedback(monkeypatch):
    monkeypatch.setattr("bookkeeping_agent.feedback.UPDATE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("bookkeeping_agent.feedback.HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr("bookkeeping_agent.feedback.FEEDBACK_TIMEOUT_SECONDS", 0.1)


async def test_immediate_reply_queue_live_progress_and_final_order(
    settings, fake_cli, monkeypatch, mock_reactions
):
    s = service(settings, fake_cli, create(), create(amount="29"))
    channel = build_channel(settings)
    sends, updates = [], []
    first_running, second_received, live, release = (asyncio.Event() for _ in range(4))
    decide = s.engine.decide

    async def blocked_decide(content, context):
        if not first_running.is_set():
            first_running.set()
            await release.wait()
        return await decide(content, context)

    async def send(chat, payload, opts):
        sends.append((payload, opts))
        if opts["reply_to"] == "om_second":
            second_received.set()
        return SendResult(success=True, message_id="reply_" + opts["reply_to"])

    async def update(mid, card):
        updates.append((mid, card))
        if mid == "reply_om_first" and "处理中" in text(card):
            live.set()
        return SendResult(success=True, message_id=mid)

    monkeypatch.setattr(s.engine, "decide", blocked_decide)
    monkeypatch.setattr(channel, "send", send)
    monkeypatch.setattr(channel, "update_card", update)
    register_handlers(channel, s)
    first = asyncio.create_task(channel._handle_message_event(event(message_id="om_first")))
    second = None
    try:
        await asyncio.wait_for(first_running.wait(), 1)
        assert "已收到" in text(sends[0][0])
        assert sends[0][1] == {"reply_to": "om_first"}
        second = asyncio.create_task(channel._handle_message_event(event(message_id="om_second")))
        await asyncio.wait_for(second_received.wait(), 1)
        assert "等待上一条消息" in text(sends[1][0])
        await asyncio.wait_for(live.wait(), 1)
        assert "正在理解" in text(updates[0][1])
        assert "已记账" not in text(updates[0][1])
        release.set()
        await asyncio.gather(first, second)
        final = {mid: card for mid, card in updates}
        for mid, amount in [("reply_om_first", "18.1"), ("reply_om_second", "29")]:
            elements = final[mid]["body"]["elements"]
            assert elements[-1]["text"]["content"].endswith(f"｜{amount}元")
        count = len(updates)
        await asyncio.sleep(0.05)
        assert len(updates) == count and len(sends) == 2
        assert mock_reactions.await_count == 2
        assert all(call.args[1] == "OneSecond" for call in mock_reactions.await_args_list)
        assert len(written(fake_cli)) == 2
    finally:
        release.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


@pytest.mark.parametrize("failure", ["result", "exception", "timeout"])
async def test_initial_card_failure_does_not_prevent_bookkeeping(
    settings, fake_cli, monkeypatch, failure
):
    s = service(settings, fake_cli, create())
    channel = build_channel(settings)
    calls = []

    async def send(chat, payload, opts):
        calls.append(payload)
        if len(calls) == 1:
            if failure == "exception":
                raise RuntimeError("private-secret")
            if failure == "timeout":
                await asyncio.sleep(10)
            return SendResult(success=False)
        return SendResult(success=True, message_id="final")

    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, s)
    await channel._handle_message_event(event())
    assert len(written(fake_cli)) == 1
    assert len(calls) == 2
    assert "已记账" in text(calls[-1]) and "private-secret" not in text(calls[-1])


@pytest.mark.parametrize("card_fallback_success", [True, False])
async def test_final_failure_falls_back_without_repeating_write(
    settings, fake_cli, monkeypatch, mock_card_updates, card_fallback_success
):
    s = service(settings, fake_cli, create())
    channel = build_channel(settings)
    send = AsyncMock(
        side_effect=[
            SendResult(success=True, message_id="initial"),
            SendResult(success=card_fallback_success, message_id="final"),
            SendResult(success=True, message_id="text"),
        ]
    )
    mock_card_updates.return_value = SendResult(success=False)
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, s)
    await channel._handle_message_event(event())
    assert send.await_count == (2 if card_fallback_success else 3)
    assert "已记账" in text(send.call_args.args[1])
    assert ("card" if card_fallback_success else "text") in send.call_args.args[1]
    assert len(written(fake_cli)) == 1


async def test_inflight_progress_drained_before_final_and_no_stale_overwrite(monkeypatch):
    patch_started, release_patch = asyncio.Event(), asyncio.Event()
    applied = []

    async def patch(mid, card):
        if not patch_started.is_set():
            patch_started.set()
            await release_patch.wait()
        applied.append(card)
        return SendResult(success=True, message_id=mid)

    channel = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="card")),
        update_card=patch,
    )
    feedback = CardFeedback(channel, SimpleNamespace(chat_id="chat", message_id="source"))
    await feedback.acknowledge()
    feedback.state.start()
    feedback.dirty.set()
    await asyncio.wait_for(patch_started.wait(), 1)
    feedback.state.finish("最终结果")
    final = asyncio.create_task(feedback.deliver_final())
    await asyncio.sleep(0)
    assert not final.done()
    release_patch.set()
    await final
    assert len(applied) == 2
    assert "最终结果" in text(applied[-1])
    assert "最终结果" not in text(applied[0])
    await asyncio.sleep(0.03)
    assert len(applied) == 2 and feedback.worker is None


async def test_temporary_progress_failure_is_retried_on_next_heartbeat():
    patched = asyncio.Event()
    calls = 0

    async def patch(mid, card):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SendResult(success=False)
        patched.set()
        return SendResult(success=True, message_id=mid)

    channel = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="card")), update_card=patch
    )
    feedback = CardFeedback(channel, SimpleNamespace(chat_id="chat", message_id="source"))
    await feedback.acknowledge()
    try:
        feedback.state.start()
        feedback.dirty.set()
        await asyncio.wait_for(patched.wait(), 1)
        assert calls == 2
    finally:
        await feedback.stop()


@pytest.mark.parametrize("submitted", [False, True])
async def test_cancellation_closes_card_and_stops_updates(settings, monkeypatch, submitted):
    from bookkeeping_agent.tracing import TraceStore, current_trace, emit

    started = asyncio.Event()

    async def reply(incoming):
        emit("decision.context")
        current_trace().mutation_submitted = submitted
        started.set()
        await asyncio.Event().wait()

    s = SimpleNamespace(reply=reply, traces=TraceStore(settings))
    channel = build_channel(settings)
    monkeypatch.setattr(
        channel, "send", AsyncMock(return_value=SendResult(success=True, message_id="initial"))
    )
    update = AsyncMock(return_value=SendResult(success=True))
    monkeypatch.setattr(channel, "update_card", update)
    register_handlers(channel, s)
    task = asyncio.create_task(channel._handle_message_event(event()))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    card = text(update.call_args.args[1])
    assert ("结果待核对" if submitted else "已中断") in card
    assert ("勿重复发送" if submitted else "未执行写入") in card
    count = update.await_count
    await asyncio.sleep(0.04)
    assert update.await_count == count


@pytest.mark.parametrize("failure_stage", ["reply.request", "card.final.request"])
async def test_diagnostic_failure_before_final_delivery_preserves_success_and_warning(
    settings, fake_cli, monkeypatch, mock_card_updates, failure_stage
):
    from test_tracing import fail_at

    s = service(settings, fake_cli, create())
    channel = build_channel(settings)
    monkeypatch.setattr(
        channel, "send", AsyncMock(return_value=SendResult(success=True, message_id="initial"))
    )
    register_handlers(channel, s)
    fail_at(monkeypatch, failure_stage)
    await channel._handle_message_event(event())
    card = text(mock_card_updates.call_args.args[1])
    assert "已记账" in card and "调试日志保存失败" in card
    assert "未执行写入" not in card
    assert s.journal.get("om_test")["status"] == "success"
    assert len(written(fake_cli)) == 1


@pytest.mark.parametrize("failure_stage", ["card.final.result", "reply.result"])
async def test_diagnostic_failure_after_final_delivery_never_resends(
    settings, fake_cli, monkeypatch, mock_card_updates, failure_stage
):
    from test_tracing import fail_at

    s = service(settings, fake_cli, create())
    channel = build_channel(settings)
    send = AsyncMock(return_value=SendResult(success=True, message_id="initial"))
    monkeypatch.setattr(channel, "send", send)
    register_handlers(channel, s)
    fail_at(monkeypatch, failure_stage)
    await channel._handle_message_event(event())
    send.assert_awaited_once()
    finals = [
        call.args[1]
        for call in mock_card_updates.await_args_list
        if call.args[1]["body"]["elements"][-1]
        .get("text", {})
        .get("content", "")
        .startswith("已记账：")
    ]
    assert len(finals) == 1
    assert mock_card_updates.call_args.args[1] == finals[0]
    assert len(written(fake_cli)) == 1


async def test_cancellation_during_final_delivery_keeps_known_business_result(
    settings, fake_cli, monkeypatch
):
    s = service(settings, fake_cli, create())
    channel = build_channel(settings)
    started = asyncio.Event()
    cards = []

    async def update(mid, card):
        if card["body"]["elements"][0]["text"]["content"].startswith("已完成"):
            cards.append(card)
            if len(cards) == 1:
                started.set()
                await asyncio.Event().wait()
        return SendResult(success=True, message_id=mid)

    monkeypatch.setattr(
        channel, "send", AsyncMock(return_value=SendResult(success=True, message_id="initial"))
    )
    monkeypatch.setattr(channel, "update_card", update)
    register_handlers(channel, s)
    task = asyncio.create_task(channel._handle_message_event(event()))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(cards) == 2 and "已记账" in text(cards[-1])
    assert "待核对" not in text(cards[-1]) and len(written(fake_cli)) == 1
