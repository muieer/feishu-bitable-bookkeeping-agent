"""Offline model protocol, schema and prompt tests; never call a remote model."""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from test_service import create, message, written

from bookkeeping_agent.agent import DecisionEngine, validate_plan, validate_recognition
from bookkeeping_agent.knowledge import Knowledge, KnowledgeError
from bookkeeping_agent.models import (
    Decision,
    ExpensePatch,
    IdentifiedItem,
    MessagePlan,
    RecognitionResult,
)
from bookkeeping_agent.service import BookkeepingService
from bookkeeping_agent.tools import BitableTool


def recognition(*rows):
    return {
        "items": [
            {"type": "new", "target_id": None, "raw": row} if isinstance(row, str) else row
            for row in rows
        ]
    }


def decision_model(value, observer=None):
    """Use a FunctionModel to return real tool-call envelopes for both stages."""

    def respond(messages, info):
        if observer:
            observer(messages, info)
        assert not info.function_tools
        schema = info.output_tools[0].parameters_json_schema
        if "items" in schema["properties"]:
            text = next(
                p.content for m in messages for p in m.parts if p.part_kind == "user-prompt"
            )
            output = recognition() if value is None else recognition(text)
        else:
            output = value if "decisions" in value else {"decisions": [value]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    return FunctionModel(respond)


def sequence_model(outputs, calls=None):
    values = iter(outputs)

    def respond(messages, info):
        assert not info.function_tools
        if calls is not None:
            calls.append((messages, info))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, next(values))])

    return FunctionModel(respond)


def engine(settings, fake_cli, model):
    settings.lark_cli_path, _ = fake_cli
    return DecisionEngine(settings, Knowledge(settings), model)


async def test_fixed_two_stages_full_knowledge_only_second_stage(settings, fake_cli):
    observed = []
    model = sequence_model(
        [recognition("咖啡18元"), {"decisions": [create().model_dump(mode="json")]}], observed
    )
    s = BookkeepingService(settings, BitableTool(settings), model)
    settings.lark_cli_path, _ = fake_cli
    assert "已记账" in await s.reply(message())
    assert len(observed) == 2
    first, second = (repr(row[0]) for row in observed)
    assert "消费记录识别器" in first and "咖啡饮品" not in first
    assert "消费决策器" in second and "咖啡饮品" in second
    assert "compatible_categories_version" in second and "B1" in second
    assert not settings.llm_http_budget_path.exists()


async def test_nonexpense_only_recognition(settings, fake_cli):
    calls = []
    model = sequence_model([recognition()], calls)
    settings.lark_cli_path, _ = fake_cli
    s = BookkeepingService(settings, BitableTool(settings), model)
    assert await s.reply(message("明天想喝咖啡")) == "此次不包含消费行为。"
    assert len(calls) == 1 and not written(fake_cli)


async def test_recognition_retry_is_bounded_and_grounded(settings, fake_cli):
    calls = []
    e = engine(
        settings,
        fake_cli,
        sequence_model([recognition("虚构咖啡18元"), recognition("咖啡18元")], calls),
    )
    result = await e.recognize("咖啡18元", {"allowed_items": []})
    assert result.items[0].raw == "咖啡18元" and len(calls) == 2
    assert "raw 必须来自原消息" in repr(calls[-1][0])


async def test_recognition_repeated_invalid_output_never_reaches_decision(settings, fake_cli):
    calls = []
    settings.lark_cli_path, _ = fake_cli
    model = sequence_model([recognition("虚构18元"), recognition("虚构18元")], calls)
    s = BookkeepingService(settings, BitableTool(settings), model)
    assert "消息理解失败" in await s.reply(message())
    assert len(calls) == 2 and not written(fake_cli)


async def test_decision_retry_preserves_number_and_raw(settings, fake_cli):
    calls = []
    good = create().model_dump(mode="json")
    bad = {**good, "bill_id": "B2"}
    settings.lark_cli_path, _ = fake_cli
    s = BookkeepingService(
        settings,
        BitableTool(settings),
        sequence_model(
            [
                recognition("咖啡18元"),
                {"decisions": [bad]},
                {"decisions": [good]},
            ],
            calls,
        ),
    )
    assert "已记账" in await s.reply(message()) and len(calls) == 3
    assert "不得增删或合并" in repr(calls[-1][0])


