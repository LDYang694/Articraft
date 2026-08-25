from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
import websockets
from websockets.exceptions import WebSocketException

from articraft.errors import ModelError
from articraft.settings import Settings, get_settings

_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_MAX_OUTPUT_TOKENS = 128_000
_RETRY_BASE_SECONDS = 0.5
_RETRY_MAX_SECONDS = 20.0
_WEBSOCKET_OPEN_TIMEOUT_SECONDS = 20.0
# Codex caps the 1.05M API window at 272K to avoid much higher cost and lower quality.
_CODEX_CONTEXT_WINDOW_TOKENS = 272_000
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ModelSpec:
    context_window_tokens: int
    input_price: float
    cached_input_price: float
    output_price: float
    cache_write_price: float | None = None


# Prices are USD per million tokens: input, cached input, output, cache write.
_MODELS = {
    "gpt-5.6-sol": _ModelSpec(_CODEX_CONTEXT_WINDOW_TOKENS, 5.0, 0.5, 30.0, 6.25),
    "gpt-5.6-terra": _ModelSpec(_CODEX_CONTEXT_WINDOW_TOKENS, 2.0, 0.2, 12.0, 2.5),
    "gpt-5.6-luna": _ModelSpec(_CODEX_CONTEXT_WINDOW_TOKENS, 0.2, 0.02, 1.2, 0.25),
}
_MODEL_ALIASES = {"gpt-5.6": "gpt-5.6-sol"}
KNOWN_MODELS = tuple(sorted((*_MODELS, *_MODEL_ALIASES)))


def responses_http_url(base_url: str | None) -> str:
    root = (base_url or _DEFAULT_OPENAI_BASE_URL).strip() or _DEFAULT_OPENAI_BASE_URL
    parsed = urlparse(root)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    if not path.endswith("/responses"):
        if not path.endswith("/v1"):
            path = f"{path}/v1"
        path = f"{path}/responses"
    return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", ""))


def responses_websocket_url(base_url: str | None) -> str:
    parsed = urlparse(responses_http_url(base_url))
    scheme = "wss" if parsed.scheme != "http" else "ws"
    return urlunparse((scheme, parsed.netloc, parsed.path, "", "", ""))


