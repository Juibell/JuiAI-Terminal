import asyncio
import hmac
import json
import re
import subprocess
import uuid
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .config import APP, ROOT, load_secrets, load_settings
from .control_local_models import MANAGED_MODELS
from .control_models import cloud_guard, endpoint, search
from .control_store import AGENTS, ROLES
from .control_voice import (
    synthesize as voice_synthesize,
)
from .control_voice import (
    transcribe as voice_transcribe,
)
from .language import localize_error, localize_update

router = APIRouter(prefix="/api/v1")
internal = APIRouter(prefix="/internal")


@router.post("/shutdown")
async def shutdown(request: Request):
    c = control(request)
    if c.maintenance:
        raise HTTPException(409, "Wait for maintenance to finish")
    server = getattr(request.app.state, "server", None)
    if server is None:
        raise HTTPException(409, "This server does not support managed shutdown")

    async def stop():
        await asyncio.sleep(0.3)
        server.should_exit = True

    asyncio.create_task(stop())
    return {"stopping": True}


def control(request):
    value = getattr(request.app.state, "control", None)
    if value is None:
        raise HTTPException(503, "Control center is not ready")
    return value


def idle(c):
    if (
        c.workers
        or c.voice_workers
        or c.debug_workers
        or c.voice_processing.running
        or c.maintenance
    ):
        raise HTTPException(409, "Wait for tasks, speech and maintenance to finish")


@router.get("/state")
async def state(request: Request):
    c = control(request)
    return {
        "version": 1,
        "event_cursor": c.store.last_event(),
        "name": "JuiAI Terminal",
        "providers": [c.store.public_provider(p) for p in c.store.list("provider")],
        "models": c.store.list("model"),
        "roles": c.store.get("roles", "global"),
        "workspaces": c.store.library.list("workspace"),
        "workspace_id": c.store.library.preferences()["workspace_id"],
        "policies": c.store.list("policy"),
        "memories": c.store.library.list("memory"),
        "memory_enabled": c.store.library.preferences()["memory_enabled"],
        "preferences": c.store.list("preferences"),
        "running": list(c.workers),
        "debug_running": list(c.debug_workers),
        "voice_processing_running": c.voice_processing.running,
        "needs_restart": c.uncertain,
        "maintenance": c.maintenance,
    }


@router.get("/events")
async def events(request: Request, after: int = 0):
    c = control(request)
    try:
        after = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise ValueError("Invalid event cursor") from None

    async def stream():
        cursor = after
        heartbeat = 0
        while not await request.is_disconnected():
            rows = c.store.events(cursor)
            for event in rows:
                cursor = event["seq"]
                if event["kind"] in {"run", "update"}:
                    localize = (
                        localize_update if event["kind"] == "update" else localize_error
                    )
                    event = {**event, "data": localize(event["data"])}
                yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            heartbeat += 1
            if heartbeat % 40 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ConversationInput(BaseModel):
    workspace_id: str | None = None


@router.get("/conversations")
async def conversations(request: Request):
    return control(request).store.list("conversation")


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationInput, request: Request):
    return control(request).store.conversation(body.workspace_id)


@router.get("/conversations/{identity}")
async def conversation(identity: str, request: Request):
    c = control(request)
    return {
        **c.store.get("conversation", identity),
        "messages": c.store.messages(identity),
        "runs": [
            localize_error(r)
            for r in c.store.list("run")
            if r["conversation_id"] == identity
        ],
    }


