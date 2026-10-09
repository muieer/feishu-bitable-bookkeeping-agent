import json

import httpx2 as httpx
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from bookkeeping_agent.agent import QueryService
from bookkeeping_agent.tools import BitableQueryTool


async def test_openai_compatible_request_and_tool_round_trip(settings, fake_cli):
    """Check real provider serialization against a simulated Ark endpoint."""
    settings.lark_cli_path, _ = fake_cli
    requests = []

    def handle(request):
        assert str(request.url) == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "test-model"
        if len(requests) == 1:
            assert body["tools"][0]["function"]["name"] == "recent_expenses"
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "recent_expenses", "arguments": '{"n":2}'},
                    }
                ],
            }
            reason = "tool_calls"
        else:
            assert any(item["role"] == "tool" for item in body["messages"])
            assert "咖啡" not in request.content.decode()
            assert "18.1" not in request.content.decode()
            message = {"role": "assistant", "content": "查询完成"}
            reason = "stop"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": reason,
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = OpenAIChatModel(
            settings.ark_model,
            provider=OpenAIProvider(
                base_url=settings.ark_base_url,
                api_key=settings.ark_api_key.get_secret_value(),
                http_client=client,
            ),
        )
        service = QueryService(settings, BitableQueryTool(settings), model)
        reply = await service.reply("查询最近两条消费记录")
    assert len(requests) == 2
    assert "实际返回 2 条" in reply and "咖啡" in reply
