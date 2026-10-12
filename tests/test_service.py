"""Business boundaries use explicit two-stage fixtures and the real fake-CLI/journal path."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from bookkeeping_agent.agent import normalize_source
from bookkeeping_agent.models import Decision as PlanDecision
from bookkeeping_agent.models import IncomingMessage, MessagePlan, RecognitionResult
from bookkeeping_agent.service import BookkeepingService
from bookkeeping_agent.tools import BitableTool


def Decision(**kwargs):
    expense = kwargs.get("expense")
    if isinstance(expense, dict):
        kwargs["expense"] = {
            "date": None,
            "amount": None,
            "description": None,
            "category": None,
            **expense,
        }
    return TypeAdapter(PlanDecision).validate_python(
        {"bill_id": "B1", "raw": "咖啡18元", "expense": None, "reason": None, **kwargs}
    )


def create(**overrides):
    args = {k: overrides.pop(k) for k in list(overrides) if k in {"bill_id", "raw"}}
    return Decision(
        action="create",
        expense={
            "date": "2026-10-09",
            "amount": "18",
            "description": "咖啡",
            "category": {"level1": "饮食", "level2": "咖啡", "level3": None},
            **overrides,
        },
        **args,
    )


class Engine:
    """Fixtures expose both phases; identity binding is test setup, not app compatibility."""

    def __init__(self, *plans):
        self.plans = iter(plans)
        self.contexts, self.texts, self.recognition_calls, self.decision_calls = [], [], 0, 0

    async def recognize(self, text, context):
        self.recognition_calls += 1
        self.texts.append(text)
        self.contexts.append(context)
        value = next(self.plans)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, RecognitionResult):
            return value
        if value is None:
            self.current = []
            return RecognitionResult(items=[])
        self.current = (
            value.decisions
            if isinstance(value, MessagePlan)
            else value
            if isinstance(value, list)
            else [value.model_copy(update={"raw": normalize_source(text)})]
        )
        return RecognitionResult(
            items=[
                {
                    "type": "existing" if d.action == "update" else "new",
                    "target_id": d.bill_id if d.action == "update" else None,
                    "raw": d.raw,
                }
                for d in self.current
            ]
        )

    async def decide(self, items, context):
        self.decision_calls += 1
        return MessagePlan(
            decisions=[
                d.model_copy(update={"bill_id": item.bill_id})
                for item, d in zip(items, self.current, strict=True)
            ]
        )


def message(text="咖啡18元", message_id="om_1", sent_at="2026-10-09T10:00:00+08:00", **kwargs):
    return IncomingMessage(
        message_id=message_id,
        user_id=kwargs.get("user_id", "ou_1"),
        chat_id=kwargs.get("chat_id", "oc_1"),
        sent_at=sent_at,
        text=text,
    )


def service(settings, fake_cli, *plans):
    settings.lark_cli_path, _ = fake_cli
    return BookkeepingService(settings, BitableTool(settings), engine=Engine(*plans))


def written(fake_cli):
    path = Path(fake_cli[0]).with_name("written.json")
    return json.loads(path.read_text()) if path.exists() else []


def calls(fake_cli):
    path = Path(fake_cli[0]).with_name("calls.jsonl")
    return (
        [json.loads(row)["args"] for row in path.read_text().splitlines()] if path.exists() else []
    )


async def test_create_and_explicit_update_only_changed_fields(settings, fake_cli):
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "25"}))
    assert "B1：2026/10/09｜饮食｜咖啡｜咖啡｜18元" in await s.reply(message())
    original = written(fake_cli)[0]
    result = await s.reply(message("B1账单金额更新为25元", message_id="om_2"))
    assert "已更新消费记录" in result and "B1：" in result and "25元" in result
    assert written(fake_cli)[0] == {**original, "金额": 25}
    payload = json.loads(Path(fake_cli[0]).with_name("payload-capture.json").read_text())
    assert payload == {"update_records": {original["record_id"]: {"金额": 25}}}
    bill = s.sessions[("ou_1", "oc_1")].bills["B1"]
    assert bill.record_id == original["record_id"] and bill.confirmed.amount == 25


async def test_no_consumption_stops_after_recognition_and_deduplicates(settings, fake_cli):
    s = service(settings, fake_cli, None)
    assert await s.reply(message("我很开心")) == "此次不包含消费行为。"
    assert s.engine.recognition_calls == 1 and s.engine.decision_calls == 0
    assert not calls(fake_cli)
    assert await s.reply(message("我很开心")) is None


async def test_clarify_has_identity_but_no_pending_bill_or_followup(settings, fake_cli):
    s = service(
        settings, fake_cli, Decision(action="clarify", reason="分类依据不足"), create(amount="25")
    )
    assert "B1：咖啡18元。原因：分类依据不足" in await s.reply(message())
    session = s.sessions[("ou_1", "oc_1")]
    assert session.bills["B1"].facts == {} and session.bills["B1"].confirmed is None
    assert not calls(fake_cli)
    response = await s.reply(message("咖啡25元", message_id="om_2"))
    assert "B2：" in response and "B1：" not in response
    assert len(written(fake_cli)) == 1
    assert "pending_items" not in s.engine.contexts[-1]


@pytest.mark.parametrize(
    "mode", ["external_edit", "bad_manifest_count", "duplicate_rows", "missing_file"]
)
async def test_update_preflight_failure_never_overwrites_remote(
    settings, fake_cli, monkeypatch, mode
):
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "25"}))
    await s.reply(message())
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    response = await s.reply(message("B1改为25元", message_id="om_2"))
    assert "执行失败" in response and "已更新" not in response
    assert written(fake_cli)[0]["金额"] == 18
    assert not any(c[1] == "+record-batch-update" for c in calls(fake_cli))


@pytest.mark.parametrize("amount", [None, "0", "-1", "1.001", "100000000000000"])
async def test_business_invalid_amount_is_failure_not_model_clarify(settings, fake_cli, amount):
    s = service(settings, fake_cli, create(amount=amount))
    response = await s.reply(message())
    assert "执行失败" in response and "有效金额" in response
    assert "以下消费记录未写入" not in response and not written(fake_cli)


async def test_invalid_category_and_missing_tertiary_rejected(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        create(category={"level1": "交通通信", "level2": "汽车", "level3": None}),
    )
    assert "分类路径无效" in await s.reply(message())
    assert not written(fake_cli)


async def test_future_date_rejected(settings, fake_cli):
    s = service(settings, fake_cli, create(date="2026-10-10"))
    assert "消费日期在未来" in await s.reply(message())
    assert not written(fake_cli)


@pytest.mark.parametrize("mode", ["missing_id", "write_failure", "ignored_fields"])
async def test_uncertain_write_is_never_retried_on_redelivery_or_restart(
    settings, fake_cli, monkeypatch, mode
):
    s = service(settings, fake_cli, create())
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    response = await s.reply(message())
    assert "待核对" in response and "已记账消费" not in response
    assert await s.reply(message()) is None
    assert await service(settings, fake_cli).reply(message()) is None
    assert sum(c[1] == "+record-batch-create" for c in calls(fake_cli)) == 1


async def test_update_uncertain_cannot_be_updated_again(settings, fake_cli, monkeypatch):
    s = service(
        settings,
        fake_cli,
        create(),
        Decision(action="update", expense={"amount": "25"}),
        Decision(action="update", expense={"amount": "30"}),
    )
    await s.reply(message())
    monkeypatch.setenv("FAKE_CLI_MODE", "wrong_id")
    assert "待核对" in await s.reply(message("B1改为25元", message_id="om_2"))
    monkeypatch.setenv("FAKE_CLI_MODE", "ok")
    assert "待核对" in await s.reply(message("B1改为30元", message_id="om_3"))
    assert sum(c[1] == "+record-batch-update" for c in calls(fake_cli)) == 1


async def test_cancelled_write_is_uncertain_and_durable(settings, fake_cli, monkeypatch):
    s = service(settings, fake_cli, create())
    monkeypatch.setenv("FAKE_CLI_MODE", "write_timeout")
    task = asyncio.create_task(s.reply(message()))
    started = Path(fake_cli[0]).with_name("write_started")
    async with asyncio.timeout(3):
        while not started.exists():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    bill = s.sessions[("ou_1", "oc_1")].bills["B1"]
    assert bill.status == "uncertain" and s.journal.get("om_1")["status"] == "started"
    assert "待核对" in await service(settings, fake_cli).reply(message())


async def test_context_scope_expiry_and_ids_cannot_target_other_users(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        create(),
        Decision(action="update", expense={"amount": "25"}),
        create(),
        create(),
    )
    await s.reply(message())
    assert "消息理解失败" in await s.reply(
        message("B1改为25元", message_id="om_2", user_id="ou_other")
    )
    session = s.sessions[("ou_1", "oc_1")]
    session.touched -= settings.context_ttl_seconds + 1
    assert "B1：" in await s.reply(message(message_id="om_3"))
    assert "B2：" in await s.reply(message(message_id="om_4"))
    assert len(written(fake_cli)) == 3


async def test_schema_drift_blocks_all_writes(settings, fake_cli, monkeypatch):
    s = service(settings, fake_cli, create())
    monkeypatch.setenv("FAKE_CLI_MODE", "schema_drift")
    assert "字段不存在或类型不符" in await s.reply(message())
    assert not written(fake_cli)


async def test_local_journal_failure_blocks_write(settings, fake_cli, monkeypatch):
    s = service(settings, fake_cli, create())
    monkeypatch.setattr(s.journal, "save", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    assert "无法保存去重状态" in await s.reply(message())
    assert not written(fake_cli)


async def test_confirmed_write_survives_journal_result_failure(settings, fake_cli, monkeypatch):
    s = service(settings, fake_cli, create())
    save = s.journal.save

    def failed(operation, status, **metadata):
        if status == "success" and "/bill/" in operation:
            raise OSError()
        save(operation, status, **metadata)

    monkeypatch.setattr(s.journal, "save", failed)
    response = await s.reply(message())
    assert "已记账" in response and "本地去重状态保存失败" in response
    assert len(written(fake_cli)) == 1


async def test_two_stage_timeout_has_no_write(settings, fake_cli):
    s = service(settings, fake_cli, create())
    settings.agent_timeout_seconds = 0.01

    async def slow(*args):
        await asyncio.sleep(1)

    s.engine.decide = slow
    assert "超时" in await s.reply(message())
    assert not written(fake_cli)
    assert s.sessions[("ou_1", "oc_1")].next_number == 2


async def test_unchanged_update_checks_remote_but_sends_no_write(settings, fake_cli):
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "18"}))
    await s.reply(message())
    assert "已更新" in await s.reply(message("B1金额改为18元", message_id="om_2"))
    assert not any(c[1] == "+record-batch-update" for c in calls(fake_cli))


async def test_existing_unsuccessful_cannot_be_recreated(settings, fake_cli):
    s = service(
        settings, fake_cli, create(amount=None), Decision(action="update", expense={"amount": "25"})
    )
    await s.reply(message())
    assert "尚未成功写入" in await s.reply(message("B1改为25元", message_id="om_2"))
    assert not written(fake_cli)