async def test_both_stages_retry_once_total_four_model_requests(settings, fake_cli):
    good = create().model_dump(mode="json")
    calls = []
    settings.lark_cli_path, _ = fake_cli
    s = BookkeepingService(
        settings,
        BitableTool(settings),
        sequence_model(
            [
                recognition("不在原文"),
                recognition("咖啡18元"),
                {"decisions": [{**good, "raw": "改写原文"}]},
                {"decisions": [good]},
            ],
            calls,
        ),
    )
    assert "已记账" in await s.reply(message()) and len(calls) == 4


async def test_stage_two_invalid_output_twice_fails_without_writing(settings, fake_cli):
    calls = []
    bad = {"decisions": [{**create().model_dump(mode="json"), "action": "preference"}]}
    settings.lark_cli_path, _ = fake_cli
    s = BookkeepingService(
        settings,
        BitableTool(settings),
        sequence_model(
            [
                recognition("咖啡18元"),
                bad,
                bad,
            ],
            calls,
        ),
    )
    assert "消息理解失败" in await s.reply(message())
    assert len(calls) == 3 and not written(fake_cli)


async def test_no_free_text_or_model_tools(settings, fake_cli):
    from pydantic_ai.messages import TextPart

    def respond(messages, info):
        assert not info.function_tools
        return ModelResponse(parts=[TextPart("已记账")])

    e = engine(settings, fake_cli, FunctionModel(respond))
    with pytest.raises(UnexpectedModelBehavior):
        await e.recognize("咖啡18元", {})


@pytest.mark.parametrize("phase", ["recognition", "decision"])
@pytest.mark.parametrize("missing", [False, True])
async def test_each_external_prompt_required(settings, fake_cli, tmp_path, phase, missing):
    path = tmp_path / f"{phase}.md"
    path.write_text("")
    if missing:
        path.unlink()
    setattr(settings, f"{phase}_prompt_path", path)
    with pytest.raises(KnowledgeError):
        engine(settings, fake_cli, decision_model(None))


@pytest.mark.parametrize(
    "value",
    [
        {"items": [{"type": "new", "raw": "咖啡18元"}]},
        {"items": [{"type": "new", "target_id": "B1", "raw": "咖啡18元"}]},
        {"items": [{"type": "existing", "target_id": None, "raw": "咖啡18元"}]},
        {"items": [{"type": "new", "target_id": None, "raw": 123}]},
        {"items": [], "explanation": "无消费"},
        {"items": [{"type": "new", "target_id": None, "raw": "咖啡18元", "category": "咖啡"}]},
    ],
)
def test_stage_one_closed_schema(value):
    with pytest.raises(ValidationError):
        RecognitionResult.model_validate(value)


def test_all_nullable_fields_are_required_in_both_schemas():
    schema = MessagePlan.model_json_schema()
    for name in ["CreateDecision", "UpdateDecision", "ClarifyDecision"]:
        definition = schema["$defs"][name]
        assert set(definition["required"]) == {"bill_id", "action", "raw", "expense", "reason"}
        assert definition["additionalProperties"] is False
    for name, required in [
        ("ExpensePatch", {"date", "amount", "description", "category"}),
        ("CategoryPath", {"level1", "level2", "level3"}),
    ]:
        assert set(schema["$defs"][name]["required"]) == required
        assert schema["$defs"][name]["additionalProperties"] is False
    recognition_schema = RecognitionResult.model_json_schema()
    for name in ("NewRecognitionItem", "ExistingRecognitionItem"):
        definition = recognition_schema["$defs"][name]
        assert set(definition["required"]) == {"type", "target_id", "raw"}
        assert definition["additionalProperties"] is False
    assert (
        recognition_schema["$defs"]["NewRecognitionItem"]["properties"]["target_id"]["type"]
        == "null"
    )
    assert (
        recognition_schema["$defs"]["ExistingRecognitionItem"]["properties"]["target_id"]["type"]
        == "string"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"action": "ignore"},
        {"action": "preference"},
        {"bill_id": "bill_1"},
        {"expense": None},
        {"reason": "写入吧"},
        {"extra": True},
        {"raw": " "},
    ],
)
def test_decision_closed_union(change):
    with pytest.raises(ValidationError):
        TypeAdapter(Decision).validate_python({**create().model_dump(mode="json"), **change})


