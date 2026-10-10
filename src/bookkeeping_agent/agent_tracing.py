"""Observe Pydantic AI hooks and the wire without enabling response streaming."""

import time

import httpx2
from pydantic_ai._http import (
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
)
from pydantic_ai.capabilities import Capability
from pydantic_ai.messages import RetryPromptPart
from pydantic_ai.models import get_user_agent

from .tracing import emit, require, run_state


class TracedHTTPClient(httpx2.AsyncClient):
    def __init__(self, **kwargs):
        kwargs.setdefault("timeout", httpx2.Timeout(DEFAULT_HTTP_TIMEOUT, connect=5))
        kwargs.setdefault(
            "limits",
            httpx2.Limits(
                max_connections=DEFAULT_MAX_CONNECTIONS,
                max_keepalive_connections=DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
            ),
        )
        kwargs.setdefault("headers", {"User-Agent": get_user_agent()})
        super().__init__(**kwargs)

    async def send(self, request, **kwargs):
        state = run_state()
        if state is not None:
            state["http_attempt"] += 1
        require()
        sdk_retry = request.headers.get("x-stainless-retry-count", "0")
        if sdk_retry != "0":
            emit(
                "llm.http_retry",
                retry=sdk_retry,
                previous_outcome=state.get("last_http_outcome") if state else None,
            )
            require()
        body = await request.aread()
        emit(
            "llm.http_request",
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            body=body,
            options=kwargs,
        )
        require()
        started = time.monotonic()
        try:
            response = await super().send(request, **kwargs)
            # Current application uses non-streaming completions. aread() caches the body.
            await response.aread()
            if state is not None:
                state["last_http_outcome"] = {"status_code": response.status_code}
            emit(
                "llm.http_response",
                status_code=response.status_code,
                headers=dict(response.headers),
                request_id=response.headers.get("x-request-id"),
                body=response.content,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            require()
            return response
        except BaseException as exc:
            if state is not None:
                state["last_http_outcome"] = {"error_type": type(exc).__name__}
            emit(
                "llm.http_error",
                status="error",
                error=exc,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            raise


class AgentTraceCapability(Capability):
    async def wrap_run(self, ctx, *, handler):
        return await self._observe(
            "agent.lifecycle",
            handler,
            no_arg=True,
            pydantic_run_id=ctx.run_id,
            usage=ctx.usage,
        )

    async def _observe(self, stage, handler, value=None, *, no_arg=False, **details):
        started = time.monotonic()
        emit(stage + ".start", **details)
        require()
        try:
            result = await handler() if no_arg else await handler(value)
        except BaseException as exc:
            emit(
                stage + ".error",
                status="error",
                error=exc,
                duration_ms=(time.monotonic() - started) * 1000,
                **details,
            )
            raise
        emit(
            stage + ".end",
            result=result,
            duration_ms=(time.monotonic() - started) * 1000,
            **details,
        )
        require()
        return result

    async def prepare_tools(self, ctx, tool_defs):
        emit("agent.function_tools", tools=tool_defs, retry=ctx.retry, max_retries=ctx.max_retries)
        require()
        return tool_defs

    async def prepare_output_tools(self, ctx, tool_defs):
        state = run_state()
        if state is not None:
            state["output_retry"] = ctx.retry
            state["output_retry_limit"] = ctx.max_retries
        emit("agent.output_tools", tools=tool_defs, retry=ctx.retry, max_retries=ctx.max_retries)
        require()
        return tool_defs

    async def wrap_node_run(self, ctx, *, node, handler):
        result = await self._observe(
            "agent.node",
            handler,
            node,
            node=type(node).__name__,
            run_step=ctx.run_step,
            usage=ctx.usage,
        )
        emit("agent.transition", from_node=type(node).__name__, to_node=type(result).__name__)
        request = getattr(result, "request", None)
        if request:
            for part in request.parts:
                if isinstance(part, RetryPromptPart):
                    state = run_state()
                    if state is not None:
                        state["observed_retry_feedback_count"] = (
                            state.get("observed_retry_feedback_count", 0) + 1
                        )
                    emit(
                        "agent.retry_feedback",
                        feedback=part,
                        retry_kind=(
                            "function_tool"
                            if part.tool_name == "search_history"
                            else "output_validation"
                            if part.tool_name
                            else "model_output"
                        ),
                        # Generic node RunContext has default retry=0/max_retries=0.
                        # The preparation hook supplies the real budget snapshot for this request.
                        retry_before_request=state.get("output_retry") if state else None,
                        max_retries=state.get("output_retry_limit") if state else None,
                        tool_call_id=part.tool_call_id,
                    )
                elif getattr(part, "part_kind", "") == "tool-return":
                    emit("agent.tool_feedback", feedback=part, tool_call_id=part.tool_call_id)
        return result

    async def before_model_request(self, ctx, request_context):
        state = run_state()
        if state is not None:
            state["loop"] += 1
        emit(
            "agent.model_request",
            request_context=request_context,
            retry=ctx.retry,
            max_retries=ctx.max_retries,
            usage=ctx.usage,
        )
        require()
        return request_context

    async def after_model_request(self, ctx, *, request_context, response):
        emit("agent.model_response", response=response, usage=ctx.usage)
        require()
        return response

    async def on_model_request_error(self, ctx, *, request_context, error):
        emit("agent.model_error", status="error", error=error, request_context=request_context)
        raise error

    async def wrap_tool_validate(self, ctx, *, call, tool_def, args, handler):
        return await self._observe(
            "agent.tool_validate",
            handler,
            args,
            call=call,
            tool=tool_def,
            tool_call_id=call.tool_call_id,
            tool_kind="function",
            retry=ctx.retry,
            max_retries=ctx.max_retries,
            raw_args=args,
        )

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        return await self._observe(
            "agent.tool_execute",
            handler,
            args,
            call=call,
            tool=tool_def,
            tool_call_id=call.tool_call_id,
            tool_kind="function",
            retry=ctx.retry,
            max_retries=ctx.max_retries,
            args=args,
        )

    async def wrap_output_validate(self, ctx, *, output_context, output, handler):
        return await self._observe(
            "agent.output_validate",
            handler,
            output,
            output_context=output_context,
            tool_kind="output" if output_context.tool_call else "text_output",
            tool_call_id=(
                output_context.tool_call.tool_call_id if output_context.tool_call else None
            ),
            raw_output=output,
            retry=ctx.retry,
            max_retries=ctx.max_retries,
        )

    async def wrap_output_process(self, ctx, *, output_context, output, handler):
        return await self._observe(
            "agent.output_process",
            handler,
            output,
            output_context=output_context,
            output=output,
            retry=ctx.retry,
            max_retries=ctx.max_retries,
            tool_kind="output" if output_context.tool_call else "text_output",
            tool_call_id=(
                output_context.tool_call.tool_call_id if output_context.tool_call else None
            ),
        )
