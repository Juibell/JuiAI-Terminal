import asyncio
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jui.control import Control
from jui.control_api import (
    ModelInput,
    ProviderInput,
    save_model,
    save_provider,
)
from jui.control_models import cloud_guard, endpoint
from jui.control_store import ControlStore, Vault
from jui.library import Library
from jui.web import app


class MemoryVault:
    def __init__(self):
        self.values = {}

    def get(self, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def put(self, name, value):
        self.values[name] = value


@pytest.fixture
def store(tmp_path, monkeypatch):
    from jui import control_store

    settings = control_store.load_settings()
    settings["local_presets"] = ["bonsai", "qwen38", "firered", "fish"]
    monkeypatch.setattr(control_store, "load_settings", lambda: settings)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    library = Library(tmp_path, workspace)
    return ControlStore(tmp_path, library, MemoryVault())


@pytest.fixture
def center(store, monkeypatch):
    from jui import plugins

    monkeypatch.setattr(plugins, "enabled", lambda _: None)
    return Control(asyncio.Lock(), store)


@pytest.fixture
def client(center, monkeypatch):
    monkeypatch.setattr(app.state, "control", center, raising=False)
    monkeypatch.setattr(app.state, "tasks", SimpleNamespace(workers={}), raising=False)
    monkeypatch.setattr(app.state, "speech", set(), raising=False)
    return TestClient(
        app,
        headers={
            "Authorization": "Bearer " + center.store.desktop_token(),
            "X-Jui-Request": "1",
        },
    )


def test_public_first_start_has_no_placeholder_models(tmp_path):
    store = ControlStore(tmp_path, Library(tmp_path, tmp_path), MemoryVault())
    assert store.list("provider") == []
    assert store.list("model") == []
    assert store.get("roles", "global")["bindings"] == {}


def test_dpapi_roundtrip_no_plaintext(tmp_path):
    vault = Vault(tmp_path)
    vault.put("test", "not-a-real-api-key")
    assert vault.get("test") == "not-a-real-api-key"
    assert b"not-a-real-api-key" not in vault.path("test").read_bytes()
    with pytest.raises(ValueError):
        vault.get("../private")


def test_provider_secret_never_in_database_or_public_state(center, client):
    body = {
        "name": "Cloud",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "unit-test-private-key",
    }
    response = client.post("/api/v1/providers", json=body)
    assert response.status_code == 201
    identity = response.json()["id"]
    assert response.json()["has_key"]
    assert "unit-test-private-key" not in client.get("/api/v1/state").text
    assert b"unit-test-private-key" not in center.store.path.read_bytes()
    first = center.store.get("provider", identity)
    client.put(
        "/api/v1/providers/" + identity, json={**body, "api_key": "new-test-key"}
    )
    assert center.store.key(first) == "unit-test-private-key"
    assert center.store.key(center.store.get("provider", identity)) == "new-test-key"


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com/v1",
        "https://user:key@example.com/v1",
        "https://example.com/v1?api_key=x",
        "http://127.0.0.1:12394/internal/model/v1",
    ],
)
def test_bad_endpoint_rejected(value):
    with pytest.raises(ValueError):
        endpoint(value, "openai")


def test_endpoint_locality_and_cloud_policy():
    assert endpoint("http://localhost:8088/v1", "openai")[1]
    assert not endpoint("https://example.com/v1", "openai")[1]
    remote = {"provider": {"local": False}}
    with pytest.raises(ValueError, match="not authorized"):
        cloud_guard(remote, True, {})
    with pytest.raises(ValueError, match="audio"):
        cloud_guard(remote, False, {}, audio=True)
    cloud_guard(remote, True, {"allow_cloud_context": True})


def test_desktop_auth_origin_and_internal_agent_auth(client):
    assert client.get("/api/v1/state").status_code == 200
    assert (
        client.get(
            "/api/v1/state", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/v1/state", headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/internal/tools", json={"action": "read_file", "arguments": {"path": "a"}}
        ).status_code
        == 401
    )
    assert client.get("/api/health").status_code == 200


