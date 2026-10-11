from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from test_service import Decision, create, message, written

from bookkeeping_agent.agent import DecisionEngine
from bookkeeping_agent.knowledge import Knowledge, KnowledgeError
from bookkeeping_agent.models import MessagePlan
from bookkeeping_agent.service import BookkeepingService
from bookkeeping_agent.tools import BitableTool


def decision_model(value, observer=None):
    def respond(messages, info):
        if observer:
            observer(messages, info)
        if "decisions" in value:
            output = value
        else:
            # Test-only fixture binding, not an application compatibility layer.
            text = next(
                part.content
                for m in messages
                for part in m.parts
                if part.part_kind == "user-prompt"
            )
            output = {
                "decisions": [Decision(**{**value, "source_text": text}).model_dump(mode="json")]
            }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    return FunctionModel(respond)


def engine(settings, fake_cli, model):
    settings.lark_cli_path, _ = fake_cli
    return DecisionEngine(settings, BitableTool(settings), Knowledge(settings), model)


async def test_nonexpense_one_run_no_function_tools(settings, fake_cli):
    observed = []

    def inspect(messages, info):
        observed.append(messages)
        assert not info.function_tools
        assert "咖啡饮品" in repr(messages) and "测试专用系统提示词" in repr(messages)

    e = engine(settings, fake_cli, decision_model({"action": "ignore"}, inspect))
    result = await e.decide("明天打算喝咖啡", {"default_date": "2026-10-09"})
    assert result.decisions[0].action == "ignore" and len(observed) == 1
    assert not e.tool.settings.llm_http_budget_path.exists()


@pytest.mark.parametrize("missing", [False, True])
async def test_external_system_prompt_required(settings, fake_cli, missing):
    settings.agent_system_prompt_path.write_text("")
    if missing:
        settings.agent_system_prompt_path.unlink()
    with pytest.raises(KnowledgeError):
        engine(settings, fake_cli, decision_model({"action": "ignore"}))


async def test_entire_message_multiple_intents_one_inference(settings, fake_cli):
    text = "咖啡18元外卖25元"
    output = MessagePlan(
        decisions=[
            create(source_text="咖啡18元", decision_id="a"),
            create(source_text="外卖25元", amount="25", decision_id="b"),
        ]
    )
    calls = []
    e = engine(
        settings,
        fake_cli,
        decision_model(output.model_dump(mode="json"), lambda m, i: calls.append(m)),
    )
    result = await e.decide(text, {})
    assert len(result.decisions) == 2 and len(calls) == 1
    assert any(
        part.part_kind == "user-prompt" and part.content == text
        for m in calls[0]
        for part in m.parts
    )


async def test_history_deduplicates_keywords_and_freezes_completed_decisions(settings, fake_cli):
    first = MessagePlan(
        decisions=[
            create(source_text="外卖25元", decision_id="fixed", amount="25"),
            create(
                source_text="咖啡18元",
                decision_id="a",
                category=None,
                needs_history=True,
                history_keywords=["咖啡"],
            ),
            create(
                source_text="另一杯咖啡20元",
                decision_id="b",
                amount="20",
                category=None,
                needs_history=True,
                history_keywords=["咖啡"],
            ),
        ]
    )
    second = MessagePlan(
        decisions=[
            create(source_text="咖啡18元", decision_id="a"),
            create(source_text="另一杯咖啡20元", decision_id="b", amount="20"),
        ]
    )
    calls = []

    def respond(messages, info):
        assert not info.function_tools
        calls.append(messages)
        output = first if len(calls) == 1 else second
        if len(calls) == 2:
            assert '"history_cases"' in repr(messages) and '"fixed"' not in repr(messages)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, output.model_dump(mode="json"))]
        )

    e = engine(settings, fake_cli, FunctionModel(respond))
    e.tool.history = AsyncMock(
        return_value=[
            {"具体": "咖啡", "一级分类": ["饮食"], "二级分类": ["咖啡"], "三级分类": None}
        ]
    )
    result = await e.decide("外卖25元咖啡18元另一杯咖啡20元", {})
    assert len(calls) == 2 and result.decisions[0] == first.decisions[0]
    assert all(not d.needs_history for d in result.decisions)
    e.tool.history.assert_awaited_once_with("咖啡")


@pytest.mark.parametrize(
    "corruption",
    ["amount", "date", "description", "target", "extra", "missing", "error", "still_history"],
)
async def test_history_cannot_change_facts_or_membership_and_preserves_ready_bill(
    settings, fake_cli, corruption
):
    initial = MessagePlan(
        decisions=[
            create(source_text="咖啡18元", decision_id="ready"),
            create(
                source_text="豆浆5元",
                decision_id="waiting",
                amount="5",
                description="豆浆",
                category=None,
                needs_history=True,
                history_keywords=["豆浆"],
            ),
        ]
    )
    candidate = create(source_text="豆浆5元", decision_id="waiting", amount="5", description="豆浆")
    if corruption in {"amount", "date", "description"}:
        patch = {"amount": "6", "date": "2026-10-08", "description": "编造"}[corruption]
        candidate.expense = type(candidate.expense).model_validate(
            {**candidate.expense.model_dump(), corruption: patch}
        )
    elif corruption == "target":
        candidate.target_number = 1
    elif corruption == "still_history":
        candidate.needs_history, candidate.history_keywords = True, ["豆浆"]
    candidates = [candidate]
    if corruption == "extra":
        candidates.append(initial.decisions[0])
    if corruption == "missing":
        candidates = [initial.decisions[0]]
    outputs = iter([initial, MessagePlan(decisions=candidates)])

    def respond(messages, info):
        if corruption == "error" and '"phase": "history"' in repr(messages):
            raise RuntimeError("provider failure")
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, next(outputs).model_dump(mode="json"))]
        )

    e = engine(settings, fake_cli, FunctionModel(respond))
    e.tool.history = AsyncMock(return_value=[])
    result = await e.decide("咖啡18元豆浆5元", {})
    assert result.decisions[0] == initial.decisions[0]
    assert result.decisions[1].action == "clarify"
    assert result.decisions[1].expense == initial.decisions[1].expense


