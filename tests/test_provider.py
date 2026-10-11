import json

import httpx2 as httpx
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from test_service import create, message

from bookkeeping_agent.service import BookkeepingService
from bookkeeping_agent.tools import BitableTool


async def test_ark_openai_compatible_structured_decision(settings, fake_cli):
    """Real provider serialization with a mock Ark endpoint; no network or real writes."""
    settings.lark_cli_path, _ = fake_cli
    requests = []

    def handle(request):
        assert str(request.url) == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "test-model"
        names = [tool["function"]["name"] for tool in body["tools"]]
        schema = body["tools"][0]["function"]["parameters"]
        assert {"expense", "preference", "question"} <= set(schema["$defs"]["Decision"]["required"])
        assert set(schema["$defs"]["ExpensePatch"]["required"]) == {
            "date",
            "amount",
            "description",
            "category",
        }
        assert "search_history" not in names
        assert "create_record" not in names and "update_record" not in names
        assert "测试专用系统提示词" in json.dumps(body, ensure_ascii=False)
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
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": names[0],
                                        "arguments": json.dumps(
                                            {"decisions": [create().model_dump(mode="json")]}
                                        ),
                                    },
                                }
                            ],
                        },
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
        service = BookkeepingService(settings, BitableTool(settings), model)
        reply = await service.reply(message())
    assert len(requests) == 1
    assert "已记账" in reply and "18.1元" in reply