def test_role_capabilities_and_builtin_preservation(center, client):
    assert center.store.resolve("decision")["id"] == "bonsai"
    assert center.store.resolve("asr")["id"] == "firered"
    with pytest.raises(ValueError):
        center.store.resolve("vision")
    roles = center.store.get("roles", "global")
    roles["bindings"]["vision"] = "fish"
    response = client.put("/api/v1/roles", json={"bindings": roles["bindings"]})
    assert response.status_code == 400
    assert client.delete("/api/v1/models/bonsai").status_code == 400
    assert center.store.resolve("main")["model_id"] == "jui-bonsai2"


def test_atomic_batch_delete_refuses_active_conversation(center, client):
    first = center.store.conversation()
    second = center.store.conversation()
    center.store.put(
        "run", {"id": "busy", "conversation_id": second["id"], "status": "running"}
    )
    result = client.post(
        "/api/v1/conversations/delete", json={"ids": [first["id"], second["id"]]}
    )
    assert result.status_code == 409
    assert len(center.store.list("conversation")) == 2


def test_event_cursor_never_reexecutes_tasks(store):
    a = store.event("delta", text="first")
    b = store.event("delta", text="second")
    assert [e["seq"] for e in store.events(a)] == [b]
    assert store.events(b) == []


def test_legacy_import_is_idempotent_and_preserves_ids(store):
    with sqlite3.connect(store.directory / "tasks.sqlite") as db:
        db.execute("CREATE TABLE tasks(id TEXT,data TEXT)")
        db.execute(
            "INSERT INTO tasks VALUES(?,?)",
            (
                "old-one",
                json.dumps(
                    {
                        "id": "old-one",
                        "prompt": "Old question",
                        "output": "Old answer",
                        "status": "completed",
                        "created": "2026-01-01",
                        "mode": "chat",
                    }
                ),
            ),
        )
    store.import_legacy()
    store.import_legacy()
    assert len(store.list("conversation")) == 1
    assert {m["id"] for m in store.messages("legacy-old-one")} == {
        "old-one-user",
        "old-one-assistant",
    }
    assert store.messages("legacy-old-one")[1]["content"] == "Old answer"


def stage(center, task):
    profile = center.store.resolve("main")
    root = center.store.library.active_workspace()["path"]
    center.store.put(
        "run",
        {
            "id": "test-run",
            "workspace": root,
            "workspace_id": "default",
            "profiles": {"openclaw": profile},
            "policy": {},
            "status": "running",
            "sensitive": False,
        },
    )
    center.workers["test-run"] = task
    center.active_stage = {
        "agent": "openclaw",
        "run_id": "test-run",
        "nonce": "one-stage",
    }
    return root


async def pending(center):
    for _ in range(100):
        if center.broker.pending:
            return next(iter(center.broker.pending))
        await asyncio.sleep(0.01)
    raise AssertionError("No approval was requested")


def test_write_requires_single_use_approval_and_exact_diff(center):
    async def run():
        from pathlib import Path

        root = Path(stage(center, asyncio.current_task()))
        target = root / "a.txt"
        target.write_text("before")
        worker = asyncio.create_task(
            center.broker.call(
                "openclaw", "write_file", {"path": "a.txt", "content": "after"}
            )
        )
        identity = await pending(center)
        assert target.read_text() == "before"
        assert "before" in center.store.get("approval", identity)["preview"]
        center.broker.decide(identity, True)
        assert (await worker)["success"]
        assert target.read_text() == "after"
        assert center.store.get("approval", identity)["status"] == "executed"
        with pytest.raises(ValueError):
            center.broker.decide(identity, True)

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["reject", "revoke", "file_changed", "stage_changed", "tool_disabled"]
)
def test_write_rejection_revocation_and_toctou(center, change):
    async def run():
        from pathlib import Path

        root = Path(stage(center, asyncio.current_task()))
        target = root / "a.txt"
        target.write_text("before")
        worker = asyncio.create_task(
            center.broker.call(
                "openclaw", "write_file", {"path": "a.txt", "content": "after"}
            )
        )
        identity = await pending(center)
        if change == "file_changed":
            target.write_text("user edit")
        if change == "stage_changed":
            center.active_stage["nonce"] = "other-stage"
        if change == "tool_disabled":
            preferences = center.store.get("preferences", "tools")
            center.store.put("preferences", {**preferences, "workspace": False})
        if change == "revoke":
            center.broker.revoke("test-run")
        elif change == "stage_changed":
            with pytest.raises(ValueError):
                center.broker.decide(identity, True)
            center.broker.revoke("test-run")
        else:
            center.broker.decide(identity, change != "reject")
        with pytest.raises(ValueError):
            await worker
        assert target.read_text() != "after"

    asyncio.run(run())