class ConversationChange(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    favorite: bool | None = None


@router.patch("/conversations/{identity}")
async def change_conversation(
    identity: str, body: ConversationChange, request: Request
):
    c = control(request)
    changes = body.model_dump(exclude_none=True)
    if body.title is not None:
        changes["title_is_placeholder"] = False
    return c.store.put(
        "conversation", {**c.store.get("conversation", identity), **changes}
    )


class Selection(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)


@router.post("/conversations/delete")
async def delete_conversations(body: Selection, request: Request):
    c = control(request)
    for run in c.store.list("run"):
        if run["conversation_id"] in body.ids and (
            run["id"] in c.workers or run["status"] in {"queued", "running"}
        ):
            raise HTTPException(
                409, "A selected conversation has a running task; nothing was deleted"
            )
    legacy_ids = {
        m["run_id"]
        for m in c.store.list("message")
        if m["conversation_id"] in body.ids
        and m["conversation_id"].startswith("legacy-")
    }
    legacy = getattr(request.app.state, "tasks", None)
    if legacy_ids and hasattr(legacy, "list"):
        existing = {task["id"] for task in legacy.list()}
        if legacy_ids & existing:
            legacy.delete(sorted(legacy_ids & existing))
    with c.store.connect() as db:
        for identity in body.ids:
            if identity.startswith("legacy-"):
                db.execute(
                    "INSERT OR REPLACE INTO objects VALUES('legacy_deleted',?,?)",
                    (identity, json.dumps({"id": identity})),
                )
        for kind in ("message", "run", "conversation"):
            for item in c.store.list(kind):
                cid = item["id"] if kind == "conversation" else item["conversation_id"]
                if cid in body.ids:
                    db.execute(
                        "DELETE FROM objects WHERE kind=? AND id=?", (kind, item["id"])
                    )
        # Deleted content must not remain replayable through the event journal.
        db.execute("DELETE FROM events")
    return {"deleted": len(set(body.ids))}


class MessageInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=24000)
    mode: Literal["auto", "chat", "general", "code", "research", "pipeline"] = "auto"
    model_ref: str | None = None
    use_memory: bool = True
    use_search: bool = False
    image: str | None = Field(default=None, max_length=4_000_000)
    regenerate_run_id: str | None = None


@router.post("/conversations/{identity}/messages", status_code=202)
async def message(identity: str, body: MessageInput, request: Request):
    if not body.prompt.strip():
        raise ValueError("Message is empty")
    if body.image:
        from base64 import b64decode

        if not re.fullmatch(
            r"data:image/(png|jpeg);base64,[A-Za-z0-9+/=]+", body.image
        ):
            raise ValueError("Only inline PNG/JPEG images are accepted")
        raw = b64decode(body.image.split(",", 1)[1], validate=True)
        if not raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
            raise ValueError("Image signature does not match PNG/JPEG")
    return control(request).submit(identity, **body.model_dump())


@router.get("/runs/{identity}")
async def run(identity: str, request: Request):
    value = dict(control(request).store.get("run", identity))
    value.pop("profiles", None)
    return localize_error(value)


@router.post("/runs/{identity}/cancel")
async def cancel(identity: str, request: Request):
    return localize_error(await control(request).cancel(identity))


class ProviderInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    protocol: Literal["openai", "anthropic", "gemini", "brave", "tavily", "searxng"]
    base_url: str = Field(min_length=1, max_length=1000)
    enabled: bool = True
    api_key: str | None = Field(default=None, max_length=4096)


def save_provider(c, body, identity=None):
    base, local = endpoint(body.base_url, body.protocol)
    old = c.store.get("provider", identity) if identity else {}
    if old.get("protocol", "").startswith("native-") or identity in {
        *MANAGED_MODELS,
        "zonos3",
    }:
        raise ValueError(
            "Built-in local connections are read-only; add a new connection instead"
        )
    identity = identity or uuid.uuid4().hex
    ref = old.get("secret_ref")
    if body.api_key is not None:
        if any(ch in body.api_key for ch in "\r\n\x00"):
            raise ValueError("Invalid credential")
        ref = "provider-" + uuid.uuid4().hex if body.api_key else None
        if ref:
            c.store.vault.put(ref, body.api_key)
    value = {
        **body.model_dump(exclude={"api_key"}),
        "id": identity,
        "base_url": base,
        "local": local,
        "secret_ref": ref,
    }
    c.store.put("provider", value)
    return c.store.public_provider(value)


@router.post("/providers", status_code=201)
async def add_provider(body: ProviderInput, request: Request):
    return save_provider(control(request), body)


@router.put("/providers/{identity}")
async def edit_provider(identity: str, body: ProviderInput, request: Request):
    return save_provider(control(request), body, identity)


@router.delete("/providers/{identity}")
async def delete_provider(identity: str, request: Request):
    c = control(request)
    if identity in {*MANAGED_MODELS, "fish", "firered", "zonos3"} or any(
        m["provider_id"] == identity for m in c.store.list("model")
    ):
        raise ValueError("Built-in or referenced connections cannot be deleted")
    c.store.remove("provider", identity)
    return {"deleted": True}


