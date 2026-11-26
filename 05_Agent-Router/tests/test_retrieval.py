import asyncio
from types import SimpleNamespace

import httpx

from jui import control_models, retrieval
from jui.library import Library


def test_memory_semantics_filter_scope_before_encoding(monkeypatch, tmp_path):
    store = Library(tmp_path, tmp_path)
    project = tmp_path / "other"
    project.mkdir()
    workspace = store.add_workspace("Other", str(project))

    def add(body, **changes):
        return store.save_memory(
            {
                "title": body,
                "content": body,
                "pinned": False,
                "enabled": True,
                "workspace_id": None,
                **changes,
            }
        )

    add("Private", workspace_id=workspace["id"])
    add("Disabled", enabled=False)
    add("Weak")
    semantic = add("Voice")
    pin = add("Pinned", pinned=True)

    def score(query, documents):
        assert query == "Narrator"
        assert [doc.split("\n")[0] for doc in documents] == ["Weak", "Voice", "Pinned"]
        return [0.82, 0.91, 0.51]

    monkeypatch.setattr(retrieval, "scores", score)
    assert [value["id"] for value in store.recall("Narrator", "default")] == [
        pin["id"],
        semantic["id"],
    ]
    monkeypatch.setattr(retrieval, "scores", lambda *_: None)
    assert [value["id"] for value in store.recall("Narrator", "default")] == [pin["id"]]


def test_search_ranks_local_candidates_without_changing_links(monkeypatch):
    response = {
        "results": [
            {
                "title": "Less relevant",
                "url": "https://example.org/one",
                "content": "First",
            },
            {"title": "Target", "url": "https://example.org/two", "content": "Second"},
            {"title": "Reject", "url": "file:///private", "content": "Invalid link"},
        ]
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    client = httpx.AsyncClient
    monkeypatch.setattr(
        control_models.httpx,
        "AsyncClient",
        lambda **kw: client(transport=transport, **kw),
    )
    store = SimpleNamespace(
        get=lambda *_: {
            "enabled": True,
            "protocol": "searxng",
            "base_url": "http://127.0.0.1:8888",
        },
        key=lambda _: "",
    )

    def score(query, candidates):
        assert query == "Target"
        assert candidates == ["Less relevant\nFirst", "Target\nSecond"]
        return [0.5, 0.95]

    monkeypatch.setattr(retrieval, "scores", score)
    config = {"enabled": True, "provider_id": "test", "count": 1}
    rows = asyncio.run(control_models.search(store, "Target", config))
    assert rows == [
        {"title": "Target", "url": "https://example.org/two", "summary": "Second"}
    ]
    monkeypatch.setattr(retrieval, "scores", lambda *_: None)
    assert (
        asyncio.run(control_models.search(store, "Target", config))[0]["title"]
        == "Less relevant"
    )
