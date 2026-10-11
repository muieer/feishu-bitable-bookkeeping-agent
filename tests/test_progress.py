import json

import pytest
from test_service import Decision, create, message, preference, service, written

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


async def test_real_create_events_no_success_until_confirmation(settings, fake_cli):
    s = service(settings, fake_cli, create(description='包子 **大份** <at id="all">\r\n热饮'))
    state, snapshots = await run(s)
    assert state.title == "已记账" and state.status == "已完成"
    assert '包子 **大份** <at id=\\"all\\"> 热饮' in text(state.snapshot())
    elements = state.snapshot()["body"]["elements"]
    assert elements[-1]["text"]["tag"] == "plain_text"
    assert next(e for e in elements if e["tag"] == "collapsible_panel")["expanded"] is False
    verification = next(
        card for stage, card in snapshots if stage == "table.create_verification_start"
    )
    assert "已记账：" not in text(verification)
    assert all(step.status == "completed" for step in state.steps)


async def test_correction_confirmed_after_readback(settings, fake_cli):
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "28"}))
    await s.reply(message())
    state, snapshots = await run(s, message("刚才改为28元", message_id="om_2"))
    verification = next(c for stage, c in snapshots if stage == "table.update_verification_start")
    assert "✓ 已确认修改结果" not in text(verification) and state.title == "已修改"
    assert "28元" in text(state.snapshot())


@pytest.mark.parametrize("mode", ["missing_id", "write_failure", "partial_visible"])
async def test_uncertain_feedback_never_claims_completed(settings, fake_cli, monkeypatch, mode):
    if mode == "partial_visible":
        mode = "duplicate_rows"
    s = service(settings, fake_cli, create())
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    state, _ = await run(s)
    assert state.status == "结果待核对" and "已记账：" not in text(state.snapshot())
    assert "勿重复" in state.response


@pytest.mark.parametrize(
    "d,status,title",
    [
        (Decision(action="ignore"), "已结束", "本次未记账"),
        (Decision(action="clarify", question="堂食还是外卖？"), "待补充", "请补充或确认"),
    ],
)
async def test_nonwrites_feedback(settings, fake_cli, d, status, title):
    state, _ = await run(service(settings, fake_cli, d))
    assert state.status == status and state.title == title
    assert not written(fake_cli)


async def test_preference_does_not_show_previous_expense(settings, fake_cli):
    s = service(settings, fake_cli, create(), preference())
    await s.reply(message())
    state, _ = await run(s, message("以后豆浆都归食品", message_id="om_2"))
    assert state.title == "已保存分类偏好" and "金额" not in state.result_text()


def test_waiting_elapsed_finish_freezes():
    now = [0]
    state = ProgressState(queued=True, clock=lambda: now[0])
    now[0] = 3
    assert "已等待 3 秒" in text(state.snapshot())
    state.start()
    now[0] = 11
    assert "已执行 8 秒" in text(state.snapshot())
    state.finish("结束")
    card = state.snapshot()
    now[0] = 500
    state.on_event("history.query", {})
    assert state.snapshot() == card


async def test_observer_failure_does_not_prevent_write(settings, fake_cli):
    def broken(*args):
        raise RuntimeError("observer")

    s = service(settings, fake_cli, create())
    with progress_scope(broken):
        assert "已记账" in await s.reply(message())