class ProbeInput(BaseModel):
    model_ref: str | None = None
    generate: bool = False


@router.post("/providers/{identity}/probe")
async def probe_provider(identity: str, body: ProbeInput, request: Request):
    c = control(request)
    provider = c.store.get("provider", identity)
    model = None
    if body.generate:
        if not body.model_ref:
            raise ValueError("Select a model for the explicit generation test")
        model = c.store.get("model", body.model_ref)
        if model["provider_id"] != identity:
            raise ValueError("Model belongs to another connection")
        model = {**model, "provider": provider}
        async with c.engine.model_session(model):
            pass
    return await c.engine.probe(provider, model)


class ModelInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider_id: str
    model_id: str = Field(min_length=1, max_length=300)
    enabled: bool = True
    context: int = Field(default=8192, ge=512, le=2_000_000)
    max_tokens: int = Field(default=2048, ge=1, le=131072)
    temperature: float = Field(default=0.7, ge=0, le=2)
    timeout: int = Field(default=240, ge=5, le=600)
    stream: bool = True
    capabilities: list[Literal["text", "tools", "vision", "asr", "tts"]] = ["text"]
    voice: str = Field(default="alloy", max_length=200)
    speed: float = Field(default=1, ge=0.25, le=4)
    language: str = Field(default="", max_length=30)


def save_model(c, body, identity=None):
    provider = c.store.get("provider", body.provider_id)
    if provider["protocol"].startswith("native-"):
        raise ValueError(
            "Built-in voice runtimes use their existing model configuration. Add a compatible local/API connection for additional voice models"
        )
    if body.max_tokens >= body.context:
        raise ValueError("Maximum output must be less than the context size")
    if "tools" in body.capabilities and "text" not in body.capabilities:
        raise ValueError("Tool calling requires text capability")
    if provider["protocol"] not in {"openai", "native-asr", "native-tts"} and set(
        body.capabilities
    ) & {"asr", "tts"}:
        raise ValueError(
            "Voice API profiles require an OpenAI-compatible audio endpoint"
        )
    if identity in {*MANAGED_MODELS, "firered", "fish"}:
        raise ValueError(
            "Built-in model profiles are read-only; duplicate to customize"
        )
    if identity:
        c.store.get("model", identity)
    return c.store.put(
        "model", {**body.model_dump(), "id": identity or uuid.uuid4().hex}
    )


@router.post("/models", status_code=201)
async def add_model(body: ModelInput, request: Request):
    return save_model(control(request), body)


@router.put("/models/{identity}")
async def edit_model(identity: str, body: ModelInput, request: Request):
    return save_model(control(request), body, identity)


@router.delete("/models/{identity}")
async def delete_model(identity: str, request: Request):
    c = control(request)
    roles = c.store.get("roles", "global")
    if identity in {*MANAGED_MODELS, "firered", "fish"} or identity in [
        *roles["bindings"].values(),
        *roles.get("agents", {}).values(),
    ]:
        raise ValueError("Built-in or bound models cannot be deleted")
    c.store.remove("model", identity)
    return {"deleted": True}


class RolesInput(BaseModel):
    bindings: dict[str, str | None]
    agents: dict[str, str | None] = {}


@router.put("/roles")
async def roles(body: RolesInput, request: Request):
    c = control(request)
    if set(body.bindings) - set(ROLES) or set(body.agents) - set(AGENTS):
        raise ValueError("Unknown role or agent")
    value = {"id": "global", "bindings": body.bindings, "agents": body.agents}
    for role in ROLES:
        if body.bindings.get(role) or role in {
            "main",
            "decision",
            "code",
            "reasoning",
            "summary",
        }:
            c.store.resolve(role, value)
    for ref in body.agents.values():
        if ref:
            profile = c.store.resolve("main", value, ref)
            if "tools" not in profile["capabilities"]:
                raise ValueError("Agent overrides require tool capability")
    return c.store.put("roles", value)


class MainRoleInput(BaseModel):
    model_ref: str = Field(min_length=1, max_length=100)


@router.patch("/roles/main")
async def main_role(body: MainRoleInput, request: Request):
    c = control(request)
    value = c.store.get("roles", "global")
    value["bindings"]["main"] = body.model_ref
    c.store.resolve("main", value)
    return c.store.put("roles", value)