async def test_history_read_error_and_invalid_paths_become_question(settings, fake_cli):
    initial = create(category=None, needs_history=True, history_keywords=["咖啡"])
    final = Decision(action="clarify", expense=initial.expense, question="请说明用途。")
    outputs = iter([initial, final])

    def respond(messages, info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"decisions": [next(outputs).model_dump(mode="json")]},
                )
            ]
        )

    e = engine(settings, fake_cli, FunctionModel(respond))
    e.tool.history = AsyncMock(side_effect=RuntimeError("read failed"))
    result = await e.decide("咖啡18元", {})
    assert result.decisions[0].action == "clarify"


async def test_unparseable_response_exhaustion_never_writes(settings, fake_cli):
    e = engine(settings, fake_cli, decision_model({"decisions": [{"action": "create"}]}))
    with pytest.raises(UnexpectedModelBehavior):
        await e.decide("咖啡18元", {})


async def test_source_must_appear_and_occurrence_must_exist(settings, fake_cli):
    e = engine(
        settings,
        fake_cli,
        decision_model({"decisions": [create(source_text="编造消费1元").model_dump(mode="json")]}),
    )
    with pytest.raises(UnexpectedModelBehavior):
        await e.decide("咖啡18元", {})


@pytest.mark.parametrize(
    "field", ["expense", "preference", "question", "date", "amount", "description", "category"]
)
def test_output_payload_fields_are_required_even_when_nullable(field):
    output = create().model_dump(mode="json")
    if field in {"expense", "preference", "question"}:
        del output[field]
    else:
        del output["expense"][field]
    with pytest.raises(ValidationError) as error:
        MessagePlan.model_validate({"decisions": [output]})
    assert any(e["type"] == "missing" and e["loc"][-1] == field for e in error.value.errors())


@pytest.mark.parametrize("recover", [False, True])
async def test_missing_consumption_payload_retry_and_mixed_partial_result(
    settings, fake_cli, recover
):
    # Reproduce the production response: three create decisions omitted expense,
    # although the source fragments and routing fields were present.
    text = "买蔬菜花费 199 元。商场停车花费 199 元。超市话费 199 元。我好像很开心。"
    corrected = [
        create(
            decision_id="veg",
            source_text="买蔬菜花费 199 元",
            amount="199",
            description="买蔬菜",
            category={"level1": "饮食", "level2": "食品"},
        ),
        create(
            decision_id="parking",
            source_text="商场停车花费 199 元",
            amount="199",
            description="商场停车",
            category={"level1": "交通通信", "level2": "汽车", "level3": "停车费"},
        ),
        Decision(
            decision_id="phone",
            action="clarify",
            source_text="超市话费 199 元",
            expense={"amount": "199", "description": "超市话费"},
            question="是话费充值，还是超市购物？",
        ),
        Decision(decision_id="chatter", action="ignore", source_text="我好像很开心。"),
    ]
    valid = {"decisions": [d.model_dump(mode="json") for d in corrected]}
    broken = {"decisions": [dict(d) for d in valid["decisions"]]}
    for n, d in enumerate(broken["decisions"]):
        for key in ("expense", "preference", "question"):
            del d[key]
        d["action"] = "create" if n < 3 else "ignore"
    calls = []

    def respond(messages, info):
        calls.append(messages)
        schema = info.output_tools[0].parameters_json_schema
        assert "expense" in schema["$defs"]["Decision"]["required"]
        if len(calls) == 2:
            assert any(p.part_kind == "retry-prompt" for m in messages for p in m.parts)
        output = valid if recover and len(calls) == 2 else broken
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    e = engine(settings, fake_cli, FunctionModel(respond))
    s = BookkeepingService(settings, e.tool, engine=e)
    response = await s.reply(message(text))
    assert len(calls) == 2
    if recover:
        assert "成功2笔，待补充1笔" in response and "是话费充值" in response
        assert len(written(fake_cli)) == 2
    else:
        assert "记账信息不完整或格式不正确" in response and "网络连接" not in response
        assert not written(fake_cli)
        assert not Path(fake_cli[0]).with_name("args.json").exists()


@pytest.mark.parametrize("slow_phase", ["query", "refinement"])
async def test_history_timeout_retains_first_phase_ready_decisions(settings, fake_cli, slow_phase):
    import asyncio

    settings.agent_timeout_seconds = 0.1
    initial = MessagePlan(
        decisions=[
            create(source_text="咖啡18元", decision_id="ready"),
            create(
                source_text="豆浆5元",
                decision_id="waiting",
                amount="5",
                description="豆浆",
                category=None,
                needs_history=True,
                history_keywords=["豆浆"],
            ),
        ]
    )
    calls = 0

    async def respond(messages, info):
        nonlocal calls
        calls += 1
        if calls == 2:
            await asyncio.Event().wait()
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, initial.model_dump(mode="json"))]
        )

    e = engine(settings, fake_cli, FunctionModel(respond))

    async def query(*args):
        if slow_phase == "query":
            await asyncio.Event().wait()
        return []

    e.tool.history = query
    result = await e.decide("咖啡18元豆浆5元", {})
    assert result.decisions[0] == initial.decisions[0]
    assert result.decisions[1].action == "clarify"
