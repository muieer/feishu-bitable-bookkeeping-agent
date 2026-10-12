"""Mixed decisions, per-bill failures and ordered business feedback."""

from unittest.mock import AsyncMock

from test_service import Decision, calls, create, message, service, written


async def test_mixed_new_update_clarify_are_grouped_in_required_order(settings, fake_cli):
    plans = [
        Decision(action="update", bill_id="B1", raw="B1改为20元", expense={"amount": "20"}),
        create(
            raw="午餐堂食25元",
            amount="25",
            description="午餐堂食",
            category={"level1": "饮食", "level2": "堂食", "level3": None},
        ),
        Decision(action="clarify", raw="咖啡12元", reason="分类无法确定"),
    ]
    s = service(settings, fake_cli, create(), plans)
    await s.reply(message())
    result = await s.reply(message("B1改为20元。午餐堂食25元。咖啡12元", message_id="om_2"))
    assert (
        result.index("已记账消费记录")
        < result.index("已更新消费记录")
        < result.index("以下消费记录未写入")
    )
    assert "B2：2026/10/09｜饮食｜堂食｜午餐堂食｜25元" in result
    assert "B1：2026/10/09｜饮食｜咖啡｜咖啡｜20元" in result
    assert "B3：咖啡12元。原因：分类无法确定" in result
    assert len(written(fake_cli)) == 2


async def test_bad_amount_does_not_discard_valid_sibling_and_statuses_are_distinct(
    settings, fake_cli
):
    s = service(
        settings,
        fake_cli,
        [
            create(raw="咖啡18元"),
            create(raw="咖啡0元", amount="0"),
            Decision(action="clarify", raw="超市20元", reason="未说明购买内容"),
        ],
    )
    response = await s.reply(message("咖啡18元。咖啡0元。超市20元"))
    assert "B1：" in response and "B2：" in response and "B3：" in response
    assert (
        "已记账消费记录" in response and "执行失败" in response and "以下消费记录未写入" in response
    )
    assert len(written(fake_cli)) == 1


async def test_partial_batch_confirmation_preserves_known_successes(settings, fake_cli):
    s = service(settings, fake_cli, [create(raw="咖啡18元"), create(raw="咖啡25元", amount="25")])
    s.tool.create_records = AsyncMock(return_value=["rec_1", None])
    result = await s.reply(message("咖啡18元。咖啡25元"))
    assert "已记账消费记录" in result and "待核对" in result
    assert s.sessions[("ou_1", "oc_1")].bills["B1"].status == "success"
    assert s.sessions[("ou_1", "oc_1")].bills["B2"].status == "uncertain"
    assert await s.reply(message()) is None
    s.tool.create_records.assert_awaited_once()


async def test_external_update_failure_does_not_block_independent_create(
    settings, fake_cli, monkeypatch
):
    s = service(
        settings,
        fake_cli,
        create(),
        [
            Decision(action="update", raw="B1改为20元", expense={"amount": "20"}),
            create(raw="咖啡25元", amount="25"),
        ],
    )
    await s.reply(message())
    monkeypatch.setenv("FAKE_CLI_MODE", "external_edit")
    result = await s.reply(message("B1改为20元。咖啡25元", message_id="om_2"))
    assert "B2：" in result and "已记账消费记录" in result and "账单已被外部修改" in result
    assert len(written(fake_cli)) == 2
    assert not any(c[1] == "+record-batch-update" for c in calls(fake_cli))


async def test_ids_sort_numerically_within_group_and_tertiary_shown(settings, fake_cli):
    rows = [create(raw=f"咖啡{n}元", amount=str(n)) for n in range(1, 12)]
    rows[1] = create(
        raw="汽车停车2元",
        amount="2",
        description="汽车停车",
        category={"level1": "交通通信", "level2": "汽车", "level3": "停车费"},
    )
    s = service(settings, fake_cli, rows)
    response = await s.reply(message("。".join(row.raw for row in rows)))
    assert response.index("B2：") < response.index("B10：") < response.index("B11：")
    assert "交通通信｜汽车｜停车费｜汽车停车｜2元" in response
    assert len(written(fake_cli)) == 11


async def test_description_punctuation_cannot_become_a_success_warning(settings, fake_cli):
    description = '咖啡。面包 **大份** <at id="all">\n热饮'
    s = service(settings, fake_cli, create(description=description))
    response = await s.reply(message())
    assert response.count("咖啡。面包") == 1
    assert response.count("热饮") == 1 and ' <at id="all"> 热饮｜18元' in response


async def test_duplicate_remote_ids_leave_every_batch_item_uncertain(
    settings, fake_cli, monkeypatch
):
    s = service(settings, fake_cli, [create(raw="咖啡18元"), create(raw="咖啡25元", amount="25")])
    monkeypatch.setenv("FAKE_CLI_MODE", "duplicate_ids")
    response = await s.reply(message("咖啡18元。咖啡25元"))
    assert "已记账消费记录" not in response
    assert "待核对" in response and "B1：" in response and "B2：" in response
    assert len(written(fake_cli)) == 2
    assert await s.reply(message()) is None
