"""Interface localization must preserve conversation and provider data exactly."""

import pytest
import test_control as shared

from jui import config
from jui.language import negotiate_language

client = shared.client
center = shared.center
store = shared.store


@pytest.mark.parametrize(
    ("header", "language"),
    [
        ("en-US", "en-US"),
        ("zh-Hans", "zh-CN"),
        ("zh-Hans-CN", "zh-CN"),
        ("en;q=0.2, zh-CN;q=0.8", "zh-CN"),
        ("zh-CN;q=0, en", "en-US"),
        ("fr,zh-Hant", "en-US"),
    ],
)
def test_language_negotiation(header, language):
    assert negotiate_language(header) == language


def test_header_overrides_preference_and_localizes_errors(client):
    path = config.STATE / "ui-language.txt"
    assert not path.exists()
    missing = "/api/v1/conversations/missing"
    assert client.get(missing).json()["error"]["message"] == "Item not found"
    path.write_text("zh-CN\n", encoding="utf-8-sig")
    response = client.get(missing)
    assert response.headers["Content-Language"] == "zh-CN"
    assert response.json() == {
        "error": {"code": "not_found", "message": "找不到所请求的项目。"}
    }
    assert "Accept-Language" in response.headers["Vary"]
    response = client.get(missing, headers={"Accept-Language": "en-US"})
    assert response.json()["error"]["message"] == "Item not found"
    assert response.headers["Content-Language"] == "en-US"
    denied = client.get("/api/workspaces", headers={"Authorization": "Bearer invalid"})
    assert (
        denied.status_code == 401
        and denied.json()["detail"] == "需要桌面客户端身份验证。"
    )
    invalid = client.post("/api/v1/models", json={})
    assert invalid.json()["error"]["message"] == "请求字段无效。"
    assert invalid.json()["error"]["fields"][0][0] == "body"
    path.write_text("broken", encoding="utf-8")
    assert client.get(missing).headers["Content-Language"] == "en-US"


def test_translates_only_service_errors_and_preserves_user_data(
    client, center, tmp_path
):
    value = "Task queue is full"
    stored = center.store
    workspace = stored.library.add_workspace(value, str(tmp_path))
    provider = client.post(
        "/api/v1/providers",
        json={
            "name": value,
            "protocol": "openai",
            "base_url": "http://127.0.0.1:8765/v1",
        },
    ).json()["id"]
    model = client.post(
        "/api/v1/models",
        json={"provider_id": provider, "model_id": value, "name": value},
    ).json()["id"]
    conversation = stored.conversation(workspace["id"])["id"]
    stored.put(
        "message",
        {
            "id": "user",
            "run_id": "example",
            "role": "user",
            "content": value,
            "conversation_id": conversation,
            "created": "2025-01-01",
        },
    )
    stored.put(
        "message",
        {
            "id": "assistant",
            "run_id": "example",
            "role": "assistant",
            "content": value,
            "conversation_id": conversation,
            "created": "2025-01-02",
        },
    )
    stored.put(
        "run",
        {
            "id": "example",
            "conversation_id": conversation,
            "status": "failed",
            "error": value,
            "prompt": value,
            "output": value,
            "results": [{"agent": "opencode", "output": value}],
        },
    )
    client.headers["Accept-Language"] = "zh-Hans"
    state = client.get("/api/v1/state").json()
    assert next(p for p in state["providers"] if p["id"] == provider)["name"] == value
    model_data = next(m for m in state["models"] if m["id"] == model)
    assert model_data["name"] == model_data["model_id"] == value
    assert (
        next(w for w in state["workspaces"] if w["id"] == workspace["id"])["name"]
        == value
    )
    history = client.get("/api/v1/conversations/" + conversation).json()
    assert [m["content"].encode() for m in history["messages"]] == [value.encode()] * 2
    for run in (history["runs"][0], client.get("/api/v1/runs/example").json()):
        assert run["error"] == "任务队列已满。"
        assert run["status"] == "failed" and run["prompt"] == run["output"] == value
        assert run["results"][0]["output"].encode() == value.encode()
    assert stored.get("run", "example")["error"] == value
    assert (
        client.get("/api/v1/runs/example", headers={"Accept-Language": "en"}).json()[
            "error"
        ]
        == value
    )


def test_only_generated_conversation_titles_are_placeholders(
    client, center, monkeypatch
):
    import asyncio

    async def complete(identity, _image=None):
        center.change_run(identity, status="completed")

    monkeypatch.setattr(center, "execute", complete)

    def submit(conversation_id, prompt):
        async def run():
            result = center.submit(
                conversation_id, prompt, mode="chat", use_memory=False
            )
            await center.workers[result["id"]]

        asyncio.run(run())

    def create():
        response = client.post("/api/v1/conversations", json={}).json()
        assert (
            response["title_is_placeholder"] is True and response["title"] == "New chat"
        )
        return response["id"], "/api/v1/conversations/" + response["id"]

    identity, path = create()
    assert (
        client.patch(path, json={"favorite": True}).json()["title_is_placeholder"]
        is True
    )
    # A first user message can itself be exactly equal to the canonical placeholder.
    submit(identity, "New chat")
    first = client.get(path).json()
    assert first["title"] == "New chat" and first["title_is_placeholder"] is False
    submit(identity, "A different prompt")
    assert client.get(path).json()["title"] == "New chat"

    for custom in ("New chat", "我的对话"):
        identity, path = create()
        renamed = client.patch(path, json={"title": custom}).json()
        assert renamed["title"] == custom and renamed["title_is_placeholder"] is False
        submit(identity, "Keep my title")
        assert client.get(path).json()["title"] == custom

    identity, path = create()
    legacy = center.store.get("conversation", identity)
    del legacy["title_is_placeholder"]
    center.store.put("conversation", legacy)
    submit(identity, "Keep this old title as written")
    assert client.get(path).json()["title"] == "New chat"

    identity, path = create()
    submit(identity, "First prompt")
    titled = client.get(path).json()
    assert titled["title"] == "First prompt" and titled["title_is_placeholder"] is False