class PolicyInput(BaseModel):
    allow_cloud_context: bool = False
    allow_cloud_audio: bool = False


@router.put("/workspaces/{identity}/policy")
async def policy(identity: str, body: PolicyInput, request: Request):
    c = control(request)
    c.store.library.get("workspace", identity)
    return c.store.put("policy", {"id": identity, **body.model_dump()})


class PreferenceInput(BaseModel):
    values: dict


@router.put("/preferences/{identity}")
async def preferences(identity: str, body: PreferenceInput, request: Request):
    c = control(request)
    allowed = {
        "tools": {"workspace", "git", "desktop", "commands"},
        "search": {"enabled", "provider_id", "language", "region", "count"},
        "voice": {"language", "voice", "speed", "auto_speak"},
    }
    if identity not in allowed or set(body.values) - allowed[identity]:
        raise ValueError("Unknown preference")
    value = {**c.store.get("preferences", identity), **body.values}
    if identity == "tools" and any(
        not isinstance(value[k], bool) for k in allowed[identity]
    ):
        raise ValueError("Tool switches must be boolean")
    if identity == "search":
        if not isinstance(value["enabled"], bool) or not 1 <= int(value["count"]) <= 10:
            raise ValueError("Invalid search configuration")
        if value["enabled"] and c.store.get("provider", value["provider_id"])[
            "protocol"
        ] not in {"brave", "tavily", "searxng"}:
            raise ValueError("Select a search connection")
    if identity == "voice" and not 0.25 <= float(value["speed"]) <= 4:
        raise ValueError("Invalid speech speed")
    return c.store.put("preferences", value)


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)


@router.post("/search/test")
async def search_test(body: SearchInput, request: Request):
    c = control(request)
    return await search(c.store, body.query, c.store.get("preferences", "search"))


@router.get("/approvals")
async def approvals(request: Request):
    return control(request).store.list("approval")[-200:]


class DecisionInput(BaseModel):
    accept: bool


@router.post("/approvals/{identity}/decision")
async def approval_decision(identity: str, body: DecisionInput, request: Request):
    return control(request).broker.decide(identity, body.accept)


@router.get("/services")
async def services(request: Request):
    c = control(request)
    health = await c.adapters.health()
    settings = load_settings()
    from update_tools import installed_versions

    versions = await asyncio.to_thread(installed_versions, settings)
    result = [
        {
            "id": name,
            "ready": health.get(name, {}).get("ready", False),
            "owned": name in c.runtime.instances,
            "path": settings["executables"][name],
            "version": versions.get(name, versions.get("hermes_commit", "unknown")),
            "model_ref": c.store.get("roles", "global").get("agents", {}).get(name),
        }
        for name in AGENTS
    ]
    if any(p["id"] == "zonos3" for p in c.store.list("provider")):
        from .zonos import executable

        try:
            ready = await c.zonos.ready()
        except (httpx.HTTPError, ValueError, KeyError):
            ready = False
        result.append(
            {
                "id": "zonos3",
                "ready": ready,
                "owned": c.zonos.owned,
                "path": str(executable()),
                "version": "ZONOS3-Mini / 96d9e0ec",
                "model_ref": None,
            }
        )
    return result


class ServiceInput(BaseModel):
    action: Literal["start", "stop", "restart"]


@router.post("/services/{identity}")
async def service(identity: str, body: ServiceInput, request: Request):
    c = control(request)
    idle(c)
    if identity == "zonos3":
        async with c.gpu_lock:
            if body.action in {"stop", "restart"}:
                await c.zonos.close()
            if body.action in {"start", "restart"}:
                await c.zonos.ensure()
        return {"owned": c.zonos.owned}
    if identity not in AGENTS:
        raise ValueError("Unknown managed service")
    if body.action in {"stop", "restart"}:
        await c.runtime.stop(identity)
    if body.action in {"start", "restart"}:
        roles = c.store.get("roles", "global")
        profile = c.store.resolve(
            {"openclaw": "main", "opencode": "code", "hermes": "reasoning"}[identity],
            roles,
            roles.get("agents", {}).get(identity),
        )
        await c.runtime.ensure(identity, profile)
    if not c.runtime.instances:
        c.uncertain = False
    return {"owned": identity in c.runtime.instances}