def test_workspace_path_and_agent_boundary(center):
    async def run():
        stage(center, asyncio.current_task())
        with pytest.raises(ValueError):
            await center.broker.call("hermes", "read_file", {"path": "a.txt"})
        for path in ("../outside.txt", ".runtime/secrets.json", "C:/Windows/test.txt"):
            with pytest.raises(ValueError):
                await center.broker.call(
                    "openclaw", "write_file", {"path": path, "content": "x"}
                )
        assert not center.broker.pending

    asyncio.run(run())


def test_commands_are_approved_and_bounded(center):
    async def run():
        stage(center, asyncio.current_task())
        worker = asyncio.create_task(
            center.broker.call(
                "openclaw",
                "run_command",
                {
                    "argv": [sys.executable, "-c", "print('approved-test')"],
                    "timeout": 10,
                },
            )
        )
        identity = await pending(center)
        center.broker.decide(identity, True)
        value = await worker
        assert value["success"]
        assert "approved-test" in value["output"]

    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
def test_protocol_parameters_no_fallback_or_retry(center, monkeypatch, protocol):
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    provider = save_provider(
        center,
        ProviderInput(
            name="test",
            protocol=protocol,
            base_url="https://example.com/v1",
            api_key="fake-key",
        ),
    )
    model = save_model(
        center, ModelInput(name="test", provider_id=provider["id"], model_id="test-id")
    )
    profile = {**model, "provider": center.store.get("provider", provider["id"])}
    assert (
        asyncio.run(center.engine.text(profile, [{"role": "user", "content": "Hi"}]))
        == "OK"
    )
    assert calls[0]["model"] == protocol + "/test-id"
    assert calls[0]["num_retries"] == 0
    assert calls[0]["api_key"] == "fake-key"


def test_model_failure_is_sanitized(center, monkeypatch):
    async def completion(**kwargs):
        raise RuntimeError("PRIVATE-KEY or private remote error")

    async def no_model(_):
        pass

    monkeypatch.setattr(center, "ensure_model", no_model)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    with pytest.raises(RuntimeError) as error:
        asyncio.run(
            center.engine.text(
                center.store.resolve("main"), [{"role": "user", "content": "Hi"}]
            )
        )
    assert "PRIVATE-KEY" not in str(error.value)


def test_qwen_backfill_and_main_role_patch_preserve_existing_roles(center, client):
    roles = center.store.get("roles", "global")
    roles["bindings"]["decision"] = "bonsai"
    roles["agents"]["hermes"] = "bonsai"
    center.store.put("roles", roles)
    center.store.remove("model", "qwen38")
    center.store.remove("provider", "qwen38")
    center.store.seed_qwen()
    assert center.store.resolve("main")["id"] == "bonsai"
    assert client.delete("/api/v1/models/qwen38").status_code == 400
    assert client.delete("/api/v1/providers/qwen38").status_code == 400
    response = client.patch("/api/v1/roles/main", json={"model_ref": "qwen38"})
    assert response.status_code == 200
    assert response.json()["bindings"]["decision"] == "bonsai"
    assert response.json()["agents"] == {"hermes": "bonsai"}
    selected = center.store.resolve("main")
    assert selected["id"] == "qwen38" and selected["model_id"] == "jui-qwen38-27b"
    assert selected["max_tokens"] < selected["context"] == 4096
    assert selected["capabilities"] == ["text", "tools"]
    assert (
        client.patch("/api/v1/roles/main", json={"model_ref": "fish"}).status_code
        == 400
    )
    assert center.store.resolve("main")["id"] == "qwen38"


