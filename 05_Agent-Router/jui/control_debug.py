"""Explicit, stateless model tests. No memory, Agent delegation or real tool execution."""

import asyncio
import base64
import re
import time
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .control_models import cloud_guard, search
from .control_voice import synthesize, transcribe

router = APIRouter(prefix="/api/v1/debug")
REQUEST_ID = r"^[a-zA-Z0-9_-]{1,100}$"


class TestRequest(BaseModel):
    request_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex, pattern=REQUEST_ID
    )
    confirm_external: bool = False


class DebugInput(TestRequest):
    model_ref: str = Field(min_length=1, max_length=100)
    workspace_id: str = "default"


def remember(c, identity):
    c.debug_seen.add(identity)
    if len(c.debug_seen) > 2000:
        c.debug_seen = set(c.debug_workers) | {identity}


def selected(c, body, capability, audio=False, sensitive=False):
    c.store.library.get("workspace", body.workspace_id)
    profile = c.store.resolve(
        capability if capability in {"tts", "asr", "vision"} else "main",
        override=body.model_ref,
    )
    if capability not in profile.get("capabilities", []):
        raise ValueError("The selected profile does not have this capability")
    if not profile["provider"].get("local") and not body.confirm_external:
        raise ValueError("Confirm this external model test and its potential API cost")
    cloud_guard(profile, sensitive, c.store.policy(body.workspace_id), audio=audio)
    return profile


async def tracked(request, identity, operation, timeout=240):
    c = request.app.state.control
    if c.maintenance:
        raise HTTPException(409, "Wait for maintenance to finish")
    if identity in c.debug_workers or identity in c.debug_seen:
        raise HTTPException(
            409, "This debug request was already submitted; it was not repeated"
        )
    if len(c.debug_workers) >= 4:
        raise HTTPException(429, "Too many debug requests are already waiting")
    remember(c, identity)
    task = asyncio.create_task(operation())
    c.debug_workers[identity] = task
    try:
        return await asyncio.wait_for(task, timeout)
    except asyncio.CancelledError:
        raise HTTPException(409, "Debug request cancelled") from None
    except TimeoutError:
        raise HTTPException(504, "Debug request timed out") from None
    finally:
        if not task.done():
            task.cancel()
        c.debug_workers.pop(identity, None)


@router.post("/{identity}/cancel")
async def cancel_debug(identity: str, request: Request):
    if not re.fullmatch(REQUEST_ID, identity):
        raise ValueError("Invalid debug request ID")
    c = request.app.state.control
    # A cancelled upload may reach the handler after the cancellation request.
    remember(c, identity)
    task = c.debug_workers.get(identity)
    if task and not task.cancelling():
        task.cancel()
    return {"cancelled": task is not None}


class TextInput(DebugInput):
    prompt: str = Field(min_length=1, max_length=12000)
    image: str | None = Field(default=None, max_length=4_000_000)
    mode: Literal["text", "vision", "tools"] = "text"