@router.get("/logs/{identity}")
async def logs(identity: str, request: Request):
    if identity not in {*AGENTS, "updates", "workbench", "zonos3"}:
        raise ValueError("Unknown log")
    name = "updates.log" if identity == "updates" else identity + ".stderr.log"
    path = ROOT / "12_Logs/Workbench" / name
    if not path.exists():
        return {"text": ""}
    with path.open("rb") as source:
        source.seek(max(0, path.stat().st_size - 16000))
        text = source.read().decode("utf-8", errors="replace")
    c = control(request)
    for token in [
        *load_secrets().values(),
        *c.runtime.tokens.values(),
        c.store.desktop_token(),
    ]:
        if isinstance(token, str) and token:
            text = text.replace(token, "[redacted]")
    for provider in c.store.list("provider"):
        if provider.get("secret_ref"):
            key = c.store.key(provider)
            if key:
                text = text.replace(key, "[redacted]")
    return {"text": text}


@router.get("/diagnostics")
async def diagnostics(request: Request):
    c = control(request)
    return {
        "version": 1,
        "name": "JuiAI Terminal",
        "needs_restart": c.uncertain,
        "running_count": len(c.workers),
        "conversation_count": len(c.store.list("conversation")),
        "models": [
            {k: m.get(k) for k in ("name", "model_id", "capabilities", "enabled")}
            for m in c.store.list("model")
        ],
        "approvals": [
            {k: a[k] for k in ("action", "agent", "status")}
            for a in c.store.list("approval")[-50:]
        ],
    }


@router.get("/configuration/export")
async def export_configuration(request: Request):
    c = control(request)
    return {
        "version": 1,
        "providers": [
            {k: v for k, v in c.store.public_provider(p).items() if k != "has_key"}
            for p in c.store.list("provider")
        ],
        "models": c.store.list("model"),
        "roles": c.store.get("roles", "global"),
        "preferences": c.store.list("preferences"),
    }