class OpenAIModel:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.config = settings or get_settings()
        if not self.config.openai_api_key:
            raise ModelError("OpenAI credentials are required. Set OPENAI_API_KEY.")
        # An injected client is closed with the model. It exists for tests.
        self._client = client
        self._websocket: Any = None
        self._input_items: list[dict[str, Any]] = []
        self._last_message_count = 0
        self._previous_response_id: str | None = None

    def _uses_http(self) -> bool:
        """Whether to send plain HTTP instead of the chained websocket.

        A custom base URL means a proxy, and a proxy is not assumed to keep
        response state, so every request carries the whole conversation.
        """
        return bool((self.config.openai_base_url or "").strip())

    @property
    def context_window_tokens(self) -> int:
        return context_window_tokens_for(self.config.openai_model) or 0

    async def query(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Query the OpenAI Responses API and return the completed text response."""
        new_items = self._new_input_items(messages)
        chained = self._previous_response_id is not None and not self._uses_http()
        if chained:
            input_items = new_items
            previous_response_id = self._previous_response_id
        else:
            input_items = [*self._input_items, *new_items]
            previous_response_id = None

        request = self._request(messages, input_items, previous_response_id, tools)
        fallback_request = self._request(
            messages,
            [*self._input_items, *new_items],
            None,
            tools,
        )

        response = await self._send_with_retries(request, fallback_request)
        text = _response_text(response)
        tool_calls = _response_tool_calls(response)
        _raise_for_bad_status(response, text)
        if not text and not tool_calls and not _response_has_reasoning(response):
            raise ModelError("OpenAI response did not contain output_text")

        self._input_items.extend(new_items)
        self._input_items.extend(_response_output(response))
        self._previous_response_id = _response_id(response)
        self._last_message_count = len(messages)
        return {
            "text": text,
            "tool_calls": tool_calls,
            "token_usage": _response_token_usage(response),
            "cost": _response_cost(response),
            "response": response,
        }

    async def summarize_context(
        self,
        messages: list[dict[str, Any]],
        *,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        """Create a plain checkpoint without extending the active response chain."""

        input_items = [
            _input_message(message, self.config.openai_model)
            for message in messages
            if "type" not in message and message["role"] != "system"
        ]
        request = self._request(messages, input_items, None, None)
        request["max_output_tokens"] = max_output_tokens
        response = await self._send_with_retries(request, request)
        text = _response_text(response)
        _raise_for_bad_status(response, text)
        if not text:
            raise ModelError("OpenAI summary response did not contain output_text")

        # The agent replaces its old messages with the checkpoint after this
        # call. Reset the incremental state so the next query sends that new
        # message list in full instead of continuing the old response chain.
        self._input_items.clear()
        self._previous_response_id = None
        self._last_message_count = 0
        return {
            "text": text,
            "token_usage": _response_token_usage(response),
            "cost": _response_cost(response),
        }

    async def close(self) -> None:
        await self._close_websocket()
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _request(
        self,
        messages: list[dict[str, Any]],
        input_items: list[dict[str, Any]],
        previous_response_id: str | None,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.config.openai_model,
            "input": input_items,
            "reasoning": {"effort": self.config.openai_reasoning_effort},
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": _MAX_OUTPUT_TOKENS,
            "store": False,
        }
        if previous_response_id is not None:
            request["previous_response_id"] = previous_response_id
        if tools:
            request["tools"] = tools
            request["parallel_tool_calls"] = True
        instructions = _instructions(messages)
        if instructions:
            request["instructions"] = instructions
        return request

    async def _send_with_retries(
        self,
        request: dict[str, Any],
        fallback_request: dict[str, Any],
    ) -> dict[str, Any]:
        for attempt in range(1, self.config.openai_max_attempts + 1):
            try:
                return await asyncio.wait_for(
                    self._send_with_fallback(request, fallback_request),
                    timeout=self.config.openai_request_timeout_seconds,
                )
            except Exception as exc:
                if attempt >= self.config.openai_max_attempts or not _should_retry(exc):
                    raise
                await self._close_websocket()
                delay = random.random() * min(
                    _RETRY_MAX_SECONDS,
                    _RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                )
                logger.warning(
                    "OpenAI request failed (attempt %s/%s), retrying in %.2fs: %s",
                    attempt,
                    self.config.openai_max_attempts,
                    delay,
                    _format_exception(exc),
                )
                await asyncio.sleep(delay)
        raise AssertionError("retry loop did not return or raise")

    async def _send_with_fallback(
        self,
        request: dict[str, Any],
        fallback_request: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return await self._send_once(request)
        except _OpenAIRequestError as exc:
            if exc.code != "previous_response_not_found" or "previous_response_id" not in request:
                raise
            logger.warning("OpenAI lost the previous response; resending the full conversation")
            return await self._send_once(fallback_request, force_reconnect=True)

    async def _send_once(
        self,
        request: dict[str, Any],
        *,
        force_reconnect: bool = False,
    ) -> dict[str, Any]:
        if self._uses_http():
            return await self._send_http(request)
        return await self._send_websocket(request, force_reconnect=force_reconnect)

    async def _send_http(self, request: dict[str, Any]) -> dict[str, Any]:
        response = await self._client_or_create().post(
            responses_http_url(self.config.openai_base_url),
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=request,
            timeout=self.config.openai_request_timeout_seconds,
        )
        payload = _http_payload(response)
        if response.status_code >= 400:
            raise _OpenAIRequestError.from_http(response.status_code, payload)
        if not isinstance(payload, dict):
            raise ModelError("OpenAI HTTP response was not a JSON object")
        return payload

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def _send_websocket(
        self,
        request: dict[str, Any],
        *,
        force_reconnect: bool = False,
    ) -> dict[str, Any]:
        websocket = await self._ensure_websocket(force_reconnect=force_reconnect)
        await websocket.send(json.dumps({"type": "response.create", **request}))
        return await _receive_websocket_response(websocket)

    async def _ensure_websocket(self, *, force_reconnect: bool = False) -> Any:
        if (
            not force_reconnect
            and self._websocket is not None
            and not getattr(self._websocket, "closed", False)
        ):
            return self._websocket

        await self._close_websocket()
        self._websocket = await websockets.connect(
            responses_websocket_url(self.config.openai_base_url),
            additional_headers={"Authorization": f"Bearer {self.config.openai_api_key}"},
            open_timeout=_WEBSOCKET_OPEN_TIMEOUT_SECONDS,
            max_size=None,
        )
        return self._websocket

    async def _close_websocket(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                logger.debug("OpenAI websocket close failed", exc_info=True)

    def _new_input_items(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        new_items: list[dict[str, Any]] = []
        for message in messages[self._last_message_count :]:
            if "type" in message:
                new_items.append(_normalize_image_details(message, self.config.openai_model))
                continue
            if message["role"] == "system":
                continue
            if self._previous_response_id is not None and message["role"] == "assistant":
                continue
            if message["role"] == "assistant":
                new_items.extend(_input_assistant_items(message, self.config.openai_model))
            else:
                new_items.append(_input_message(message, self.config.openai_model))
        return new_items


async def _receive_websocket_response(websocket: Any) -> dict[str, Any]:
    while True:
        raw = await websocket.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        event = json.loads(raw)
        event_type = event.get("type")

        if event_type == "error":
            raise _OpenAIRequestError.from_event(event)
        if event_type in {"response.completed", "response.incomplete"}:
            response = event.get("response")
            if not isinstance(response, dict):
                raise _OpenAIRequestError(
                    code="missing_response",
                    message=f"{event_type} did not include a response object",
                )
            return response
        if event_type == "response.failed":
            raise _OpenAIRequestError.from_event(event)


class _OpenAIRequestError(ModelError):
    def __init__(self, *, code: str | None, message: str, status: int | None = None):
        self.code = code
        self.status = status
        prefix = f"{code}: " if code else ""
        super().__init__(f"{prefix}{message}")

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> _OpenAIRequestError:
        error: Any = event.get("error")
        if event.get("type") == "response.failed" and isinstance(event.get("response"), dict):
            error = event["response"].get("error") or error
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or json.dumps(error, sort_keys=True)
        else:
            code = None
            message = str(error or "OpenAI websocket error")
        status = event.get("status")
        return cls(
            code=str(code) if code is not None else None,
            message=str(message),
            status=status if isinstance(status, int) else None,
        )

    @classmethod
    def from_http(cls, status: int, payload: Any) -> _OpenAIRequestError:
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or json.dumps(error, sort_keys=True)
        elif isinstance(error, str):
            code = None
            message = error
        else:
            code = None
            message = str(payload) if payload else f"OpenAI HTTP {status}"
        return cls(
            code=str(code) if code is not None else None,
            message=str(message),
            status=status,
        )


def _http_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        text = response.text.strip()
        return {"error": {"message": text or f"OpenAI HTTP {response.status_code}"}}


def _should_retry(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            asyncio.TimeoutError,
            TimeoutError,
            OSError,
            WebSocketException,
            httpx.TimeoutException,
            httpx.TransportError,
        ),
    ):
        status = _http_status(exc)
        return status is None or _is_transient_status(status)

    status = _http_status(exc)
    if status is not None:
        return _is_transient_status(status)

    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return True
    if isinstance(exc, _OpenAIRequestError):
        if exc.code == "previous_response_not_found":
            return False
        return exc.code in {
            "internal_error",
            "missing_response",
            "overloaded",
            "rate_limit_exceeded",
            "response_failed",
            "server_error",
            "temporarily_unavailable",
            "websocket_connection_limit_reached",
        }
    return False


def _http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _is_transient_status(status: int) -> bool:
    return status in {408, 409, 425, 429} or status >= 500


def _format_exception(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message or repr(exc)}"


def _instructions(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        _message_text(message)
        for message in messages
        if "type" not in message and message["role"] == "system"
    )


def _input_message(message: dict[str, Any], model: str) -> dict[str, Any]:
    content = message["content"]
    if not isinstance(content, str | list):
        raise TypeError("OpenAIModel message content must be a string or list")
    return {
        "role": message["role"],
        "content": _normalize_image_details(content, model),
    }


def _input_assistant_items(message: dict[str, Any], model: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if message.get("content"):
        items.append(_input_message(message, model))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        arguments = call.get("arguments") or "{}"
        items.append(
            {
                "type": "function_call",
                "call_id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "arguments": (
                    arguments
                    if isinstance(arguments, str)
                    else json.dumps(arguments, sort_keys=True)
                ),
            }
        )
    return items


def _normalize_image_details(value: Any, model: str) -> Any:
    if isinstance(value, list):
        return [_normalize_image_details(item, model) for item in value]
    if not isinstance(value, dict):
        return value
    item = {key: _normalize_image_details(child, model) for key, child in value.items()}
    if (
        item.get("type") == "input_image"
        and item.get("detail") == "original"
        and _is_compact_model(model)
    ):
        item["detail"] = "high"
    return item


def _is_compact_model(model: str) -> bool:
    return any(
        model == name or model.startswith(f"{name}-") for name in ("gpt-5.4-mini", "gpt-5.4-nano")
    )


def _message_text(message: dict[str, Any]) -> str:
    content = message["content"]
    if not isinstance(content, str):
        raise TypeError("OpenAIModel messages must use string content")
    return content


def _response_id(response: dict[str, Any]) -> str | None:
    response_id = response.get("id")
    return response_id if isinstance(response_id, str) else None


def _response_output(response: dict[str, Any]) -> list[dict[str, Any]]:
    output = response.get("output")
    if not isinstance(output, list):
        return []
    return [item for item in output if isinstance(item, dict)]


def _response_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text

    parts: list[str] = []
    for item in _response_output(response):
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def _response_tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in _response_output(response):
        if item.get("type") != "function_call":
            continue
        calls.append(
            {
                "id": item["call_id"],
                "name": item["name"],
                "arguments": item.get("arguments") or "{}",
            }
        )
    return calls


def _response_has_reasoning(response: dict[str, Any]) -> bool:
    return any(item.get("type") == "reasoning" for item in _response_output(response))


def _response_cost(response: dict[str, Any]) -> float:
    spec = _model_spec(str(response.get("model") or ""))
    usage = _response_token_usage(response)
    if not usage or spec is None:
        return 0.0

    uncached_tokens = max(
        0,
        usage["input_tokens"] - usage["cached_input_tokens"] - usage["cache_write_tokens"],
    )
    cache_write_price = spec.cache_write_price or spec.input_price
    return round(
        (
            uncached_tokens * spec.input_price
            + usage["cached_input_tokens"] * spec.cached_input_price
            + usage["cache_write_tokens"] * cache_write_price
            + usage["output_tokens"] * spec.output_price
        )
        / 1_000_000,
        8,
    )


def _response_token_usage(response: dict[str, Any]) -> dict[str, int]:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return {}

    details = usage.get("input_tokens_details")
    cached_tokens = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    cache_write_tokens = _int(details.get("cache_write_tokens")) if isinstance(details, dict) else 0
    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    total_tokens = _int(usage.get("total_tokens")) or input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _model_spec(model: str) -> _ModelSpec | None:
    model = _MODEL_ALIASES.get(model, model)
    return _MODELS.get(model)


def context_window_tokens_for(model: str) -> int | None:
    spec = _model_spec(model)
    return spec.context_window_tokens if spec is not None else None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _raise_for_bad_status(response: dict[str, Any], text: str) -> None:
    status = response["status"]
    if status == "completed":
        return
    if status == "incomplete":
        details = response.get("incomplete_details") or {}
        reason = details.get("reason", "unknown")
        if text:
            raise ModelError(f"OpenAI response incomplete ({reason}); partial output returned")
        raise ModelError(f"OpenAI response incomplete ({reason}); no visible output")
    raise ModelError(f"OpenAI response ended with status {status}")
