import asyncio
import hmac
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import plugins
from .config import load_settings
from .control import Control
from .control_api import internal as control_internal
from .control_api import router as control_router
from .control_debug import router as debug_router
from .control_voice_processing import router as voice_processing_router
from .language import active_language, negotiate_language, translate
from .voice import Voice

gpu_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app):
    app.state.voice = Voice(gpu_lock)
    app.state.speech = set()
    app.state.control = Control(gpu_lock)
    app.state.voice.prepare_native_voice = app.state.control.zonos.close
    yield
    await app.state.control.close()
    # Speech is intentionally drained so its transient backend output is cleaned.
    await asyncio.gather(*app.state.speech, return_exceptions=True)


app = FastAPI(
    title="JuiAI Terminal",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.exception_handler(StarletteHTTPException)
async def http_failure(request, error):
    if request.url.path.startswith(("/api/v1/", "/internal/")):
        return JSONResponse(
            {
                "error": {
                    "code": "request_failed",
                    "message": translate(str(error.detail)),
                }
            },
            status_code=error.status_code,
        )
    return JSONResponse(
        {"detail": translate(error.detail)}, status_code=error.status_code
    )


@app.exception_handler(RequestValidationError)
async def validation_failure(request, error):
    if request.url.path.startswith(("/api/v1/", "/internal/")):
        return JSONResponse(
            {
                "error": {
                    "code": "validation_failed",
                    "message": translate("Invalid request fields"),
                    "fields": [list(e["loc"]) for e in error.errors()],
                }
            },
            status_code=422,
        )
    return JSONResponse(
        {"detail": translate("Invalid request fields")}, status_code=422
    )


@app.exception_handler(httpx.HTTPError)
@app.exception_handler(RuntimeError)
async def upstream_failure(request, error):
    return JSONResponse(
        {
            "error": {
                "code": "upstream_failed",
                "message": translate(
                    type(error).__name__
                    + ": local service or external API failed; no retry was performed"
                ),
            }
        },
        status_code=502,
    )


@app.exception_handler(KeyError)
async def missing_item(request, error):
    if request.url.path.startswith(("/api/v1/", "/internal/")):
        return JSONResponse(
            {"error": {"code": "not_found", "message": translate("Item not found")}},
            status_code=404,
        )
    return JSONResponse({"detail": translate("Item not found")}, status_code=404)


@app.exception_handler(ValueError)
async def invalid_item(request, error):
    if request.url.path.startswith(("/api/v1/", "/internal/")):
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": translate(str(error))}},
            status_code=400,
        )
    return JSONResponse({"detail": translate(str(error))}, status_code=400)


def library(request):
    return request.app.state.control.store.library


@app.middleware("http")
async def local_boundary(request, call_next):
    port = load_settings()["port"]
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", "testserver"}
    if request.headers.get("host", "").lower() not in hosts:
        return JSONResponse(
            {"detail": translate("Invalid local host")}, status_code=403
        )
    origin = request.headers.get("origin")
    if origin and origin not in {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        "http://testserver",
    }:
        return JSONResponse(
            {"detail": translate("Cross-origin request rejected")}, status_code=403
        )
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        center = getattr(request.app.state, "control", None)
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if (
            not center
            or not bearer
            or not hmac.compare_digest(bearer, center.store.desktop_token())
        ):
            return JSONResponse(
                {"detail": translate("Desktop authentication required")},
                status_code=401,
            )
    response = await call_next(request)
    response.headers.update(
        {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Permissions-Policy": "camera=(), microphone=(self)",
        }
    )
    return response


@app.middleware("http")
async def interface_language(request, call_next):
    language = negotiate_language(request.headers.get("accept-language"))
    token = active_language.set(language)
    try:
        response = await call_next(request)
        response.headers["Content-Language"] = language
        response.headers.add_vary_header("Accept-Language")
        return response
    finally:
        active_language.reset(token)


@app.get("/")
async def index():
    return {"name": "JuiAI Terminal", "interface": translate("Windows desktop")}


@app.get("/api/health")
async def health():
    return {
        "name": "JuiAI Terminal",
        "ready": True,
        "control_api": 1,
        "native": True,
    }


@app.get("/api/workspace")
async def workspace(request: Request, path: str = "."):
    try:
        root = library(request).active_workspace()["path"]
        return plugins.list_files(path, root=root)
    except (ValueError, OSError) as error:
        raise HTTPException(400, "Directory is unavailable or not permitted") from error


@app.get("/api/workspace/file")
async def workspace_file(request: Request, path: str):
    try:
        root = library(request).active_workspace()["path"]
        return plugins.read_file(path, root=root)
    except (ValueError, OSError) as error:
        raise HTTPException(400, "File is unavailable or not permitted") from error


@app.get("/api/git")
async def git(request: Request):
    return await asyncio.to_thread(
        plugins.git_status, library(request).active_workspace()["path"]
    )


@app.get("/api/workspaces")
async def workspaces(request: Request):
    store = library(request)
    return {
        "items": store.list("workspace"),
        "selected": store.preferences()["workspace_id"],
    }


class WorkspaceInput(BaseModel):
    name: str = Field(default="", max_length=80)
    path: str = Field(min_length=1, max_length=1000)


@app.post("/api/workspaces", status_code=201)
async def add_workspace(body: WorkspaceInput, request: Request):
    try:
        return library(request).add_workspace(body.name, body.path)
    except OSError as error:
        raise HTTPException(
            400, "Folder does not exist or cannot be accessed"
        ) from error


@app.post("/api/workspaces/{identity}/select")
async def select_workspace(identity: str, request: Request):
    try:
        return library(request).select_workspace(identity)
    except OSError as error:
        raise HTTPException(400, "Folder is unavailable") from error


@app.delete("/api/workspaces/{identity}")
async def remove_workspace(identity: str, request: Request):
    library(request).delete("workspace", identity)
    return {"removed": True}


class MemoryInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=2000)
    category: str = Field(default="fact", pattern=r"^(fact|preference|project)$")
    workspace_id: str | None = Field(default=None, max_length=80)
    enabled: bool = True
    pinned: bool = False


@app.get("/api/memories")
async def memories(request: Request):
    store = library(request)
    return {
        "items": store.list("memory"),
        "enabled": store.preferences()["memory_enabled"],
    }


@app.post("/api/memories", status_code=201)
async def add_memory(body: MemoryInput, request: Request):
    return library(request).save_memory(body.model_dump())


@app.put("/api/memories/{identity}")
async def edit_memory(identity: str, body: MemoryInput, request: Request):
    return library(request).save_memory(body.model_dump(), identity)


@app.delete("/api/memories/{identity}")
async def remove_memory(identity: str, request: Request):
    library(request).delete("memory", identity)
    return {"removed": True}


class MemoryPreference(BaseModel):
    enabled: bool


@app.put("/api/preferences/memory")
async def memory_preference(body: MemoryPreference, request: Request):
    return library(request).preferences(memory_enabled=body.enabled)


app.include_router(control_router)
app.include_router(debug_router)
app.include_router(voice_processing_router)
app.include_router(control_internal)