class SpeechInput(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    workspace_id: str = "default"
    source_run_id: str | None = None


@router.post("/voice/transcribe")
async def transcribe(request: Request, workspace_id: str = "default"):
    c = control(request)
    audio = bytearray()
    async for chunk in request.stream():
        audio.extend(chunk)
        if len(audio) > 2_000_000:
            raise HTTPException(413, "Recording exceeds the limit")
    request.app.state.voice.validate_audio(bytes(audio))
    c.store.library.get("workspace", workspace_id)
    profile = c.store.resolve("asr")
    cloud_guard(profile, False, c.store.policy(workspace_id), audio=True)
    worker = asyncio.current_task()
    c.voice_workers.add(worker)
    try:
        text = await voice_transcribe(c, request.app.state.voice, profile, bytes(audio))
        return {"text": text}
    finally:
        c.voice_workers.discard(worker)


@router.post("/voice/speak")
async def speak(body: SpeechInput, request: Request):
    c = control(request)
    c.store.library.get("workspace", body.workspace_id)
    profile = c.store.resolve("tts")
    sensitive = False
    if body.source_run_id:
        source = c.store.get("run", body.source_run_id)
        if source["workspace_id"] != body.workspace_id:
            raise ValueError("Speech source belongs to another workspace")
        sensitive = source["sensitive"]
    cloud_guard(profile, sensitive, c.store.policy(body.workspace_id), audio=True)

    async def generate():
        return await voice_synthesize(c, request.app.state.voice, profile, body.text)

    worker = asyncio.create_task(generate())
    c.voice_workers.add(worker)
    request.app.state.speech.add(worker)
    worker.add_done_callback(c.voice_workers.discard)
    worker.add_done_callback(request.app.state.speech.discard)
    return Response(await asyncio.shield(worker), media_type="audio/wav")


class UpdateInput(BaseModel):
    stop_owned: bool = False


async def update_job(c, identity, apply):
    try:
        path = ROOT / "12_Logs/Workbench/updates.json"
        previous = path.stat().st_mtime_ns if path.exists() else None
        c.store.put("update", {"id": identity, "status": "running", "apply": apply})
        c.emit("update", id=identity, status="running", apply=apply)
        process = await asyncio.create_subprocess_exec(
            str(APP / ".venv/Scripts/python.exe"),
            str(APP / "update_tools.py"),
            *(["--apply"] if apply else []),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await process.communicate()
        result = (
            json.loads(path.read_text(encoding="utf-8-sig"))
            if process.returncode != 2
            and path.exists()
            and path.stat().st_mtime_ns != previous
            else {}
        )
        value = {
            "id": identity,
            "status": "completed"
            if process.returncode == 0
            else "deferred"
            if process.returncode == 2
            else "failed",
            "apply": apply,
            "result": result,
        }
        if process.returncode == 2:
            value["error"] = (
                "Another update/check is already running; nothing was repeated"
            )
        c.store.put("update", value)
        c.emit("update", **value)
    except Exception as error:  # noqa: BLE001 -- background maintenance must always publish a terminal result.
        c.store.put(
            "update",
            {"id": identity, "status": "failed", "error": type(error).__name__},
        )
        c.emit("update", id=identity, status="failed")
    finally:
        c.maintenance = False


async def begin_update(c, apply=False, stop_owned=False):
    idle(c)
    c.maintenance = True
    try:
        if apply and c.runtime.instances:
            if not stop_owned:
                raise ValueError(
                    "Confirm stopping this application's idle Agent services before updating"
                )
            await c.runtime.close()
        identity = uuid.uuid4().hex
        c.store.put("update", {"id": identity, "status": "queued", "apply": apply})
        worker = asyncio.create_task(update_job(c, identity, apply))
        c.voice_workers.add(worker)
        worker.add_done_callback(c.voice_workers.discard)
        return {"id": identity, "status": "queued"}
    except BaseException:
        c.maintenance = False
        raise


@router.post("/updates/check", status_code=202)
async def check_updates(request: Request):
    return await begin_update(control(request))


@router.post("/updates/apply", status_code=202)
async def apply_updates(body: UpdateInput, request: Request):
    return await begin_update(control(request), True, body.stop_owned)


@router.get("/updates")
async def updates(request: Request):
    return [
        localize_update(item) for item in control(request).store.list("update")[-20:]
    ]


def agent_identity(request):
    c = control(request)
    value = request.headers.get("authorization", "").removeprefix("Bearer ")
    for agent, token in c.runtime.tokens.items():
        if hmac.compare_digest(value, token):
            return agent
    raise HTTPException(401, "Invalid agent credential")


class ToolInput(BaseModel):
    action: str
    arguments: dict = {}


@internal.post("/tools")
async def tool(body: ToolInput, request: Request):
    return await control(request).broker.call(
        agent_identity(request), body.action, body.arguments
    )


@internal.get("/model/v1/models")
async def gateway_models(request: Request):
    agent_identity(request)
    return {
        "object": "list",
        "data": [
            {"id": load_settings()["model"]["id"], "object": "model", "owned_by": "jui"}
        ],
    }


@internal.post("/model/v1/chat/completions")
async def gateway(request: Request):
    c = control(request)
    agent = agent_identity(request)
    _, run = c.broker.context(agent)
    profile = run["profiles"][agent]
    cloud_guard(profile, run["sensitive"], run["policy"])
    body = await request.json()
    messages = body.get("messages")
    if not isinstance(messages, list) or len(json.dumps(messages)) > 2_000_000:
        raise ValueError("Invalid model messages")

    async def generate():
        async with c.engine.model_session(profile):
            response = await c.engine.completion(
                profile,
                messages,
                stream=bool(body.get("stream")),
                tools=body.get("tools"),
                tool_choice=body.get("tool_choice"),
            )
            if body.get("stream"):
                try:
                    async for chunk in response:
                        value = chunk.model_dump(exclude_none=True)
                        for choice in value.get("choices", []):
                            choice.get("delta", {}).pop("reasoning_content", None)
                        yield "data: " + json.dumps(value) + "\n\n"
                    yield "data: [DONE]\n\n"
                finally:
                    close = getattr(response, "aclose", None)
                    if close:
                        await close()
            else:
                value = response.model_dump(exclude_none=True)
                for choice in value.get("choices", []):
                    choice.get("message", {}).pop("reasoning_content", None)
                yield json.dumps(value)

    if body.get("stream"):
        return StreamingResponse(generate(), media_type="text/event-stream")
    async for value in generate():
        return Response(value, media_type="application/json")
