import json

from test_service import create, message, service

from bookkeeping_agent.progress import ProgressState, progress_scope


def text(card):
    return json.dumps(card, ensure_ascii=False)


async def run(s, incoming=None):
    state, snapshots = ProgressState(), []
    state.start()

    def capture(stage, data):
        state.on_event(stage, data)
        snapshots.append((stage, state.snapshot()))

    with progress_scope(capture):
        response = await s.reply(incoming or message())
    state.finish(response, submitted=bool(s.journal.get((incoming or message()).message_id)))
    return state, snapshots


async def test_processing_only_elapsed_even_after_confirmation(settings, fake_cli):
    state, snapshots = await run(service(settings, fake_cli, create()))
    assert state.status == "已完成" and "已记账消费记录" in text(state.snapshot())
    for _, card in snapshots:
        assert len(card["body"]["elements"]) == 1
        content = card["body"]["elements"][0]["text"]["content"]
        assert content.startswith("已耗时 ")
        assert "咖啡" not in text(card) and "正在" not in text(card)
        assert "执行过程" not in text(card) and "collapsible_panel" not in text(card)


def test_elapsed_includes_queue_and_final_freezes():
    now = [0]
    state = ProgressState(queued=True, clock=lambda: now[0])
    now[0] = 3
    assert "已耗时 3 秒" in text(state.snapshot())
    state.start()
    now[0] = 11
    assert "已耗时 11 秒" in text(state.snapshot())
    state.finish("结束")
    card = state.snapshot()
    now[0] = 500
    state.on_event("agent.instructions", {"instructions": "不应显示的内容"})
    assert state.snapshot() == card


async def test_observer_failure_does_not_prevent_write(settings, fake_cli):
    def broken(*args):
        raise RuntimeError("observer")

    with progress_scope(broken):
        assert "已记账" in await service(settings, fake_cli, create()).reply(message())
