"""Business tests use explicit plans and the real service/CLI/journal boundary."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from bookkeeping_agent.models import Decision as PlanDecision
from bookkeeping_agent.models import IncomingMessage, MessagePlan
from bookkeeping_agent.service import BookkeepingService
from bookkeeping_agent.tools import BitableTool


def Decision(**kwargs):
    if isinstance(kwargs.get("expense"), dict):
        kwargs["expense"] = {
            "date": None,
            "amount": None,
            "description": None,
            "category": None,
            **kwargs["expense"],
        }
    return PlanDecision.model_validate(
        {
            "decision_id": "d1",
            "source_text": "咖啡18元",
            "continues_pending": False,
            "target_id": None,
            "target_number": None,
            "expense": None,
            "preference": None,
            "question": None,
            **kwargs,
        }
    )


def create(**overrides):
    source = overrides.pop("source_text", "咖啡18元")
    decision_args = {
        k: overrides.pop(k)
        for k in list(overrides)
        if k
        in {
            "decision_id",
            "source_occurrence",
            "target_id",
            "target_reference",
            "target_number",
            "continues_pending",
            "question",
            "needs_history",
            "history_keywords",
        }
    }
    return Decision(
        action="create",
        source_text=source,
        expense={
            "amount": "18.10",
            "description": "咖啡",
            "category": {"level1": "饮食", "level2": "咖啡"},
            **overrides,
        },
        **decision_args,
    )


class Engine:
    """Bind a one-decision test fixture's source to the actual input. Multi-decision tests
    always provide explicit source snippets, so there is no text splitting in this fake."""

    def __init__(self, *plans):
        self.plans = iter(plans)
        self.contexts, self.texts = [], []

    async def decide(self, text, context):
        self.contexts.append(context)
        self.texts.append(text)
        value = next(self.plans)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, MessagePlan):
            return value
        if isinstance(value, list):
            decisions = [
                d.model_copy(update={"decision_id": f"d{n}"}) for n, d in enumerate(value, 1)
            ]
        else:
            decisions = [value.model_copy(update={"source_text": text})]
        return MessagePlan(decisions=decisions)


def message(text="咖啡18元", message_id="om_1", sent_at="2026-10-09T10:00:00+08:00", **kwargs):
    return IncomingMessage(
        message_id=message_id,
        user_id=kwargs.get("user_id", "ou_1"),
        chat_id=kwargs.get("chat_id", "oc_1"),
        text=text,
        sent_at=sent_at,
    )


def service(settings, fake_cli, *plans):
    settings.lark_cli_path, _ = fake_cli
    return BookkeepingService(settings, BitableTool(settings), engine=Engine(*plans))


def written(fake_cli):
    path = Path(fake_cli[0]).with_name("written.json")
    return json.loads(path.read_text()) if path.exists() else []


def bill(s, index=0):
    return list(s.sessions[("ou_1", "oc_1")].bills.values())[index]


async def test_create_exact_amount_date_dedup_restart(settings, fake_cli):
    s = service(settings, fake_cli, create(amount="0.29"))
    assert "0.29元" in await s.reply(message())
    assert written(fake_cli)[0]["金额"] == 0.29
    assert written(fake_cli)[0]["记账日期"].startswith("2026-10-09")
    assert await s.reply(message()) is None
    restarted = service(settings, fake_cli)
    assert await restarted.reply(message()) is None and not restarted.sessions
    assert len(written(fake_cli)) == 1


@pytest.mark.parametrize("amount", ["0", "-1", "1.001", "1000000000000.00"])
async def test_invalid_amount_is_independent_business_failure(settings, fake_cli, amount):
    s = service(
        settings,
        fake_cli,
        [create(amount=amount, source_text="坏账1元"), create(source_text="咖啡18元")],
    )
    response = await s.reply(message("坏账1元咖啡18元"))
    assert "成功1笔，待补充1笔" in response
    assert len(written(fake_cli)) == 1


@pytest.mark.parametrize("amount", [True, "NaN", "Infinity"])
def test_unparseable_amount_rejects_schema(amount):
    with pytest.raises(ValidationError):
        create(amount=amount)


async def test_invalid_category_future_date_missing_facts_do_not_write(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        [
            create(source_text="甲1元", category={"level1": "饮食", "level2": "汽车"}),
            create(source_text="乙2元", date="2026-10-10"),
            create(source_text="丙3元", description=None),
        ],
    )
    response = await s.reply(message("甲1元乙2元丙3元"))
    assert "待补充3笔" in response and not written(fake_cli)


async def test_timezone_and_pending_original_anchor(settings, fake_cli):
    s = service(settings, fake_cli, create())
    assert "2026/10/10" in await s.reply(message(sent_at="2026-10-09T20:00:00+00:00"))
    s = service(
        settings,
        fake_cli,
        Decision(
            action="clarify",
            expense={"amount": "25", "description": "午饭"},
            question="堂食还是外卖？",
        ),
        Decision(
            action="create",
            continues_pending=True,
            expense={"category": {"level1": "饮食", "level2": "堂食"}},
        ),
    )
    await s.reply(message("午饭25元", message_id="om_2", sent_at="2026-10-09T23:59:00+08:00"))
    response = await s.reply(
        message("堂食", message_id="om_3", sent_at="2026-10-11T00:01:00+08:00")
    )
    assert "2026/10/09" in response and "25元" in response
    assert s.engine.contexts[-1]["pending_items"][0]["sent_at"].startswith("2026-10-09")


async def test_new_consumption_does_not_clear_or_inherit_pending(settings, fake_cli):
    s = service(settings, fake_cli, Decision(action="clarify", question="买了什么？"), create())
    await s.reply(message("买东西", sent_at="2026-10-09T23:59:00+08:00"))
    response = await s.reply(
        message("咖啡18元", message_id="om_2", sent_at="2026-10-10T00:01:00+08:00")
    )
    assert "2026/10/10" in response and "待补充1笔" in response
    assert bill(s).status == "pending" and bill(s, 1).facts["amount"] == "18.10"


async def test_multiple_supplements_keep_known_facts(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        Decision(action="clarify", expense={"description": "午饭"}, question="多少钱？"),
        Decision(
            action="clarify",
            expense={"amount": "25"},
            question="堂食还是外卖？",
            continues_pending=True,
        ),
        Decision(
            action="create",
            expense={"category": {"level1": "饮食", "level2": "堂食"}},
            continues_pending=True,
        ),
    )
    await s.reply(message("午饭"))
    await s.reply(message("25元", message_id="om_2"))
    assert "午饭｜25元" in await s.reply(message("堂食", message_id="om_3"))
    assert len(written(fake_cli)) == 1


async def test_correction_only_changes_patch_and_clears_third_level(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        create(category={"level1": "交通通信", "level2": "汽车", "level3": "充电"}),
        Decision(action="update", expense={"amount": "28"}),
        Decision(action="update", expense={"category": {"level1": "饮食", "level2": "咖啡"}}),
    )
    await s.reply(message())
    assert "已修改" in await s.reply(message("不对，刚才是28元", message_id="om_2"))
    assert written(fake_cli)[0]["三级分类"] == ["充电"]
    assert "已修改" in await s.reply(message("分类改为咖啡", message_id="om_3"))
    assert len(written(fake_cli)) == 1 and written(fake_cli)[0]["三级分类"] is None
    assert not settings.preferences_path.exists()


@pytest.mark.parametrize("mode", ["external_edit", "update_not_applied", "ignored_fields"])
async def test_correction_checks_remote_before_and_after(settings, fake_cli, monkeypatch, mode):
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "28"}))
    await s.reply(message())
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    response = await s.reply(message("改为28元", message_id="om_2"))
    assert "已修改" not in response and "核对" in response
    assert bill(s).status == ("failed" if mode == "external_edit" else "uncertain")


@pytest.mark.parametrize("other", [{"user_id": "ou_other"}, {"chat_id": "oc_other"}])
async def test_context_isolation(settings, fake_cli, other):
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "28"}))
    await s.reply(message())
    assert "没有可纠错" in await s.reply(message("刚才改为28元", message_id="om_2", **other))
    assert written(fake_cli)[0]["金额"] == 18.1


async def test_context_expiry(settings, fake_cli):
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "28"}))
    await s.reply(message())
    s.sessions[("ou_1", "oc_1")].touched -= settings.context_ttl_seconds + 1
    assert "没有可纠错" in await s.reply(message("改为28元", message_id="om_2"))


def preference(evidence="以后豆浆都归食品", **kwargs):
    return Decision(
        action="preference",
        source_text=evidence,
        preference={
            "subject": "豆浆",
            "condition": "以后豆浆归食品",
            "evidence": evidence,
            "category": {"level1": "饮食", "level2": "食品"},
            **kwargs,
        },
    )


async def test_preference_updates_existing_and_no_historical_reclassification(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        create(),
        preference(),
        preference(category={"level1": "饮食", "level2": "咖啡"}),
    )
    await s.reply(message())
    await s.reply(message("以后豆浆都归食品", message_id="om_2"))
    await s.reply(message("以后豆浆都归食品", message_id="om_3"))
    assert len(s.knowledge.preferences()) == 1
    assert s.knowledge.preferences()[0].category.level2 == "咖啡"
    assert written(fake_cli)[0]["金额"] == 18.1
    assert len(service(settings, fake_cli).knowledge.preferences()) == 1


@pytest.mark.parametrize(
    "text,kwargs",
    [
        ("这次豆浆归食品", {}),
        ("以后豆浆都归食品", {"category": {"level1": "饮食", "level2": "汽车"}}),
    ],
)
async def test_preference_requires_authorization_and_valid_path(settings, fake_cli, text, kwargs):
    s = service(settings, fake_cli, preference(**kwargs))
    assert "尚未保存" in await s.reply(message(text))
    assert not settings.preferences_path.exists()


@pytest.mark.parametrize("mode", ["missing_id", "write_failure", "write_timeout"])
async def test_uncertain_writes_block_bill_followup(settings, fake_cli, monkeypatch, mode):
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "20"}))
    settings.cli_timeout_seconds = 0.15 if mode == "write_timeout" else 3
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    assert "已记账" not in await s.reply(message())
    assert bill(s).status == "uncertain"
    monkeypatch.delenv("FAKE_CLI_MODE")
    assert "待核对" in await s.reply(message("金额改为20元", message_id="om_2"))
    assert len(written(fake_cli)) <= 1


@pytest.mark.parametrize("mode", ["schema_drift", "failure", "false_success"])
async def test_preflight_failure_is_unexecuted(settings, fake_cli, monkeypatch, mode):
    s = service(settings, fake_cli, create())
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    response = await s.reply(message())
    assert "已记账" not in response and bill(s).status == "failed" and not written(fake_cli)
    assert not s.journal.get(f"bill/{bill(s).bill_id}/create")


async def test_truncated_schema_options_are_loaded(settings, fake_cli, monkeypatch):
    s = service(settings, fake_cli, create())
    monkeypatch.setenv("FAKE_CLI_MODE", "truncated")
    assert "已记账" in await s.reply(message())


async def test_journal_failure_prevents_submission(settings, fake_cli, monkeypatch):
    s = service(settings, fake_cli, create())
    monkeypatch.setattr(s.journal, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert "未执行写入" in await s.reply(message()) and not written(fake_cli)


async def test_batch_reservation_failure_prevents_submission(settings, fake_cli, monkeypatch):
    s = service(settings, fake_cli, create())
    monkeypatch.setattr(
        s.journal, "reserve_operations", lambda *a: (_ for _ in ()).throw(OSError("disk"))
    )
    assert "未执行写入" in await s.reply(message()) and not written(fake_cli)


async def test_local_failure_after_remote_confirmation_preserves_success(
    settings, fake_cli, monkeypatch
):
    s = service(settings, fake_cli, create())
    save = s.journal.save

    def fail(mid, status, **metadata):
        if status == "success":
            raise OSError("disk")
        save(mid, status, **metadata)

    monkeypatch.setattr(s.journal, "save", fail)
    response = await s.reply(message())
    assert "已记账" in response and "去重状态保存失败" in response
    assert bill(s).status == "success"
    assert "待核对" in await service(settings, fake_cli).reply(message())
    assert len(written(fake_cli)) == 1


async def test_unparseable_envelope_and_model_error_never_call_cli(settings, fake_cli):
    s = service(settings, fake_cli)
    s.engine.decide = AsyncMock(return_value={"decisions": [create().model_dump(), {"bad": True}]})
    assert "理解失败" in await s.reply(message())
    s.engine.decide = AsyncMock(side_effect=TimeoutError())
    assert "超时" in await s.reply(message(message_id="om_2"))
    s.engine.decide = AsyncMock(side_effect=RuntimeError("private-secret"))
    assert "private-secret" not in await s.reply(message(message_id="om_3"))
    assert not Path(fake_cli[0]).with_name("args.json").exists()


async def test_chat_order_serializes_understanding_and_execution(settings, fake_cli):
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "28"}))
    decide = s.engine.decide
    active = 0

    async def serial(text, context):
        nonlocal active
        active += 1
        assert active == 1
        await asyncio.sleep(0.01)
        result = await decide(text, context)
        active -= 1
        return result

    s.engine.decide = serial
    responses = await asyncio.gather(
        s.reply(message()), s.reply(message("刚才改为28元", message_id="om_2"))
    )
    assert "已记账" in responses[0] and "已修改" in responses[1]
    assert len(written(fake_cli)) == 1


async def test_question_prevents_write_of_complete_expense(settings, fake_cli):
    s = service(settings, fake_cli, create(question="消费用途是什么？"))
    assert "用途" in await s.reply(message()) and not written(fake_cli)


async def test_ignore_preserves_pending_but_clears_latest_mapping(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        Decision(action="clarify", question="买了什么？"),
        Decision(action="ignore"),
    )
    await s.reply(message("买东西"))
    assert (
        await s.reply(message("明天打算买咖啡", message_id="om_2"))
        == "这条消息未涉及实际消费，本次未记账。"
    )
    assert bill(s).status == "pending" and s.sessions[("ou_1", "oc_1")].display == []


async def test_cancel_pending_and_reject_cancel_of_uncertainty(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        Decision(action="clarify", question="买了什么？"),
        Decision(action="ignore", cancel_pending=True),
        create(),
        Decision(action="ignore", cancel_pending=True, target_number=1),
    )
    await s.reply(message("买东西"))
    assert "已取消" in await s.reply(message("取消记账", message_id="om_2"))
    assert bill(s).status == "cancelled"
    await s.reply(message("咖啡18元", message_id="om_3"))
    assert "只能取消" in await s.reply(message("取消第一笔", message_id="om_4"))
    assert len(written(fake_cli)) == 1
