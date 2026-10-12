import json

import httpx2 as httpx
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from test_agent import recognition
from test_service import create, message, written

from bookkeeping_agent.service import BookkeepingService
from bookkeeping_agent.tools import BitableTool


def completion(body, output):
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
                                    "name": body["tools"][0]["function"]["name"],
                                    "arguments": json.dumps(output),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        },
    )


async def test_ark_openai_compatible_two_structured_stages(settings, fake_cli):
    """Real provider serialization against MockTransport, with no remote I/O."""
    settings.lark_cli_path, _ = fake_cli
    requests = []

    def handle(request):
        assert str(request.url) == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "test-model" and len(body["tools"]) == 1
        assert not body.get("stream")
        schema = body["tools"][0]["function"]["parameters"]
        if len(requests) == 1:
            assert set(schema["required"]) == {"items"}
            assert "ExpensePatch" not in schema["$defs"]
            assert "咖啡饮品" not in json.dumps(body, ensure_ascii=False)
            output = recognition("咖啡18元")
        else:
            assert set(schema["required"]) == {"decisions"}
            assert set(schema["$defs"]["ExpensePatch"]["required"]) == {
                "date",
                "amount",
                "description",
                "category",
            }
            for name in ["CreateDecision", "UpdateDecision", "ClarifyDecision"]:
                assert set(schema["$defs"][name]["required"]) == {
                    "bill_id",
                    "action",
                    "raw",
                    "expense",
                    "reason",
                }
                assert schema["$defs"][name]["additionalProperties"] is False
            assert "咖啡饮品" in json.dumps(body, ensure_ascii=False)
            output = {"decisions": [create().model_dump(mode="json")]}
        return completion(body, output)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = OpenAIChatModel(
            settings.ark_model,
            provider=OpenAIProvider(
                base_url=settings.ark_base_url,
                api_key=settings.ark_api_key.get_secret_value(),
                http_client=client,
            ),
        )
        s = BookkeepingService(settings, BitableTool(settings), model)
        reply = await s.reply(message())
    assert len(requests) == 2 and "已记账" in reply and "18元" in reply
    assert len(written(fake_cli)) == 1