@pytest.mark.parametrize(
    "change",
    [
        {"date": 1791475200},
        {"date": "2026-10-09T00:00:00"},
        {"amount": True},
        {"description": 12},
        {"category": {"level1": "饮食", "level2": "咖啡"}},
    ],
)
def test_patch_rejects_coercions_and_missing_nulls(change):
    with pytest.raises(ValidationError):
        ExpensePatch.model_validate(
            {"date": None, "amount": None, "description": None, "category": None, **change}
        )


def test_clarify_cannot_carry_facts_or_empty_reason():
    for change in [{"expense": create().expense}, {"reason": ""}, {"reason": None}]:
        with pytest.raises(ValidationError):
            TypeAdapter(Decision).validate_python(
                {
                    "bill_id": "B1",
                    "action": "clarify",
                    "raw": "咖啡18元",
                    "expense": None,
                    "reason": "依据不足",
                    **change,
                }
            )


@pytest.mark.parametrize(
    "rows",
    [
        [{"type": "existing", "target_id": "B2", "raw": "B2改为20元"}],
        [{"type": "new", "target_id": None, "raw": "B1改为20元"}],
        [{"type": "existing", "target_id": "B1", "raw": "B1改为20元"}] * 2,
        [{"type": "existing", "target_id": "B1", "raw": "B2改为20元"}],
    ],
)
def test_recognition_target_validation(rows):
    with pytest.raises(ValueError):
        validate_recognition(
            RecognitionResult(items=rows),
            "B1改为20元 B2改为20元",
            {"allowed_items": [{"bill_id": "B1"}]},
        )


def test_normalized_currency_and_repeated_real_purchases():
    result = RecognitionResult.model_validate(recognition("咖啡18元", "咖啡18元"))
    validate_recognition(result, "咖啡 ¥18。下午咖啡18￥", {})
    with pytest.raises(ValueError):
        validate_recognition(result, "咖啡18元", {})


def test_decision_membership_raw_and_type_are_program_validated():
    item = IdentifiedItem(bill_id="B1", type="new", raw="咖啡18元")
    for change in [{"bill_id": "B2"}, {"raw": "咖啡20元"}, {"action": "update"}]:
        with pytest.raises(ValueError):
            validate_plan(
                MessagePlan(decisions=[{**create().model_dump(mode="json"), **change}]), [item]
            )
    with pytest.raises(ValidationError):
        MessagePlan(decisions=[create(), create()])


async def test_second_stage_related_context_only_and_knowledge_retains_all_rules(
    settings, fake_cli
):
    calls = []
    e = engine(
        settings,
        fake_cli,
        sequence_model(
            [
                {
                    "decisions": [
                        {
                            "bill_id": "B1",
                            "action": "update",
                            "raw": "B1改为25元",
                            "expense": {
                                "date": None,
                                "amount": 25,
                                "description": None,
                                "category": None,
                            },
                            "reason": None,
                        }
                    ]
                }
            ],
            calls,
        ),
    )
    context = {
        "message_sent_at": "2026-10-09T10:00:00+08:00",
        "default_date": "2026-10-09",
        "timezone": "Asia/Shanghai",
        "allowed_items": [
            {"bill_id": "B1", "expense": create().expense.model_dump(mode="json")},
            {"bill_id": "B2", "raw": "无关且敏感的旧消费"},
        ],
    }
    assert not hasattr(e, "tool")
    before = deepcopy(context)
    await e.decide([IdentifiedItem(bill_id="B1", type="existing", raw="B1改为25元")], context)
    sent = repr(calls[0][0])
    assert "无关且敏感的旧消费" not in sent
    assert '"bill_id": "B2"' not in sent
    assert "咖啡饮品" in sent and "给父母购买" in sent and context == before
