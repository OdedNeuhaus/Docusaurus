"""HolmesGPT Pipe Function"""

import asyncio
import aiohttp
import json
import logging
import re
import uuid
from collections.abc import Iterable
from typing import Any, AsyncGenerator, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Pipe:
    class Valves(BaseModel):
        HOLMESGPT_URL: str = Field(
            default="https://holmesgpt-holmes",
            description="Base URL of holmesgpt server"
        )
        MODEL_LIST: list[str] = Field(
            default=["watsonteam", "uscarlet", "uindigo"],
            description="HolmesGPT model list"
        )
        GENERIC_MODEL_NAME: str = Field(
            default="generic",
            description="HolmesGPT generic model name"
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "holmesgpt", "name": "HolmesGPT"}]

    def _sanitize_header_value(self, value: Any) -> str:
        """Strip control characters so user data is safe to forward as a header."""
        return re.sub(r"[\r\n\t]", "", str(value))

    def _user_dict(self, __user__: Optional[dict]) -> dict:
        return __user__ if isinstance(__user__, dict) else {}

    def _message_content_to_text(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if text is not None:
                        parts.append(str(text))
            return "".join(parts)
        return str(content)

    def _normalize_messages(self, messages: Any) -> list[dict[str, str]]:
        if not isinstance(messages, list):
            return []

        normalized: list[dict[str, str]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                logger.warning("Skipping malformed message entry: %r", msg)
                continue
            normalized.append(
                {
                    "role": str(msg.get("role", "user") or "user"),
                    "content": self._message_content_to_text(msg.get("content")),
                }
            )
        return normalized

    def _iter_sse_events(self, lines: Iterable[str], *, flush_final: bool = False):
        current_event: Optional[str] = None
        data_lines: list[str] = []

        def finalize():
            nonlocal current_event, data_lines
            if current_event is None and not data_lines:
                return None
            event = (current_event, "\n".join(data_lines))
            current_event = None
            data_lines = []
            return event

        for raw_line in lines:
            line = raw_line.rstrip("\r")
            if not line:
                event = finalize()
                if event is not None:
                    yield event
                continue

            if line.startswith(":"):
                continue

            field, sep, value = line.partition(":")
            if not sep:
                value = ""
            elif value.startswith(" "):
                value = value[1:]

            if field == "event":
                current_event = value
            elif field == "data":
                data_lines.append(value)

        if flush_final:
            event = finalize()
            if event is not None:
                yield event

    def _extract_sse_result(
        self, events: Iterable[tuple[Optional[str], str]], trace_id: Optional[str]
    ) -> tuple[list[str], Optional[str], bool]:
        reasoning_parts: list[str] = []
        analysis: Optional[str] = None
        saw_done = False

        for event_name, data in events:
            if data == "[DONE]":
                saw_done = True
                break

            try:
                event_data = json.loads(data)
            except json.JSONDecodeError:
                logger.warning(
                    "Failed to parse SSE data line (trace_id=%s): %r",
                    trace_id,
                    data,
                )
                continue

            if event_name == "ai_message":
                reasoning = event_data.get("reasoning")
                if reasoning:
                    reasoning_parts.append(str(reasoning))
            elif event_name == "ai_answer_end":
                raw_analysis = event_data.get("analysis")
                if raw_analysis:
                    analysis = str(raw_analysis)
                break

        return reasoning_parts, analysis, saw_done

    def _build_trace_headers(self, body: dict, __user__: Optional[dict] = None) -> dict:
        """Build Langfuse tracing headers."""
        trace_id = str(uuid.uuid4())
        session_id = body.get("chat_id")
        if not session_id:
            session_id = str(uuid.uuid4())
            logger.warning(
                "chat_id missing from request body; traces will not group correctly "
                "(trace_id=%s)", trace_id
            )

        headers = {
            "trace_id": trace_id,
            "session_id": self._sanitize_header_value(session_id),
            "trace_name": "holmesgpt-chat",
        }

        user = self._user_dict(__user__)
        if user:
            trace_user_id = user.get("email") or user.get("name") or user.get("id")
            if trace_user_id:
                headers["trace_user_id"] = self._sanitize_header_value(trace_user_id)

        return headers

    async def pipe(self, body: dict, __user__: Optional[dict] = None):
        if not isinstance(body, dict):
            return "Error: invalid request body."

        messages = self._normalize_messages(body.get("messages", []))
        if not messages:
            return "Error: no user message provided."

        conversation_history = None
        if len(messages) > 1:
            conversation_history = [
                {"role": msg["role"], "content": msg["content"]}
                for msg in messages[:-1]
            ]

        ask = messages[-1]["content"]
        stream = body.get("stream", False)

        payload = {
            "ask": ask,
            "stream": stream,
            "model": self.valves.GENERIC_MODEL_NAME,
        }

        if conversation_history:
            if conversation_history[0]["role"] != "system":
                conversation_history.insert(
                    0,
                    {
                        "role": "system",
                        "content": "You are a helpful kubernetes troubleshooting assistant",
                    },
                )
            payload["conversation_history"] = conversation_history

        user = self._user_dict(__user__)
        if user:
            email = str(user.get("email") or "").lower()
            username = email.split("@")[0]
            if username in self.valves.MODEL_LIST:
                payload["model"] = username

        url = f"{self.valves.HOLMESGPT_URL}/api/chat"
        headers = self._build_trace_headers(body, __user__)
        trace_id = headers["trace_id"]

        logger.info(
            "Sending request to HolmesGPT (trace_id=%s, stream=%s, model=%s)",
            trace_id, stream, payload["model"],
        )

        if stream:
            # Return the generator directly — do not await.
            # Errors inside the generator are caught within _stream.
            return self._stream(url, payload, headers)
        else:
            return await self._non_stream(url, payload, headers)

    async def _non_stream(self, url: str, payload: dict, extra_headers: Optional[dict] = None) -> str:
        """Handle non-streaming response."""
        timeout = aiohttp.ClientTimeout(total=1800)  # HolmesGPT may run long tool chains
        req_headers = {"Content-Type": "application/json"}
        if extra_headers:
            req_headers.update(extra_headers)

        trace_id = (extra_headers or {}).get("trace_id")

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=req_headers) as resp:
                    resp.raise_for_status()

                    text = await resp.text()
                    events = self._iter_sse_events(text.splitlines(), flush_final=True)
                    reasoning_parts, analysis, saw_done = self._extract_sse_result(events, trace_id)

                    if analysis:
                        if reasoning_parts:
                            return f"{''.join(reasoning_parts)}\n\n{analysis}"
                        return analysis

                    logger.error(
                        "No terminal analysis in HolmesGPT response (trace_id=%s, saw_done=%s)",
                        trace_id,
                        saw_done,
                    )
                    return "Error: HolmesGPT returned an incomplete streaming response."

        except aiohttp.ClientResponseError as e:
            logger.error(
                "HolmesGPT returned HTTP %s (trace_id=%s): %s", e.status, trace_id, e.message
            )
            return f"Error: HolmesGPT returned HTTP {e.status}."
        except aiohttp.ClientConnectorError as e:
            logger.error("Could not connect to HolmesGPT (trace_id=%s): %s", trace_id, e)
            return "Error: Could not connect to HolmesGPT. Check HOLMESGPT_URL."
        except asyncio.TimeoutError:
            logger.error("HolmesGPT request timed out (trace_id=%s)", trace_id)
            return "Error: HolmesGPT request timed out."
        except Exception as e:
            logger.error(
                "Unexpected error calling HolmesGPT (trace_id=%s): %s", trace_id, e, exc_info=True
            )
            return f"Error: Unexpected error: {e}"

    async def _stream(self, url: str, payload: dict, extra_headers: Optional[dict] = None) -> AsyncGenerator[str, None]:
        """Handle streaming response with live reasoning output."""
        timeout = aiohttp.ClientTimeout(total=1800)
        req_headers = {"Content-Type": "application/json"}
        if extra_headers:
            req_headers.update(extra_headers)

        trace_id = (extra_headers or {}).get("trace_id")
        thinking = True  # Track whether we're still in the <think> block

        yield "<think>"

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=req_headers) as resp:
                    resp.raise_for_status()

                    buffer = b""
                    current_event: Optional[str] = None
                    data_lines: list[str] = []

                    def flush_event() -> tuple[list[str], bool]:
                        nonlocal current_event, data_lines, thinking
                        if current_event is None and not data_lines:
                            return [], False

                        data = "\n".join(data_lines)
                        event_name = current_event
                        current_event = None
                        data_lines = []

                        if data == "[DONE]":
                            return [], True

                        try:
                            event_data = json.loads(data)
                        except json.JSONDecodeError:
                            logger.warning(
                                "Failed to parse SSE data line (trace_id=%s): %r",
                                trace_id,
                                data,
                            )
                            return [], False

                        if event_name == "ai_message":
                            reasoning = event_data.get("reasoning")
                            if reasoning and thinking:
                                return [str(reasoning) + "\n\n"], False
                            return [], False

                        if event_name == "ai_answer_end":
                            analysis = event_data.get("analysis")
                            outputs: list[str] = []
                            if thinking:
                                thinking = False
                                outputs.append("</think>\n\n")
                            if analysis:
                                outputs.append(str(analysis))
                            return outputs, True

                        return [], False

                    def handle_line(raw_line: str) -> tuple[list[str], bool]:
                        nonlocal current_event, data_lines
                        line = raw_line.rstrip("\r")

                        if not line:
                            return flush_event()

                        if line.startswith(":"):
                            return [], False

                        field, sep, value = line.partition(":")
                        if not sep:
                            value = ""
                        elif value.startswith(" "):
                            value = value[1:]

                        if field == "event":
                            current_event = value
                        elif field == "data":
                            data_lines.append(value)
                        return [], False

                    async for raw_chunk in resp.content.iter_any():
                        buffer += raw_chunk

                        while b"\n" in buffer:
                            line_bytes, buffer = buffer.split(b"\n", 1)
                            line = line_bytes.decode("utf-8")

                            outputs, should_stop = handle_line(line)
                            for output in outputs:
                                yield output
                            if should_stop:
                                return

                    # Handle any trailing data in the buffer
                    if buffer:
                        trailing_line = buffer.decode("utf-8")
                        outputs, should_stop = handle_line(trailing_line)
                        for output in outputs:
                            yield output
                        if should_stop:
                            return

                    # Flush any remaining event
                    outputs, should_stop = flush_event()
                    for output in outputs:
                        yield output
                    if should_stop:
                        return

                    # If we got here, the stream ended without a final analysis
                    if thinking:
                        yield "</think>\n\n"
                    logger.error(
                        "HolmesGPT stream ended without final analysis (trace_id=%s)",
                        trace_id,
                    )
                    yield "Error: HolmesGPT returned an incomplete streaming response."

        except aiohttp.ClientResponseError as e:
            logger.error(
                "HolmesGPT returned HTTP %s (trace_id=%s): %s", e.status, trace_id, e.message
            )
            if thinking:
                yield "</think>\n\n"
            yield f"Error: HolmesGPT returned HTTP {e.status}."
        except aiohttp.ClientConnectorError as e:
            logger.error("Could not connect to HolmesGPT (trace_id=%s): %s", trace_id, e)
            if thinking:
                yield "</think>\n\n"
            yield "Error: Could not connect to HolmesGPT. Check HOLMESGPT_URL."
        except asyncio.TimeoutError:
            logger.error("HolmesGPT request timed out (trace_id=%s)", trace_id)
            if thinking:
                yield "</think>\n\n"
            yield "Error: HolmesGPT request timed out."
        except Exception as e:
            logger.error(
                "Unexpected error in HolmesGPT stream (trace_id=%s): %s", trace_id, e, exc_info=True
            )
            if thinking:
                yield "</think>\n\n"
            yield f"Error: Unexpected error: {e}"
