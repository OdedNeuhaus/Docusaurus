"""HolmesGPT Pipe Function"""

import aiohttp
import json
import uuid
from typing import AsyncGenerator
from pydantic import BaseModel, Field


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
            trace_user_id = __user__.get("email") or __user__.get("name") or __user__.get("id")
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
                    {
                        "role": msg["role"],
                        "content": msg["content"]
                    }
                )

        ask = messages[-1]["content"] if messages else ""
        stream = body.get("stream", False)

        payload = {
            "ask": ask,
            "stream": stream,
            "model": self.valves.GENERIC_MODEL_NAME
        }

        if conversation_history:
            if conversation_history[0]["role"] != "system":
                conversation_history.insert(
                    0,
                    {
                        "role": "system",
                        "content": "You are a helpful kubernetes troubleshooting assistant"
                    }
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

    async def _non_stream(self, url: str, payload: dict, extra_headers: dict = None):
        """Handle non-streaming response"""
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

    async def _stream(self, url: str, payload: dict, extra_headers: dict = None) -> AsyncGenerator[str, None]:
        """Handle streaming response"""
        timeout = aiohttp.ClientTimeout(total=1800)
        req_headers = {"Content-Type": "application/json"}
        if extra_headers:
            req_headers.update(extra_headers)

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

                                if current_event == "ai_message":
                                    reasoning = event_data.get("reasoning", "")
                                    if reasoning:
                                        if thinking:
                                            yield f"{reasoning}\n\n"
                                elif current_event == "ai_answer_end":
                                    analysis = event_data.get("analysis")
                                    if analysis:
                                        thinking = False
                                        yield "</think>\n\n"
                                        yield analysis
                                        break
                            except json.JSONDecodeError:
                                continue