def test_qwen_upgrade_upgrades_stock_text_only_profile_and_keeps_customizations(center):
    model = center.store.get("model", "qwen38")
    center.store.put("model", {**model, "capabilities": ["text"]})
    center.store.seed_qwen()
    assert center.store.get("model", "qwen38")["capabilities"] == ["text", "tools"]
    custom = center.store.put(
        "model", {**model, "capabilities": ["text"], "temperature": 0.3}
    )
    center.store.seed_qwen()
    assert center.store.get("model", "qwen38") == custom
    assert center.store.resolve("main")["id"] == "bonsai"


def test_qwen_normal_chat_prepares_and_generates_under_one_gpu_lock(
    center, monkeypatch
):
    roles = center.store.get("roles", "global")
    roles["bindings"]["main"] = "qwen38"
    center.store.put("roles", roles)
    calls = []

    async def prepare(profile):
        assert center.gpu_lock.locked()
        calls.append(profile["id"])

    async def completion(profile, messages, *, stream):
        assert center.gpu_lock.locked()
        assert profile["model_id"] == "jui-qwen38-27b" and stream
        assert messages[-1]["content"] == "Hello"

        async def deltas():
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="你好"))]
            )

        return deltas()

    monkeypatch.setattr(center, "ensure_model", prepare)
    monkeypatch.setattr(center.engine, "completion", completion)

    async def run():
        conversation = center.store.conversation()
        value = center.submit(
            conversation["id"], "Hello", mode="chat", use_memory=False
        )
        await center.workers[value["id"]]
        assert center.store.get("run", value["id"])["status"] == "completed"
        assert center.store.messages(conversation["id"])[-1]["content"] == "你好"

    asyncio.run(run())
    assert calls == ["qwen38"]


def test_orchestration_and_frozen_model_snapshot(center, monkeypatch):
    calls = []

    async def no_model(_):
        pass

    async def text(profile, messages, on_delta=None):
        calls.append(profile["id"])
        if on_delta:
            on_delta("hello")
        return "hello"

    monkeypatch.setattr(center, "ensure_model", no_model)
    monkeypatch.setattr(center.engine, "text", text)

    async def run():
        conversation = center.store.conversation()
        result = center.submit(conversation["id"], "Hi", mode="chat", use_memory=False)
        roles = center.store.get("roles", "global")
        roles["bindings"]["main"] = "disabled-new-model"
        center.store.put("roles", roles)
        await center.workers[result["id"]]
        assert center.store.get("run", result["id"])["status"] == "completed"
        assert center.store.messages(conversation["id"])[-1]["content"] == "hello"

    asyncio.run(run())
    assert calls == ["bonsai"]


@pytest.mark.parametrize("model_ref", ["bonsai", "qwen38"])
def test_automatic_route_requests_bounded_structured_output(
    center, monkeypatch, model_ref
):
    async def no_model(_):
        pass

    calls = []
    prompt = "主脑切换验证：17 加 25 等于多少？请只回复数字。"
    roles = center.store.get("roles", "global")
    roles["bindings"]["main"] = model_ref
    center.store.put("roles", roles)

    async def text(profile, messages, on_delta=None, response_format=None):
        calls.append(response_format)
        assert profile["id"] == model_ref
        assert messages[-1] == {"role": "user", "content": prompt}
        if response_format:
            assert profile["temperature"] == 0
            schema = response_format["json_schema"]["schema"]
            assert schema["properties"]["agents"]["maxItems"] == 3
            system = messages[0]["content"]
            if model_ref == "qwen38":
                assert (
                    "arithmetic" in system
                    and "Delegate only when the user requests" in system
                )
            else:
                assert system.startswith("Route the user request.")
            return '{"agents":[]}'
        return "42"

    monkeypatch.setattr(center, "ensure_model", no_model)
    monkeypatch.setattr(center.engine, "text", text)

    async def run():
        value = center.submit(
            center.store.conversation()["id"], prompt, use_memory=False
        )
        await center.workers[value["id"]]
        run = center.store.get("run", value["id"])
        assert run["status"] == "completed" and run["output"] == "42"
        assert not center.runtime.instances

    asyncio.run(run())
    assert len(calls) == 2