@router.post("/text")
async def text_test(body: TextInput, request: Request):
    c = request.app.state.control
    profile = selected(c, body, body.mode, sensitive=bool(body.image))
    if not body.prompt.strip():
        raise ValueError("The test prompt is empty")
    if body.mode == "vision" and not body.image:
        raise ValueError("Select an image for this vision test")
    if body.image and body.mode != "vision":
        raise ValueError("Images require the vision test mode")
    content = body.prompt
    if body.image:
        if not re.fullmatch(
            r"data:image/(png|jpeg);base64,[A-Za-z0-9+/=]+", body.image
        ):
            raise ValueError("Only inline PNG/JPEG images are accepted")
        raw = base64.b64decode(body.image.split(",", 1)[1], validate=True)
        if not raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
            raise ValueError("Invalid image signature")
        content = [
            {"type": "text", "text": body.prompt},
            {"type": "image_url", "image_url": {"url": body.image}},
        ]
    tools = (
        [
            {
                "type": "function",
                "function": {
                    "name": "debug_echo",
                    "description": "A simulated echo tool. Propose a call; no tool will actually execute.",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            }
        ]
        if body.mode == "tools"
        else None
    )

    async def perform():
        async with c.engine.model_session(profile):
            start = time.monotonic()
            result = await c.engine.completion(
                profile,
                [{"role": "user", "content": content}],
                stream=False,
                tools=tools,
            )
            message = result.choices[0].message
            return {
                "text": message.content or "",
                "tool_calls": [
                    {"name": call.function.name, "arguments": call.function.arguments}
                    for call in (message.tool_calls or [])
                ],
                "tools_executed": False,
                "model_ref": profile["id"],
                "elapsed_ms": round((time.monotonic() - start) * 1000),
            }

    return await tracked(
        request, body.request_id, perform, profile.get("timeout", 240) + 180
    )


class SpeechInput(DebugInput):
    text: str = Field(min_length=1, max_length=600)
    voice: str | None = Field(default=None, max_length=200)
    speed: float | None = Field(default=None, ge=0.25, le=4)
    language: Literal["en_us", "en_gb", "cmn"] | None = None


@router.post("/speech")
async def speech_test(body: SpeechInput, request: Request):
    c = request.app.state.control
    profile = selected(c, body, "tts", audio=True)
    if not body.text.strip():
        raise ValueError("The speech test text is empty")
    if body.language is not None:
        if not c.zonos.matches(profile):
            raise ValueError(
                "This voice profile does not support a debug language override"
            )
        profile = {**profile, "language": body.language}

    async def perform():
        return await synthesize(
            c, request.app.state.voice, profile, body.text, body.voice, body.speed
        )

    result = await tracked(
        request, body.request_id, perform, profile.get("timeout", 240) + 180
    )
    return Response(
        result,
        media_type="audio/wav",
        headers={"X-Jui-Model": profile["id"], "Cache-Control": "no-store"},
    )


@router.post("/transcribe")
async def recognition_test(
    request: Request,
    model_ref: str,
    workspace_id: str = "default",
    request_id: str = "",
    confirm_external: bool = False,
):
    body = DebugInput(
        model_ref=model_ref,
        workspace_id=workspace_id,
        request_id=request_id or uuid.uuid4().hex,
        confirm_external=confirm_external,
    )
    c = request.app.state.control
    profile = selected(c, body, "asr", audio=True)
    audio = bytearray()
    async for chunk in request.stream():
        audio.extend(chunk)
        if len(audio) > 2_000_000:
            raise HTTPException(413, "Recording exceeds the limit")
    request.app.state.voice.validate_audio(bytes(audio))

    async def perform():
        start = time.monotonic()
        text = await transcribe(c, request.app.state.voice, profile, bytes(audio))
        return {
            "text": text,
            "model_ref": profile["id"],
            "elapsed_ms": round((time.monotonic() - start) * 1000),
        }

    return await tracked(
        request, body.request_id, perform, profile.get("timeout", 240) + 180
    )


@router.get("/models/{identity}/voices")
async def voices(identity: str, request: Request):
    c = request.app.state.control
    profile = c.store.resolve("tts", override=identity)
    if c.zonos.matches(profile):
        if c.maintenance:
            raise HTTPException(409, "Wait for maintenance to finish")
        worker = asyncio.current_task()
        c.voice_workers.add(worker)
        try:
            async with c.gpu_lock:
                values = await c.zonos.voices()
            return {"voices": values, "speed_supported": False}
        finally:
            c.voice_workers.discard(worker)
    return {
        "voices": [
            {"id": profile.get("voice", "alloy"), "name": profile.get("voice", "alloy")}
        ],
        "speed_supported": profile["provider"]["protocol"] != "native-tts",
    }


class SearchInput(TestRequest):
    provider_ref: str
    query: str = Field(min_length=1, max_length=500)
    language: str = Field(default="en", max_length=30)
    region: str = Field(default="us", max_length=10)
    count: int = Field(default=5, ge=1, le=10)


@router.post("/search")
async def search_test(body: SearchInput, request: Request):
    c = request.app.state.control
    provider = c.store.get("provider", body.provider_ref)
    if not provider.get("local") and not body.confirm_external:
        raise ValueError("Confirm sending this test query to the search service")
    if not body.query.strip():
        raise ValueError("The test query is empty")

    async def perform():
        start = time.monotonic()
        results = await search(
            c.store,
            body.query,
            {
                "provider_id": body.provider_ref,
                "enabled": True,
                "language": body.language,
                "region": body.region,
                "count": body.count,
            },
        )
        return {
            "results": results,
            "elapsed_ms": round((time.monotonic() - start) * 1000),
        }

    return await tracked(request, body.request_id, perform, timeout=30)
