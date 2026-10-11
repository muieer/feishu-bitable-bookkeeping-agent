import json
import re
import shutil
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from test_service import Decision, create, message, service, written

from bookkeeping_agent.agent_tracing import TracedHTTPClient
from bookkeeping_agent.request_budget import RequestBudget, RequestBudgetExceeded
from bookkeeping_agent.service import BookkeepingService
from bookkeeping_agent.tools import BitableTool
from bookkeeping_agent.tracing import LOG_FAILED, LOG_FAILED_SUFFIX, MessageTrace, TraceStore, emit


def records(path):
    return [json.loads(block) for block in re.split(r"\n=== [^\n]+ ===\n", path.read_text())[1:]]


def logfile(settings, message_id="om_1"):
    for path in sorted((settings.message_log_dir / "messages").glob("*.log")):
        if any(r["message_id"] == message_id for r in records(path)):
            return path
    return TraceStore(settings).new(message_id).path


def fail_at(monkeypatch, stage):
    original = MessageTrace.emit

    def failing(self, name, **kwargs):
        if name == stage:
            self.failed = True
            return False
        return original(self, name, **kwargs)

    monkeypatch.setattr(MessageTrace, "emit", failing)


def test_removed_directory_restored_before_raw_callback(settings):
    store = TraceStore(settings)
    shutil.rmtree(store.directory)
    trace = store.received({"event": {"message": {"message_id": "om_removed_directory"}}})
    assert not trace.failed and store.claim(trace.message_id) is trace
    assert records(trace.path)[0]["stage"] == "message.raw_received"


@pytest.mark.parametrize("remove_directory", [False, True])
def test_deleted_active_log_resumes_without_recreating_old_events(settings, remove_directory):
    store = TraceStore(settings)
    trace = store.new("om_active_log")
    assert trace.emit("before_cleanup")
    if remove_directory:
        shutil.rmtree(store.directory)
    else:
        trace.path.unlink()
    assert trace.emit("after_cleanup", durable=True)
    trace.require()
    assert not trace.failed
    events = records(trace.path)
    assert len(events) == 1 and events[0]["stage"] == "after_cleanup"
    assert events[0]["sequence"] == 2


async def test_removed_directory_allows_normal_business_after_recovery(settings, fake_cli):
    s = service(settings, fake_cli, create())
    shutil.rmtree(s.traces.directory)
    response = await s.reply(message())
    assert "已记账" in response and LOG_FAILED not in response
    assert len(written(fake_cli)) == 1 and await s.reply(message()) is None


@pytest.mark.parametrize(
    "stage",
    [
        "message.normalized",
        "decision.context",
        "message.plan",
        "expense.validated",
        "table.write_planned",
        "journal.reserved",
    ],
)
async def test_logging_failure_before_submission_stops_write(
    settings, fake_cli, monkeypatch, stage
):
    s = service(settings, fake_cli, create())
    fail_at(monkeypatch, stage)
    assert await s.reply(message()) == LOG_FAILED and not written(fake_cli)


@pytest.mark.parametrize("stage", ["table.confirmed", "business.completed", "message.completed"])
async def test_logging_failure_after_submission_retains_real_result(
    settings, fake_cli, monkeypatch, stage
):
    s = service(settings, fake_cli, create())
    fail_at(monkeypatch, stage)
    response = await s.reply(message())
    assert "已记账" in response and LOG_FAILED_SUFFIX in response
    assert len(written(fake_cli)) == 1 and await s.reply(message()) is None


async def test_post_submission_log_failure_continues_response_confirmation(
    settings, fake_cli, monkeypatch
):
    s = service(settings, fake_cli, create(), Decision(action="update", expense={"amount": "25"}))
    await s.reply(message())
    original = MessageTrace.emit

    def failing(self, stage, **kwargs):
        if self.mutation_submitted and stage == "cli.result":
            self.failed = True
            return False
        return original(self, stage, **kwargs)

    monkeypatch.setattr(MessageTrace, "emit", failing)
    response = await s.reply(message("改为25元", message_id="om_2"))
    assert "已修改" in response and LOG_FAILED_SUFFIX in response
    assert written(fake_cli)[0]["金额"] == 25
    assert s.journal.get("om_2")["status"] == "success"


async def test_unusable_log_directory_and_secret_redaction(settings, fake_cli):
    store = TraceStore(settings)
    trace = store.new("../../unsafe")
    assert trace.path.parent == settings.message_log_dir.resolve() / "messages"
    with store.scope(trace):
        emit(
            "test",
            text="中文" * 2000,
            amount=__import__("decimal").Decimal("0.29"),
            headers={"Authorization": "Bearer secret-value"},
            api_key="key-value",
            value=settings.ark_api_key.get_secret_value(),
        )
    contents = trace.path.read_text()
    assert "中文" * 2000 in contents and "0.29" in contents
    assert (
        "secret-value" not in contents
        and "key-value" not in contents
        and "test-key" not in contents
    )
    s = service(settings, fake_cli, create())
    for path in s.traces.directory.iterdir():
        path.unlink()
    s.traces.directory.rmdir()
    s.traces.directory.write_text("invalid directory")
    assert await s.reply(message()) == LOG_FAILED and not written(fake_cli)