def test_invalid_automatic_plan_executes_no_agent(center, monkeypatch):
    async def no_model(_):
        pass

    async def text(*args, **kwargs):
        return '{"agents":["openclaw","openclaw","unknown"]}'

    monkeypatch.setattr(center, "ensure_model", no_model)
    monkeypatch.setattr(center.engine, "text", text)

    async def run():
        result = center.submit(
            center.store.conversation()["id"], "Hi", use_memory=False
        )
        await center.workers[result["id"]]
        assert center.store.get("run", result["id"])["status"] == "failed"
        assert not center.runtime.instances

    asyncio.run(run())


def test_update_does_not_stop_services_without_consent(center, client):
    center.runtime.instances["openclaw"] = object()
    result = client.post("/api/v1/updates/apply", json={"stop_owned": False})
    assert result.status_code == 400
    assert "openclaw" in center.runtime.instances
    assert not center.maintenance


def test_cloud_voice_is_blocked_before_http(center, client, monkeypatch):
    provider = save_provider(
        center,
        ProviderInput(
            name="audio", protocol="openai", base_url="https://example.com/v1"
        ),
    )
    model = save_model(
        center,
        ModelInput(
            name="speech",
            provider_id=provider["id"],
            model_id="tts",
            capabilities=["tts"],
        ),
    )
    roles = center.store.get("roles", "global")
    roles["bindings"]["tts"] = model["id"]
    center.store.put("roles", roles)
    assert client.post("/api/v1/voice/speak", json={"text": "test"}).status_code == 400


def test_export_and_diagnostics_exclude_keys_and_prompts(client):
    for path in ("/api/v1/configuration/export", "/api/v1/diagnostics"):
        result = client.get(path)
        assert result.status_code == 200
        assert "secret_ref" not in result.text
        assert "api_key" not in result.text


def test_official_update_defers_running_agents(tmp_path, monkeypatch):
    import update_tools

    monkeypatch.setattr(update_tools, "ROOT", tmp_path)
    monkeypatch.setattr(update_tools, "release", lambda _client, _repo: "1.2.3")
    monkeypatch.setattr(
        update_tools,
        "load_settings",
        lambda: {"executables": {"hermes": str(tmp_path / ".venv/Scripts/python.exe")}},
    )
    monkeypatch.setattr(
        update_tools,
        "installed_versions",
        lambda _settings: {
            "openclaw": "1.0.0",
            "opencode": "1.0.0",
            "hermes_commit": "abc",
        },
    )
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return '[{"ProcessId":123,"Name":"node.exe"}]'

    monkeypatch.setattr(update_tools, "command", command)
    monkeypatch.setattr(sys, "argv", ["update_tools.py", "--apply"])
    update_tools.main()
    result = json.loads((tmp_path / "12_Logs/Workbench/updates.json").read_text())
    assert result["deferred"] and not result["updated"]
    assert len(calls) == 1


def test_cancel_before_worker_starts_is_terminal(center):
    async def run():
        conversation = center.store.conversation()
        value = center.submit(
            conversation["id"], "do not execute", mode="chat", use_memory=False
        )
        await center.cancel(value["id"])
        assert center.store.get("run", value["id"])["status"] == "cancelled"

    asyncio.run(run())


