"""HolmesGPT Pipe Function"""

import aiohttp
import html
import json
import uuid
from typing import AsyncGenerator
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        HOLMESGPT_URL: str = Field(
            default="http://holmesgpt-holmes",
            description="Base URL of holmesgpt server",
        )
        MODEL_LIST: list[str] = Field(
            default=["watsonteam", "uscarlet", "uindigo"],
            description="HolmesGPT model list",
        )
        GENERIC_MODEL_NAME: str = Field(
            default="generic",
            description="HolmesGPT generic model name",
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "holmesgpt", "name": "HolmesGPT"}]

    def _build_trace_headers(self, body: dict, __user__: dict = None) -> dict:
        """Build Langfuse tracing headers."""
        trace_id = str(uuid.uuid4())
        session_id = body.get("chat_id", str(uuid.uuid4()))

        headers = {
            "trace_id": trace_id,
            "session_id": session_id,
            "trace_name": "holmesgpt-chat",
        }

        if __user__:
            trace_user_id = (
                __user__.get("name") or __user__.get("email") or __user__.get("id")
            )
            if trace_user_id:
                headers["trace_user_id"] = trace_user_id

        return headers

    async def pipe(self, body: dict, __user__: dict = None):
        messages = body.get("messages", [])
        conversation_history = None

        if len(messages) > 1:
            conversation_history = []
            for msg in messages[:-1]:
                conversation_history.append(
                    {"role": msg["role"], "content": msg["content"]}
                )

        ask = messages[-1]["content"] if messages else ""
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

        if __user__:
            username = __user__.get("email").split("@")[0]
            if username in self.valves.MODEL_LIST:
                payload["model"] = username

        url = f"{self.valves.HOLMESGPT_URL}/api/chat"
        headers = self._build_trace_headers(body, __user__)

        if stream:
            return self._stream(url, payload, headers)
        else:
            return await self._non_stream(url, payload, headers)

    async def _non_stream(
        self,
        url: str,
        payload: dict,
        extra_headers: dict = None,
    ):
        """Handle non-streaming response."""
        timeout = aiohttp.ClientTimeout(total=1800)
        req_headers = {"Content-Type": "application/json"}

        if extra_headers:
            req_headers.update(extra_headers)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=req_headers) as resp:
                resp.raise_for_status()

                text = await resp.text()

                reasoning = None
                analysis = None
                current_event = None

                lines = text.split("\n")

                for line in lines:
                    if line and line.startswith("event: "):
                        current_event = line[7:].strip()

                    elif line and line.startswith("data: "):
                        data = line[6:].strip()

                        if data == "[DONE]":
                            break

                        try:
                            event_data = json.loads(data)

                            if current_event == "ai_message":
                                reasoning = event_data.get("reasoning", "")

                            elif current_event == "ai_answer_end":
                                analysis = event_data.get("analysis")
                                break

                        except json.JSONDecodeError:
                            continue

                    elif line.strip() == "":
                        current_event = None

                if analysis:
                    if reasoning:
                        return f"{reasoning}\n\n{analysis}"

                    return analysis

                return "Error: No analysis found in HolmesGPT response"

    @staticmethod
    def _build_tool_details_block(
        tool_call_id: str,
        tool_name: str,
        params: dict,
        result_text: str,
    ) -> str:
        """
        Render a HolmesGPT tool result as Open WebUI's standard
        `<details type="tool_calls">` block. The Holmes PromQL placeholder
        resolver in the frontend reads these blocks to surface charts.
        """
        try:
            arguments_json = json.dumps(params or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            arguments_json = "{}"

        # serialize_output() in open-webui wraps `result_text` once via
        # json.dumps so that the attribute is always a JSON string. Mirror
        # that here so the holmesPromql resolver's `parseMaybeJSON` loop
        # double-unwraps the same way it does for native flows.
        try:
            result_attr = json.dumps(result_text or "", ensure_ascii=False)
        except (TypeError, ValueError):
            result_attr = json.dumps(str(result_text or ""), ensure_ascii=False)

        return (
            f'<details type="tool_calls" done="true" '
            f'id="{html.escape(tool_call_id, quote=True)}" '
            f'name="{html.escape(tool_name or "", quote=True)}" '
            f'arguments="{html.escape(arguments_json, quote=True)}" '
            f'result="{html.escape(result_attr, quote=True)}">\n'
            f"<summary>Tool Executed</summary>\n"
            f"</details>\n"
        )

    @staticmethod
    def _extract_tool_call_output(event_data: dict) -> tuple[str, str, dict, str]:
        """
        Pull out (tool_call_id, tool_name, params, stringifyed_result)
        from a HolmesGPT `tool_calling_result` SSE event payload.
        """
        tool_call_id = event_data.get("tool_call_id") or event_data.get("id") or ""
        tool_name = event_data.get("tool_name") or event_data.get("name") or ""

        result = event_data.get("result") or {}
        if not isinstance(result, dict):
            result = {}

        params = result.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        # `to_client_dict` re-serializes `result.data` via
        # `get_stringified_data()` so this is already a JSON string when
        # present. Fall back to dumping the full result for error responses.
        result_text = result.get("data")

        if result_text in (None, ""):
            try:
                result_text = json.dumps(result, ensure_ascii=False)
            except (TypeError, ValueError):
                result_text = str(result)

        elif not isinstance(result_text, str):
            try:
                result_text = json.dumps(result_text, ensure_ascii=False)
            except (TypeError, ValueError):
                result_text = str(result_text)

        return tool_call_id, tool_name, params, result_text

    async def _stream(
        self,
        url: str,
        payload: dict,
        extra_headers: dict = None,
    ) -> AsyncGenerator[str, None]:
        """
        Handle streaming response.

        Captures HolmesGPT's tool_calling_result SSE events and surfaces them
        to Open WebUI as inline `<details type="tool_calls">` blocks so the
        frontend PromQL graph resolver can match the embedded `<<{{...}}>>`
        placeholders by tool_call_id.
        """
        timeout = aiohttp.ClientTimeout(total=1800)
        req_headers = {"Content-Type": "application/json"}

        if extra_headers:
            req_headers.update(extra_headers)

        # Accumulate tool results until we're ready to flush them after
        # </think>. Order is preserved via insertion order of the dict.
        tool_results: dict[str, str] = {}

        # Track tool_name from start_tool_calling so we can fall back if the
        # result event omits it.
        pending_tool_starts: dict[str, str] = {}

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=req_headers) as resp:
                resp.raise_for_status()

                buffer = b""
                current_event = None
                thinking = True

                yield "<think>"

                async for chunk in resp.content.iter_any():
                    buffer += chunk

                    while b"\n" in buffer:
                        line_bytes, buffer = buffer.split(b"\n", 1)
                        line = line_bytes.decode("utf-8").strip()

                        if not line:
                            current_event = None
                            continue

                        if line.startswith("event: "):
                            current_event = line[7:].strip()

                        elif line.startswith("data: "):
                            chunk_data = line[6:].strip()

                            if chunk_data == "[DONE]":
                                break

                            try:
                                event_data = json.loads(chunk_data)
                            except json.JSONDecodeError:
                                continue

                            if current_event == "ai_message":
                                reasoning = event_data.get("reasoning", "")

                                if reasoning and thinking:
                                    yield f"{reasoning}\n\n"

                            elif current_event == "start_tool_calling":
                                tool_name = event_data.get("tool_name", "")
                                tool_call_id = (
                                    event_data.get("id")
                                    or event_data.get("tool_call_id")
                                    or ""
                                )

                                if tool_call_id:
                                    pending_tool_starts[tool_call_id] = tool_name

                            elif current_event == "tool_calling_result":
                                (
                                    tool_call_id,
                                    tool_name,
                                    params,
                                    result_text,
                                ) = self._extract_tool_call_output(event_data)

                                if not tool_call_id:
                                    continue

                                tool_name = tool_name or pending_tool_starts.get(
                                    tool_call_id,
                                    "",
                                )
                                pending_tool_starts.pop(tool_call_id, None)

                                tool_results[tool_call_id] = (
                                    self._build_tool_details_block(
                                        tool_call_id,
                                        tool_name,
                                        params,
                                        result_text,
                                    )
                                )

                            elif current_event == "ai_answer_end":
                                analysis = event_data.get("analysis")

                                if analysis is not None:
                                    thinking = False
                                    yield "</think>\n\n"

                                    # Flush captured tool calls so the
                                    # frontend resolver can match the
                                    # placeholders embedded in `analysis`.
                                    for block in tool_results.values():
                                        yield block

                                    yield analysis
                                    return
