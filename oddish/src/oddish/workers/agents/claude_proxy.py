"""Sandbox-local Anthropic protocol adapter; uploaded alongside Claude Code.

LiteLLM owns conversion of messages, tool calls, streaming events and usage.
The worker pins the upstream model and credentials; request bodies cannot
choose another endpoint or inject LiteLLM connection settings.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os

import litellm
import openai
from aiohttp import web


def create_app(model: str, token: str, effort: str | None = None) -> web.Application:
    async def messages(request: web.Request) -> web.StreamResponse:
        if not hmac.compare_digest(request.headers.get("x-api-key", ""), token):
            raise web.HTTPUnauthorized()
        payload = await request.json()
        if not isinstance(payload, dict) or not isinstance(
            payload.get("messages"), list
        ):
            raise web.HTTPBadRequest(text="messages must be a list")
        # Connection, credentials and model are owned by this trial, never the CLI.
        body = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "messages",
                "system",
                "max_tokens",
                "stream",
                "tools",
                "tool_choice",
                "stop_sequences",
                "temperature",
                "top_p",
                "top_k",
                "thinking",
                "output_config",
            }
        }
        if request.path.endswith("/count_tokens"):
            return web.json_response(
                {
                    "input_tokens": litellm.token_counter(
                        model=model,
                        # A local estimate includes tools and system text. Billing
                        # always uses the provider's actual response usage.
                        text=json.dumps(body),
                    )
                }
            )
        if effort:
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": effort}
        stream = None
        response = None
        try:
            async with asyncio.timeout(600):
                result = await litellm.anthropic.messages.acreate(
                    **body,
                    model=model,
                    num_retries=0,
                    timeout=590,
                    _skip_mcp_handler=True,
                )
                if not body.get("stream"):
                    return web.json_response(result)
                stream = result
                response = web.StreamResponse(
                    headers={"Content-Type": "text/event-stream"}
                )
                await response.prepare(request)
                async for event in stream:
                    data = (
                        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                        if isinstance(event, dict)
                        else event
                    )
                    await response.write(data)
                await response.write_eof()
                return response
        except (openai.APIError, TimeoutError) as exc:
            message = str(exc)
            for key, value in os.environ.items():
                if value and any(part in key for part in ("KEY", "TOKEN", "SECRET")):
                    message = message.replace(value, "[REDACTED]")
            status = getattr(exc, "status_code", 504) or 502
            error = {
                "type": "error",
                "error": {
                    "type": "rate_limit_error" if status == 429 else "api_error",
                    "message": message,
                },
            }
            if response is None:
                return web.json_response(error, status=status)
            await response.write(
                f"event: error\ndata: {json.dumps(error)}\n\n".encode()
            )
            return response
        finally:
            if stream is not None and hasattr(stream, "aclose"):
                await stream.aclose()

    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_post("/v1/messages", messages)
    app.router.add_post("/v1/messages/count_tokens", messages)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ready": True})

    app.router.add_get("/health", health)
    return app


if __name__ == "__main__":
    web.run_app(
        create_app(
            os.environ["ODDISH_PROXY_MODEL"],
            os.environ["ODDISH_PROXY_TOKEN"],
            os.environ.get("ODDISH_PROXY_EFFORT"),
        ),
        host="127.0.0.1",
        port=8787,
        access_log=None,
        print=None,
    )
