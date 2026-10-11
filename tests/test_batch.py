"""The same complete-message pipeline for every number of bills and operations."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_service import Decision, bill, create, message, preference, service, written

from bookkeeping_agent.progress import ProgressState, progress_scope
from bookkeeping_agent.state import explicit_numbers
from bookkeeping_agent.tools import QueryError


def lunch(source_text="午饭30元", **kwargs):
    return Decision(
        action="clarify",
        source_text=source_text,
        question="堂食还是外卖？",
        expense={"description": "午饭", "amount": "30"},
        **kwargs,
    )


def dining(source_text="堂食", **kwargs):
    return Decision(
        action="create",
        source_text=source_text,
        continues_pending=True,
        expense={"category": {"level1": "饮食", "level2": "堂食"}},
        **kwargs,
    )


def calls(fake_cli, command):
    path = Path(fake_cli[0]).with_name("calls.jsonl")
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if json.loads(line)["args"][1] == command
    ]


@pytest.mark.parametrize("separator", ["", "，", "。", ",", ";", "；", "\n", "然后"])
async def test_complete_message_no_punctuation_split_one_model_one_batch(
    settings, fake_cli, separator
):
    s = service(
        settings,
        fake_cli,
        [
            create(source_text="咖啡18元"),
            create(
                source_text="停车10元",
                description="停车",
                amount="10",
                category={"level1": "交通通信", "level2": "汽车", "level3": "停车费"},
            ),
        ],
    )
    text = "咖啡18元" + separator + "停车10元"
    response = await s.reply(message(text))
    assert "成功2笔" in response and s.engine.texts == [text]
    assert len(calls(fake_cli, "+record-batch-create")) == 1
    assert len(calls(fake_cli, "+field-list")) == 1
    assert len(written(fake_cli)) == 2


@pytest.mark.parametrize(
    "source,expense,count",
    [
        ("咖啡18元，不对是20元", {"description": "咖啡", "amount": "20"}, 1),
        ("咖啡和面包合计30元", {"description": "咖啡和面包", "amount": "30"}, 1),
        ("买了20个包子20元", {"description": "20个包子", "amount": "20"}, 1),
    ],
)
async def test_in_message_correction_and_single_transaction_goods(
    settings, fake_cli, source, expense, count
):
    s = service(settings, fake_cli, create(**expense))
    await s.reply(message(source))
    assert len(written(fake_cli)) == count and written(fake_cli)[0]["金额"] == int(
        expense["amount"]
    )


async def test_boundary_ambiguity_and_duplicate_sources_block_only_affected(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        [
            create(source_text="咖啡18元"),
            create(source_text="咖啡18元"),
            create(source_text="外卖25元", amount="25", description="外卖"),
        ],
    )
    response = await s.reply(message("咖啡18元外卖25元"))
    assert "重叠或重复" in response and len(written(fake_cli)) == 1
    assert written(fake_cli)[0]["具体"] == "外卖"


async def test_repeated_real_transactions_have_distinct_occurrences(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        [create(source_text="咖啡18元"), create(source_text="咖啡18元", source_occurrence=2)],
    )
    assert "成功2笔" in await s.reply(message("咖啡18元又买咖啡18元"))
    assert len(written(fake_cli)) == 2


async def test_partial_success_combined_supplement_and_correction_fixed_mapping(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        [
            create(source_text="外卖25元", description="外卖", amount="25"),
            lunch(),
            create(source_text="咖啡18元"),
        ],
        [
            dining(),
            Decision(
                action="update",
                source_text="第一笔金额是20元",
                target_number=1,
                expense={"amount": "20"},
            ),
        ],
    )
    response = await s.reply(message("外卖25元午饭30元咖啡18元"))
    assert "成功2笔，待补充1笔" in response and response.splitlines()[-1].startswith("3. ")
    response = await s.reply(message("堂食第一笔金额是20元", message_id="om_2"))
    assert "成功2笔" in response and "｜咖啡｜18.1元" not in response
    assert [r["金额"] for r in written(fake_cli)] == [20, 18.1, 30]
    assert s.engine.contexts[-1]["displayed_items"][0]["expense"]["amount"] == "25"


async def test_batch_supplement_once_and_ambiguous_no_number(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        [
            lunch(),
            Decision(
                action="clarify",
                source_text="晚饭40元",
                expense={"description": "晚饭", "amount": "40"},
                question="堂食还是外卖？",
            ),
        ],
        dining(),
        [
            dining(source_text="第一笔是堂食", target_number=1),
            dining(source_text="第二笔是堂食", target_number=2),
        ],
    )
    await s.reply(message("午饭30元晚饭40元"))
    assert "有多笔账单待补充" in await s.reply(message("堂食", message_id="om_2"))
    response = await s.reply(message("第一笔是堂食第二笔是堂食", message_id="om_3"))
    assert "成功2笔" in response and len(written(fake_cli)) == 2
    assert len(calls(fake_cli, "+record-batch-create")) == 1


async def test_cross_day_multiple_rounds_and_explicit_date_change(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        [
            lunch(),
            Decision(
                action="clarify",
                source_text="晚饭",
                expense={"description": "晚饭"},
                question="多少钱？",
            ),
        ],
        dining(target_number=1),
        Decision(
            action="clarify",
            target_number=2,
            continues_pending=True,
            expense={"amount": "45"},
            question="堂食还是外卖？",
        ),
        dining(target_number=1),
        Decision(action="update", target_number=1, expense={"date": "2026-10-10"}),
    )
    await s.reply(message("午饭30元晚饭", sent_at="2026-10-09T23:59:00+08:00"))
    await s.reply(message("第一笔是堂食", message_id="om_2", sent_at="2026-10-11T00:01:00+08:00"))
    await s.reply(message("第二笔45元", message_id="om_3", sent_at="2026-10-11T00:02:00+08:00"))
    await s.reply(message("第一笔是堂食", message_id="om_4", sent_at="2026-10-11T00:03:00+08:00"))
    assert all(r["记账日期"].startswith("2026-10-09") for r in written(fake_cli))
    await s.reply(
        message("第一笔日期是昨天", message_id="om_5", sent_at="2026-10-11T12:00:00+08:00")
    )
    assert written(fake_cli)[1]["记账日期"].startswith("2026-10-10")
    assert written(fake_cli)[1]["金额"] == 45


async def test_natural_reference_unique_and_unknown_cross_session_target(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        [
            lunch(),
            Decision(
                action="clarify",
                source_text="晚饭40元",
                expense={"description": "晚饭", "amount": "40"},
                question="堂食还是外卖？",
            ),
        ],
    )
    await s.reply(message("午饭30元晚饭40元"))
    target = bill(s, 1)
    s.engine.plans = iter(
        [
            dining(target_id=target.bill_id, target_reference="晚饭"),
            dining(target_id=target.bill_id, target_reference="晚饭"),
        ]
    )
    assert "晚饭｜40元" in await s.reply(message("晚饭是堂食", message_id="om_2"))
    response = await s.reply(message("晚饭是堂食", message_id="om_3", user_id="stranger"))
    assert "目标不属于" in response and len(written(fake_cli)) == 1


async def test_ambiguous_natural_reference_cannot_pick_bill(settings, fake_cli):
    s = service(settings, fake_cli, [lunch(), lunch(source_text="午饭40元")])
    await s.reply(message("午饭30元午饭40元"))
    s.engine.plans = iter([dining(target_id=bill(s).bill_id, target_reference="午饭")])
    assert "多个可能" in await s.reply(message("午饭是堂食", message_id="om_2"))
    assert not written(fake_cli)


async def test_multiple_updates_and_mixed_new_preference(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        [
            create(source_text="咖啡10元", amount="10"),
            create(source_text="外卖11元", amount="11", description="外卖"),
        ],
        [
            Decision(
                action="update",
                source_text="第二笔改为20元",
                target_number=2,
                expense={"amount": "20"},
            ),
            Decision(
                action="update",
                source_text="第一笔改为21元",
                target_number=1,
                expense={"amount": "21"},
            ),
            create(source_text="豆浆5元", description="豆浆", amount="5"),
            preference(),
        ],
    )
    await s.reply(message("咖啡10元外卖11元"))
    response = await s.reply(
        message("第二笔改为20元第一笔改为21元豆浆5元以后豆浆都归食品", message_id="om_2")
    )
    assert "成功3笔" in response and "已保存分类偏好" in response
    assert [r["金额"] for r in written(fake_cli)] == [21, 20, 5]
    assert len(s.knowledge.preferences()) == 1
    assert len(calls(fake_cli, "+record-batch-create")) == 2
    assert len(calls(fake_cli, "+record-batch-update")) == 2


@pytest.mark.parametrize(
    "target_number,text,expected",
    [
        (2, "第一笔金额改成20元", "编号识别不一致"),
        (99, "那笔金额改成20元", "最新列表没有第 99 笔"),
        (1, "之前列表第一笔金额改成20元", "编号只对应最新一次回复"),
    ],
)
async def test_invalid_number_no_fallback(settings, fake_cli, target_number, text, expected):
    s = service(
        settings,
        fake_cli,
        [create(source_text="咖啡10元", amount="10"), create(source_text="咖啡11元", amount="11")],
        Decision(action="update", target_number=target_number, expense={"amount": "20"}),
    )
    await s.reply(message("咖啡10元咖啡11元"))
    assert expected in await s.reply(message(text, message_id="om_2"))
    assert [r["金额"] for r in written(fake_cli)] == [10, 11]


async def test_latest_mapping_shrinks_then_ignore_clears(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        [create(source_text="咖啡10元", amount="10"), create(source_text="咖啡11元", amount="11")],
        Decision(action="update", target_number=2, expense={"amount": "20"}),
        Decision(action="update", target_number=2, expense={"amount": "21"}),
        Decision(action="ignore"),
        Decision(action="update", target_number=1, expense={"amount": "22"}),
    )
    await s.reply(message("咖啡10元咖啡11元"))
    await s.reply(message("第二笔改20元", message_id="om_2"))
    assert "没有第 2 笔" in await s.reply(message("第二笔改21元", message_id="om_3"))
    await s.reply(message("第一笔明天天气如何", message_id="om_4"))
    assert "没有第 1 笔" in await s.reply(message("第一笔改22元", message_id="om_5"))
    assert [r["金额"] for r in written(fake_cli)] == [10, 20]


@pytest.mark.parametrize("action", ["ignore", "create", "update", "clarify"])
async def test_reference_alone_never_authorizes_update(settings, fake_cli, action):
    d = Decision(
        action=action,
        target_number=1,
        **({"expense": {"amount": "20"}} if action in {"create", "update"} else {}),
        **(
            {"question": "要修改什么？", "clarification_scope": "target"}
            if action == "clarify"
            else {}
        ),
    )
    s = service(settings, fake_cli, create(), d)
    await s.reply(message())
    await s.reply(message("第一笔", message_id="om_2"))
    assert written(fake_cli)[0]["金额"] == 18.1 and len(written(fake_cli)) == 1
    assert bill(s).status == "success"


@pytest.mark.parametrize("mode", ["missing_id", "duplicate_ids", "partial_ids", "ignored_fields"])
async def test_unreliable_batch_response_never_assigns_wrong_ids(
    settings, fake_cli, monkeypatch, mode
):
    s = service(
        settings,
        fake_cli,
        [create(source_text="咖啡10元", amount="10"), create(source_text="咖啡11元", amount="11")],
    )
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    response = await s.reply(message("咖啡10元咖啡11元"))
    assert "成功" not in response and "待核对2笔" in response
    assert all(b.status == "uncertain" for b in s.sessions[("ou_1", "oc_1")].bills.values())
    assert len(written(fake_cli)) == 2 and len(calls(fake_cli, "+record-batch-create")) == 1
    assert await s.reply(message("咖啡10元咖啡11元")) is None


@pytest.mark.parametrize("mode", ["partial_visible", "duplicate_rows", "missing_file"])
async def test_create_response_confirms_without_readback_and_preserves_correction(
    settings, fake_cli, monkeypatch, mode
):
    s = service(
        settings,
        fake_cli,
        [create(source_text="咖啡10元", amount="10"), create(source_text="咖啡11元", amount="11")],
        Decision(action="update", target_number=2, expense={"amount": "30"}),
    )
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    response = await s.reply(message("咖啡10元咖啡11元"))
    assert "成功2笔" in response and "待核对" not in response
    assert not calls(fake_cli, "+record-get")
    bills = list(s.sessions[("ou_1", "oc_1")].bills.values())
    assert [b.record_id for b in bills] == [r["record_id"] for r in written(fake_cli)]
    assert all(b.status == "success" for b in bills)
    assert await s.reply(message("咖啡10元咖啡11元")) is None
    monkeypatch.delenv("FAKE_CLI_MODE")
    assert "已修改" in await s.reply(message("第二笔改30元", message_id="om_2"))
    assert len(calls(fake_cli, "+record-get")) == 2
    assert len(calls(fake_cli, "+record-batch-create")) == 1
    assert [r["金额"] for r in written(fake_cli)] == [10, 30]


async def test_independent_failed_update_does_not_block_new_or_preference(
    settings, fake_cli, monkeypatch
):
    s = service(
        settings,
        fake_cli,
        create(),
        [
            create(source_text="外卖25元", amount="25"),
            Decision(
                action="update",
                source_text="第一笔改20元",
                target_number=1,
                expense={"amount": "20"},
            ),
            preference(),
        ],
    )
    await s.reply(message())
    get = s.tool.get_record

    async def external(record_id):
        result = await get(record_id)
        result["金额"] = 999
        return result

    monkeypatch.setattr(s.tool, "get_record", external)
    response = await s.reply(message("外卖25元第一笔改20元以后豆浆都归食品", message_id="om_2"))
    assert (
        "成功1笔，待处理1笔" in response and "外部修改" in response and "已保存分类偏好" in response
    )
    assert len(written(fake_cli)) == 2


async def test_schema_failure_does_not_block_preference(settings, fake_cli, monkeypatch):
    s = service(settings, fake_cli, [create(source_text="咖啡18元"), preference()])
    monkeypatch.setenv("FAKE_CLI_MODE", "schema_drift")
    response = await s.reply(message("咖啡18元以后豆浆都归食品"))
    assert "已保存分类偏好" in response and "待处理1笔" in response and not written(fake_cli)


async def test_cancellation_reserves_all_batch_bills_across_restart(
    settings, fake_cli, monkeypatch
):
    s = service(
        settings,
        fake_cli,
        [create(source_text="咖啡10元", amount="10"), create(source_text="咖啡11元", amount="11")],
    )
    monkeypatch.setenv("FAKE_CLI_MODE", "write_timeout")
    task = asyncio.create_task(s.reply(message("咖啡10元咖啡11元")))
    marker = Path(fake_cli[0]).with_name("write_started")
    for _ in range(300):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(b.status == "uncertain" for b in s.sessions[("ou_1", "oc_1")].bills.values())
    assert "待核对" in await service(settings, fake_cli).reply(message("咖啡10元咖啡11元"))
    assert len(calls(fake_cli, "+record-batch-create")) == 1
    assert not Path(fake_cli[1]()["cwd"]).exists()


async def test_interrupted_after_create_preserves_success_and_unexecuted_update(
    settings, fake_cli, monkeypatch
):
    s = service(
        settings,
        fake_cli,
        create(),
        [
            create(source_text="外卖25元", amount="25"),
            Decision(
                action="update",
                source_text="第一笔改20元",
                target_number=1,
                expense={"amount": "20"},
            ),
        ],
    )
    await s.reply(message())
    started = asyncio.Event()

    async def pause(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(s.tool, "get_record", pause)
    state = ProgressState()
    with progress_scope(state.on_event):
        task = asyncio.create_task(s.reply(message("外卖25元第一笔改20元", message_id="om_2")))
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "成功1笔，待处理1笔" in state.completed_response
    assert len(written(fake_cli)) == 2 and bill(s).status == "failed"
    assert "待核对" in await service(settings, fake_cli).reply(
        message("外卖25元第一笔改20元", message_id="om_2")
    )


async def test_same_target_twice_requires_clarification(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        create(),
        [
            Decision(
                action="update",
                source_text="第一笔改20元",
                target_number=1,
                expense={"amount": "20"},
            ),
            Decision(
                action="update",
                source_text="第一笔改21元",
                target_number=1,
                expense={"amount": "21"},
            ),
        ],
    )
    await s.reply(message())
    assert "同一账单" in await s.reply(message("第一笔改20元第一笔改21元", message_id="om_2"))
    assert written(fake_cli)[0]["金额"] == 18.1


async def test_success_never_recreated_by_wrong_supplement_action(settings, fake_cli):
    s = service(settings, fake_cli, create(), create(target_number=1))
    await s.reply(message())
    assert "已记入" in await s.reply(message("第一笔20元", message_id="om_2"))
    assert len(written(fake_cli)) == 1


async def test_model_cannot_inherit_amount_into_new_bill(settings, fake_cli):
    s = service(settings, fake_cli, lunch(), create(amount="30"))
    await s.reply(message("午饭30元"))
    response = await s.reply(message("堂食", message_id="om_2"))
    assert "有效金额" in response and not written(fake_cli)


async def test_empty_expense_pending_retains_source_and_anchor(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        Decision(action="clarify", expense=None, question="堂食还是外卖？"),
        create(
            amount="30",
            description="午饭",
            target_number=1,
            category={"level1": "饮食", "level2": "堂食"},
        ),
    )
    await s.reply(message("午饭30元", sent_at="2026-10-09T23:59:00+08:00"))
    await s.reply(message("堂食", message_id="om_2", sent_at="2026-10-11T12:00:00+08:00"))
    assert written(fake_cli)[0]["金额"] == 30 and written(fake_cli)[0]["记账日期"].startswith(
        "2026-10-09"
    )


async def test_pending_update_supplement_does_not_create_and_cancel_restores_original(
    settings, fake_cli
):
    s = service(
        settings,
        fake_cli,
        create(),
        Decision(
            action="clarify", target_number=1, expense={"amount": "20"}, question="改哪一类？"
        ),
        Decision(action="ignore", cancel_pending=True),
        Decision(action="update", expense={"amount": "21"}),
    )
    await s.reply(message())
    await s.reply(message("第一笔金额改20元分类也改", message_id="om_2"))
    assert bill(s).status == "pending" and bill(s).pending_action == "update"
    await s.reply(message("取消这次修改", message_id="om_3"))
    assert bill(s).facts["amount"] == "18.10" and bill(s).status == "success"
    await s.reply(message("刚才金额改21元", message_id="om_4"))
    assert len(written(fake_cli)) == 1 and written(fake_cli)[0]["金额"] == 21


async def test_card_results_rows_aggregate_actual_confirmations(settings, fake_cli):
    s = service(
        settings, fake_cli, [create(source_text="咖啡18元", description="咖啡\n99. 文字"), lunch()]
    )
    state = ProgressState()
    state.start()
    with progress_scope(state.on_event):
        response = await s.reply(message("咖啡18元午饭30元"))
    state.finish(response, submitted=True)
    assert state.status == "部分完成 · 待补充" and len(response.splitlines()) == 3
    assert "咖啡 99. 文字" in response
    elements = state.snapshot()["body"]["elements"]
    assert elements[-3]["text"]["content"] == "结果：成功1笔，待补充1笔"
    assert elements[-1]["text"]["content"].startswith("2. ")
    assert len(state.steps) == len({s.key for s in state.steps})


@pytest.mark.parametrize(
    "source,number",
    [
        ("第二笔账单", 2),
        ("第二个", 2),
        ("第 ２ 笔", 2),
        ("2号", 2),
        ("2的金额改20元", 2),
        ("请把第二笔金额改20元", 2),
        ("第一百零二笔", 102),
        ("买了20个包子20元", None),
        ("2026年10月11日咖啡20元", None),
        ("第二杯咖啡18元", None),
    ],
)
def test_number_language_and_goods_quantity(source, number):
    assert explicit_numbers(source) == (set() if number is None else {number})


async def test_pending_update_completes_with_earlier_authorization(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        create(),
        Decision(
            action="clarify", target_number=1, expense={"amount": "20"}, question="改成哪类？"
        ),
        Decision(
            action="update",
            continues_pending=True,
            expense={"category": {"level1": "饮食", "level2": "堂食"}},
        ),
    )
    await s.reply(message())
    await s.reply(message("第一笔金额改20元分类也改", message_id="om_2"))
    response = await s.reply(message("堂食", message_id="om_3"))
    assert "已修改" in response and written(fake_cli)[0]["金额"] == 20
    assert written(fake_cli)[0]["二级分类"] == ["堂食"] and len(written(fake_cli)) == 1


async def test_missing_relative_date_fact_keeps_raw_origin_for_later_parse(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        Decision(action="clarify", question="堂食还是外卖？"),
        create(
            continues_pending=True,
            date="2026-10-08",
            amount="30",
            description="午饭",
            category={"level1": "饮食", "level2": "堂食"},
        ),
    )
    await s.reply(message("昨天午饭30元", sent_at="2026-10-09T23:59:00+08:00"))
    assert "date" not in bill(s).facts
    await s.reply(message("堂食", message_id="om_2", sent_at="2026-10-11T12:00:00+08:00"))
    pending = s.engine.contexts[-1]["pending_items"][0]
    assert pending["text"] == "昨天午饭30元" and pending["sent_at"].startswith("2026-10-09")
    assert written(fake_cli)[0]["记账日期"].startswith("2026-10-08")


async def test_uncertain_creates_allow_independent_existing_update_and_preference(
    settings, fake_cli, monkeypatch
):
    s = service(
        settings,
        fake_cli,
        create(),
        [
            create(source_text="外卖25元", amount="25"),
            Decision(
                action="update",
                source_text="第一笔金额改20元",
                target_number=1,
                expense={"amount": "20"},
            ),
            preference(),
        ],
    )
    await s.reply(message())
    monkeypatch.setattr(s.tool, "create_records", AsyncMock(side_effect=QueryError("结果待核对")))
    response = await s.reply(message("外卖25元第一笔金额改20元以后豆浆都归食品", message_id="om_2"))
    assert "成功1笔，待核对1笔" in response and "已保存分类偏好" in response
    assert written(fake_cli)[0]["金额"] == 20


async def test_cancel_multiple_pending_in_one_message(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        [
            lunch(),
            Decision(
                action="clarify",
                source_text="晚饭40元",
                expense={"description": "晚饭", "amount": "40"},
                question="堂食还是外卖？",
            ),
        ],
        [
            Decision(
                action="ignore", cancel_pending=True, target_number=1, source_text="取消第一笔"
            ),
            Decision(
                action="ignore", cancel_pending=True, target_number=2, source_text="取消第二笔"
            ),
        ],
    )
    await s.reply(message("午饭30元晚饭40元"))
    response = await s.reply(message("取消第一笔取消第二笔", message_id="om_2"))
    assert "已取消" in response and not written(fake_cli)
    assert all(b.status == "cancelled" for b in s.sessions[("ou_1", "oc_1")].bills.values())


async def test_preflight_failed_create_can_be_explicitly_completed_without_duplicates(
    settings, fake_cli, monkeypatch
):
    s = service(settings, fake_cli, create(), create(continues_pending=True))
    monkeypatch.setenv("FAKE_CLI_MODE", "schema_drift")
    assert "已记账" not in await s.reply(message())
    assert bill(s).status == "failed"
    monkeypatch.delenv("FAKE_CLI_MODE")
    assert "已记账" in await s.reply(message("这笔咖啡18元重新记", message_id="om_2"))
    assert len(written(fake_cli)) == 1 and len(calls(fake_cli, "+record-batch-create")) == 1


async def test_cancel_and_supplement_same_bill_require_final_intent(settings, fake_cli):
    s = service(
        settings,
        fake_cli,
        lunch(),
        [
            Decision(
                action="ignore", cancel_pending=True, target_number=1, source_text="取消第一笔"
            ),
            dining(source_text="第一笔是堂食", target_number=1),
        ],
    )
    await s.reply(message("午饭30元"))
    response = await s.reply(message("取消第一笔第一笔是堂食", message_id="om_2"))
    assert "同一账单出现多个操作" in response
    assert "已取消" not in response and bill(s).status == "pending" and not written(fake_cli)
