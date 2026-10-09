from decimal import Decimal

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from bookkeeping_agent.agent import HELP_TEXT, QueryService, format_records
from bookkeeping_agent.tools import BitableQueryTool, ExpenseRecord


def query_model(n=10):
    def respond(messages, info):
        assert [tool.name for tool in info.function_tools] == ["recent_expenses"]
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("recent_expenses", {"n": n})])
        # Demonstrate that model-generated money and claims never enter the reply.
        return ModelResponse(parts=[TextPart("已写入999元")])

    return FunctionModel(respond)


async def test_real_agent_calls_tool_but_cannot_invent_reply(settings, fake_cli):
    settings.lark_cli_path, _ = fake_cli
    service = QueryService(settings, BitableQueryTool(settings), query_model(2))
    result = await service.reply("看看最近两笔消费")
    assert "实际返回 2 条" in result
    assert result.index("咖啡") < result.index("午饭")
    assert "18.10" in result and "25.00" in result
    assert "999" not in result and "已写入" not in result


async def test_no_tool_cannot_claim_success(settings):
    model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("已删除记录")]))
    result = await QueryService(settings, BitableQueryTool(settings), model).reply("删除全部记录")
    assert result == HELP_TEXT


async def test_invalid_model_argument_does_not_execute(settings, fake_cli):
    settings.lark_cli_path, recorded = fake_cli
    service = QueryService(settings, BitableQueryTool(settings), query_model(101))
    assert await service.reply("最近101条") == HELP_TEXT
    with pytest.raises(FileNotFoundError):
        recorded()


async def test_empty_results(settings, fake_cli, monkeypatch):
    settings.lark_cli_path, _ = fake_cli
    monkeypatch.setenv("FAKE_CLI_MODE", "empty")
    service = QueryService(settings, BitableQueryTool(settings), query_model())
    assert await service.reply("最近消费") == "未找到有记账日期的消费记录。"


def test_timezone_nulls_and_decimal_rendering():
    record = ExpenseRecord(
        record_id="r1",
        date="2026-10-08T18:00:00Z",
        amount=Decimal("25.00"),
        description="午饭\n面条",
    )
    result = format_records([record], 10, "Asia/Shanghai")
    assert "2026-10-09 | 25.00 | 午饭 面条" in result
    assert "实际返回 1 条" in result
    record.amount = None
    assert "金额为空" in format_records([record], 10, "Asia/Shanghai")


async def test_view_scope_reaches_cli_and_reply(settings, fake_cli):
    settings.lark_cli_path, recorded = fake_cli
    settings.bitable_view_id = "vew_test"
    service = QueryService(settings, BitableQueryTool(settings), query_model(2))
    result = await service.reply("最近两笔")
    args = recorded()["args"]
    assert args[args.index("--view-id") + 1] == "vew_test"
    assert "--filter-json" not in args
    assert "查询范围：指定视图（vew_test）" in result
    assert "实际返回 2 条" in result


async def test_empty_view_does_not_fall_back_to_table(settings, fake_cli, monkeypatch):
    settings.lark_cli_path, recorded = fake_cli
    settings.bitable_view_id = "vew_test"
    monkeypatch.setenv("FAKE_CLI_MODE", "empty")
    service = QueryService(settings, BitableQueryTool(settings), query_model())
    assert await service.reply("最近消费") == "指定视图（vew_test）中未找到消费记录。"
    assert "--view-id" in recorded()["args"]
