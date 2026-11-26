import asyncio
import json
import time

import test_control as shared

from jui import plugins

center = shared.center
client = shared.client
store = shared.store


def test_desktop_token_is_task_bound(monkeypatch, tmp_path):
    monkeypatch.setattr(plugins, "STATE", tmp_path)
    file = tmp_path / "desktop-grant.json"
    file.write_text(
        json.dumps(
            {"task_id": "one", "token": "one-token", "expires": time.time() + 60}
        )
    )
    assert plugins.desktop_allowed("one-token")
    assert not plugins.desktop_allowed("")
    assert not plugins.desktop_allowed("two-token")
    file.write_text(
        json.dumps(
            {"task_id": "two", "token": "two-token", "expires": time.time() + 60}
        )
    )
    assert not plugins.desktop_allowed("one-token")
    assert plugins.desktop_allowed("two-token")
    file.write_text(json.dumps({"token": "two-token", "expires": 0}))
    assert not plugins.desktop_allowed("two-token")


def test_rejected_token_cannot_start_desktop_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(plugins, "STATE", tmp_path)
    monkeypatch.setattr(plugins, "enabled", lambda _: None)
    result = asyncio.run(plugins.desktop("open_google", token="expired"))
    assert result["status"] == "blocked"


def test_workspace_directories_and_boundary(client, center):
    from pathlib import Path

    workspace = Path(center.store.library.active_workspace()["path"])
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "example.txt").write_text("example")
    response = client.get("/api/workspace?path=nested")
    assert response.status_code == 200
    assert response.json()["items"][0]["path"] == "nested/example.txt"
    assert client.get("/api/workspace?path=..").status_code == 400
