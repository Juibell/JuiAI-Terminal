import json

import pytest

from jui import plugins
from jui.library import Library, folder_path, local_endpoint


def memory(title="Preference", content="Use Python", workspace_id=None, **kwargs):
    return dict(
        title=title,
        content=content,
        workspace_id=workspace_id,
        category="preference",
        enabled=True,
        pinned=False,
        **kwargs,
    )


def model(**kwargs):
    value = {
        "name": "Helper",
        "provider": "llama.cpp",
        "base_url": "http://127.0.0.1:8086/v1",
        "model_id": "helper",
        "purpose": "code",
        "enabled": True,
        "api_key": "local-secret",
    }
    value.update(kwargs)
    return value


def test_library_initialization_and_persistence(tmp_path):
    store = Library(tmp_path, tmp_path)
    assert store.list("model") == []
    store.preferences(memory_enabled=False)
    again = Library(tmp_path, tmp_path)
    assert again.list("model") == []
    assert not again.preferences()["memory_enabled"]


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1",
        "http://192.168.1.1:1234/v1",
        "http://127.0.0.1:12394/v1",
        "http://127.0.0.1:18790/v1",
        "http://user:key@localhost:8086/v1",
        "http://localhost:8086/v1?q=x",
        "http://localhost:8086/",
        "http://localhost/v1",
    ],
)
def test_model_endpoints_stay_local(url):
    with pytest.raises(ValueError):
        local_endpoint(url)


def test_model_key_is_private_and_preserved(tmp_path):
    store = Library(tmp_path, tmp_path)
    value = store.save_model(model())
    assert value["has_key"] and "api_key" not in value
    store.save_model(model(api_key=None), value["id"])
    assert store.get("model", value["id"])["api_key"] == "local-secret"
    store.save_model(model(api_key=""), value["id"])
    assert not store.get("model", value["id"])["api_key"]
    with pytest.raises(ValueError):
        store.save_model(model(model_id=""))


def test_workspace_selection_does_not_delete_folder(tmp_path):
    store = Library(tmp_path / "state", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    value = store.add_workspace("Project", str(project))
    assert store.add_workspace("Other", str(project))["id"] == value["id"]
    store.select_workspace(value["id"])
    with pytest.raises(ValueError):
        store.delete("workspace", value["id"])
    store.select_workspace("default")
    store.delete("workspace", value["id"])
    assert project.is_dir()
    with pytest.raises(ValueError):
        folder_path("relative")
    private = tmp_path / ".runtime"
    private.mkdir()
    with pytest.raises(ValueError):
        folder_path(str(private))


def test_memory_recall_scope_disable_delete_and_budget(tmp_path):
    store = Library(tmp_path / "state", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    w = store.add_workspace("P", str(project))
    global_memory = store.save_memory(memory())
    scoped = store.save_memory(memory("Local", "Use Python here", w["id"]))
    assert {m["id"] for m in store.recall("Python", "default")} == {global_memory["id"]}
    assert {m["id"] for m in store.recall("Python", w["id"])} == {
        global_memory["id"],
        scoped["id"],
    }
    assert not store.recall("unrelated", "default")
    store.save_memory({**global_memory, "pinned": True}, global_memory["id"])
    assert store.recall("unrelated", "default")
    store.save_memory({**global_memory, "enabled": False}, global_memory["id"])
    assert not store.recall("Python", "default")
    store.preferences(memory_enabled=False)
    assert not store.recall("Python", w["id"])
    store.delete("memory", global_memory["id"])
    assert len(store.list("memory")) == 1


def test_chinese_recall_and_workspace_grant(monkeypatch, tmp_path):
    store = Library(tmp_path, tmp_path)
    store.save_memory(memory("代码偏好", "代码使用清晰的类型注解"))
    assert store.recall("帮我审阅代码", "default")
    monkeypatch.setattr(plugins, "STATE", tmp_path)
    monkeypatch.setenv("JUI_AGENT", "hermes")
    with pytest.raises(ValueError):
        plugins.workspace_root()
    (tmp_path / "task-context.json").write_text(
        json.dumps({"workspace": str(tmp_path), "expires": 9999999999})
    )
    assert plugins.workspace_root() == tmp_path
    (tmp_path / "secrets.json").write_text("private")
    with pytest.raises(ValueError):
        plugins.workspace_path("secrets.json")