def test_queued_task_does_not_run_after_uncertain_state(center, monkeypatch):
    calls = []

    async def ensure_model(profile):
        calls.append(profile)

    monkeypatch.setattr(center, "ensure_model", ensure_model)

    async def run():
        await center.lock.acquire()
        value = center.submit(
            center.store.conversation()["id"], "queued", mode="chat", use_memory=False
        )
        center.uncertain = True
        center.lock.release()
        await center.workers[value["id"]]
        assert center.store.get("run", value["id"])["status"] == "failed"
        assert calls == []

    asyncio.run(run())


def test_restart_interrupts_without_replay(center):
    center.store.put("run", {"id": "power-loss", "status": "running"})
    center.store.put("approval", {"id": "pending-power-loss", "status": "approved"})
    restarted = Control(asyncio.Lock(), center.store)
    assert restarted.store.get("run", "power-loss")["status"] == "interrupted"
    assert restarted.store.get("approval", "pending-power-loss")["status"] == "expired"
    assert restarted.workers == {}


def test_validation_error_does_not_echo_bad_key(client):
    response = client.post(
        "/api/v1/providers",
        json={
            "name": "test",
            "protocol": "invalid",
            "base_url": "https://example.com",
            "api_key": {"secret": "do-not-echo"},
        },
    )
    assert response.status_code == 422
    assert "do-not-echo" not in response.text


def test_deleted_legacy_conversation_is_not_imported_again(center, client):
    with sqlite3.connect(center.store.directory / "tasks.sqlite") as db:
        db.execute("CREATE TABLE tasks(id TEXT,data TEXT)")
        db.execute(
            "INSERT INTO tasks VALUES(?,?)",
            (
                "old-deleted",
                json.dumps(
                    {
                        "id": "old-deleted",
                        "prompt": "old",
                        "output": "answer",
                        "status": "completed",
                        "mode": "chat",
                    }
                ),
            ),
        )
    center.store.import_legacy()
    assert (
        client.post(
            "/api/v1/conversations/delete", json={"ids": ["legacy-old-deleted"]}
        ).status_code
        == 200
    )
    center.store.import_legacy()
    assert center.store.list("conversation") == []
    assert center.store.messages("legacy-old-deleted") == []


def test_busy_updater_does_not_reuse_an_old_report(center, monkeypatch, tmp_path):
    from jui import control_api

    report = tmp_path / "12_Logs/Workbench/updates.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"updated": ["old-unrelated-update"]}))
    monkeypatch.setattr(control_api, "ROOT", tmp_path)

    class BusyProcess:
        returncode = 2

        async def communicate(self):
            return b"already running", b""

    async def process(*_args, **_kwargs):
        return BusyProcess()

    monkeypatch.setattr(control_api.asyncio, "create_subprocess_exec", process)
    asyncio.run(control_api.update_job(center, "busy-update", False))
    result = center.store.get("update", "busy-update")
    assert result["status"] == "deferred"
    assert result["result"] == {}


def test_workspace_data_cannot_leak_through_agent_handoff(center, monkeypatch):
    provider = save_provider(
        center,
        ProviderInput(
            name="cloud", protocol="openai", base_url="https://example.com/v1"
        ),
    )
    model = save_model(
        center,
        ModelInput(name="cloud-main", provider_id=provider["id"], model_id="cloud"),
    )
    roles = center.store.get("roles", "global")
    roles["bindings"]["main"] = model["id"]
    roles["agents"]["openclaw"] = "bonsai"
    center.store.put("roles", roles)

    async def no_model(_):
        pass

    async def no_runtime(*_):
        pass

    async def agent_run(agent, *_args, **_kwargs):
        value = await center.broker.call(agent, "git_status", {})
        return json.dumps(value)

    monkeypatch.setattr(center, "ensure_model", no_model)
    monkeypatch.setattr(center.runtime, "ensure", no_runtime)
    monkeypatch.setattr(center.adapters, "run", agent_run)

    async def run():
        value = center.submit(
            center.store.conversation()["id"],
            "check repository",
            mode="general",
            use_memory=False,
        )
        await center.workers[value["id"]]
        result = center.store.get("run", value["id"])
        assert result["sensitive"]
        assert result["status"] == "failed"
        assert "not authorized" in result["error"]

    asyncio.run(run())