async def test_full_http_trace_protocol_retry_counter_and_budget(settings, fake_cli):
    settings.lark_cli_path, _ = fake_cli
    attempts = []

    def handle(request):
        attempts.append(request)
        return httpx.Response(
            500, json={"error": {"message": "busy"}}, headers={"retry-after-ms": "1"}
        )

    counter = RequestBudget(settings.llm_http_budget_path, 2)
    async with TracedHTTPClient(
        request_budget=counter, transport=httpx.MockTransport(handle)
    ) as client:
        sdk = AsyncOpenAI(api_key="test-key", http_client=client, max_retries=2)
        model = OpenAIChatModel("test-model", provider=OpenAIProvider(openai_client=sdk))
        s = BookkeepingService(settings, BitableTool(settings), model)
        assert "未执行写入" in await s.reply(message())
    assert len(attempts) == 2 and json.loads(counter.path.read_text())["requests"] == 2
    events = records(logfile(settings))
    assert any(r["stage"] == "llm.http_retry" for r in events)
    assert any(r["stage"] == "agent.run_error" for r in events)
    assert any(r["stage"] == "agent.message_history" for r in events)
    assert not written(fake_cli)
    with pytest.raises(RequestBudgetExceeded):
        RequestBudget(counter.path, 2).reserve()


async def test_zero_budget_blocks_before_transport(settings):
    network = AsyncMock(return_value=httpx.Response(200))
    counter = RequestBudget(settings.llm_http_budget_path, 0)
    async with TracedHTTPClient(
        request_budget=counter, transport=httpx.MockTransport(network)
    ) as client:
        with pytest.raises(RequestBudgetExceeded):
            await client.post("https://example.invalid/chat/completions", json={})
    network.assert_not_awaited()
    assert not counter.path.exists()


async def test_budget_counts_failed_attempts_and_persists_restart(settings):
    async def failed(request):
        raise httpx.ConnectError("offline")

    budget = RequestBudget(settings.llm_http_budget_path, 1)
    async with TracedHTTPClient(
        request_budget=budget, transport=httpx.MockTransport(failed)
    ) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post("https://example.invalid/chat/completions")
    assert json.loads(budget.path.read_text())["requests"] == 1
    with pytest.raises(RequestBudgetExceeded):
        RequestBudget(budget.path, 1).reserve()


def test_budget_has_absolute_cap_across_clients(settings):
    for _ in range(15):
        RequestBudget(settings.llm_http_budget_path, 15).reserve()
    with pytest.raises(RequestBudgetExceeded):
        RequestBudget(settings.llm_http_budget_path, 15).reserve()
    assert json.loads(settings.llm_http_budget_path.read_text())["requests"] == 15


async def test_wire_body_and_parsed_plan_match_log(settings, fake_cli):
    settings.lark_cli_path, _ = fake_cli
    bodies = []
    output = {"decisions": [create(amount="0.29").model_dump(mode="json")]}

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert not body.get("stream")
        assert len(body["tools"]) == 1
        return httpx.Response(
            200,
            json={
                "id": "completion",
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

    async with TracedHTTPClient(transport=httpx.MockTransport(handle)) as client:
        model = OpenAIChatModel(
            "test-model", provider=OpenAIProvider(api_key="test-key", http_client=client)
        )
        s = BookkeepingService(settings, BitableTool(settings), model)
        assert "0.29元" in await s.reply(message())
    events = records(logfile(settings))
    request = next(r for r in events if r["stage"] == "llm.http_request")
    assert json.loads(request["details"]["body"]) == bodies[0]
    stages = {r["stage"] for r in events}
    assert {
        "agent.model_request",
        "agent.model_response",
        "agent.output_validate.end",
        "agent.message_history",
    } <= stages
    assert "test-key" not in logfile(settings).read_text()


async def test_budget_counts_http_redirects_before_each_transport(settings):
    attempts = []

    def handle(request):
        attempts.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://example.invalid/redirect"})

    budget = RequestBudget(settings.llm_http_budget_path, 1)
    async with TracedHTTPClient(
        request_budget=budget, transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        with pytest.raises(RequestBudgetExceeded):
            await client.post("https://example.invalid/chat/completions")
    assert len(attempts) == 1 and json.loads(budget.path.read_text())["requests"] == 1
