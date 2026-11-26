"""Local, user-managed workspaces, model profiles and explicit memories."""

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import retrieval
from .config import STATE, WORKSPACE, load_settings
from .credentials import decrypt_text, encrypt_text


def timestamp():
    return datetime.now(UTC).isoformat()


def local_endpoint(value):
    parsed = urlsplit(value.strip().rstrip("/"))
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or not parsed.port
        or parsed.port in {load_settings()["port"], 12394, 18789, 18790, 8643, 4097, 8188}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1"
    ):
        raise ValueError(
            "Use a local OpenAI-compatible http://127.0.0.1:PORT/v1 endpoint"
        )
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{parsed.port}/v1"


def folder_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("An absolute folder path is required")
    path = path.resolve(strict=True)
    if not path.is_dir() or path == Path(path.anchor):
        raise ValueError("Choose an existing project folder, not an entire drive")
    private = {
        ".runtime",
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".codex",
        ".venv",
        ".git",
        ".openclaw",
        ".hermes",
        ".opencode",
        ".config",
        ".local",
    }
    if any(part.lower() in private for part in path.parts):
        raise ValueError("Private configuration folders cannot be workspaces")
    if os.name == "nt":
        windows = Path(os.environ.get("SystemRoot", "C:/Windows")).resolve()
        if path == windows or windows in path.parents:
            raise ValueError("System directories cannot be workspaces")
    return str(path)


class Library:
    def __init__(self, directory=None, default_workspace=None):
        self.directory = Path(directory) if directory is not None else STATE
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "library.sqlite"
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS entries(kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS preferences(id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            if not db.execute("SELECT 1 FROM preferences WHERE id='app'").fetchone():
                root = str(default_workspace or WORKSPACE)
                self.put(
                    "workspace",
                    {
                        "id": "default",
                        "name": "Jui Workspace",
                        "path": root,
                        "created": timestamp(),
                    },
                    db,
                )
                db.execute(
                    "INSERT INTO preferences VALUES ('app',?)",
                    (json.dumps({"workspace_id": "default", "memory_enabled": True}),),
                )

        with self.connection() as db:
            for (raw,) in db.execute(
                "SELECT data FROM entries WHERE kind='model'"
            ).fetchall():
                value = json.loads(raw)
                if value.get("api_key"):
                    self.put("model", value, db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, kind, value, db=None):
        if db is None:
            with self.connection() as connection:
                return self.put(kind, value, connection)
        stored = dict(value)
        if kind == "model" and stored.get("api_key"):
            stored["encrypted_api_key"] = encrypt_text(stored["api_key"])
            stored["api_key"] = ""
        elif kind == "model":
            stored.pop("encrypted_api_key", None)
        db.execute(
            "INSERT OR REPLACE INTO entries VALUES (?,?,?)",
            (kind, value["id"], json.dumps(stored, ensure_ascii=False)),
        )
        return value

    def get(self, kind, identity):
        with self.connection() as db:
            row = db.execute(
                "SELECT data FROM entries WHERE kind=? AND id=?", (kind, identity)
            ).fetchone()
        if row is None:
            raise KeyError(identity)
        return self.decode(kind, json.loads(row[0]))

    @staticmethod
    def decode(kind, value):
        if kind == "model" and value.get("encrypted_api_key"):
            value["api_key"] = decrypt_text(value.pop("encrypted_api_key"))
        return value

    def list(self, kind):
        with self.connection() as db:
            return [
                self.decode(kind, json.loads(row[0]))
                for row in db.execute(
                    "SELECT data FROM entries WHERE kind=? ORDER BY rowid", (kind,)
                )
            ]

    def preferences(self, **updates):
        with self.connection() as db:
            value = json.loads(
                db.execute("SELECT data FROM preferences WHERE id='app'").fetchone()[0]
            )
            if updates:
                value.update(updates)
                db.execute(
                    "UPDATE preferences SET data=? WHERE id='app'", (json.dumps(value),)
                )
        return value

    def active_workspace(self):
        return self.get("workspace", self.preferences()["workspace_id"])

    def add_workspace(self, name, path):
        path = folder_path(path)
        for existing in self.list("workspace"):
            if os.path.normcase(existing["path"]) == os.path.normcase(path):
                return existing
        return self.put(
            "workspace",
            {
                "id": uuid.uuid4().hex,
                "name": name.strip() or Path(path).name,
                "path": path,
                "created": timestamp(),
            },
        )

    def select_workspace(self, identity):
        value = self.get("workspace", identity)
        folder_path(value["path"])
        self.preferences(workspace_id=identity)
        return value

    def delete(self, kind, identity):
        self.get(kind, identity)
        if kind == "workspace":
            if identity == "default" or identity == self.preferences()["workspace_id"]:
                raise ValueError("The active and default workspaces cannot be removed")
            if any(m.get("workspace_id") == identity for m in self.list("memory")):
                raise ValueError("Remove this workspace's memories first")
        with self.connection() as db:
            db.execute("DELETE FROM entries WHERE kind=? AND id=?", (kind, identity))

    @staticmethod
    def public_model(value):
        return {
            **{k: v for k, v in value.items() if k != "api_key"},
            "has_key": bool(value.get("api_key")),
        }

    def save_model(self, value, identity=None):
        if len(self.list("model")) >= 16 and identity is None:
            raise ValueError("At most 16 auxiliary profiles are supported")
        old = self.get("model", identity) if identity else {}
        value = dict(value)
        value["name"] = value["name"].strip()
        value["model_id"] = value["model_id"].strip()
        if not value["name"]:
            raise ValueError("Model name cannot be blank")
        value["base_url"] = local_endpoint(value["base_url"])
        if value["enabled"] and not value["model_id"].strip():
            raise ValueError("Set a model ID before enabling this profile")
        key = value.pop("api_key", None)
        value["api_key"] = old.get("api_key", "") if key is None else key
        if any(c in value["api_key"] for c in "\r\n"):
            raise ValueError("Invalid credential")
        value.update(
            id=identity or uuid.uuid4().hex,
            created=old.get("created", timestamp()),
            updated=timestamp(),
        )
        self.put("model", value)
        return self.public_model(value)

    async def probe_model(self, identity):
        value = self.get("model", identity)
        endpoint = local_endpoint(value["base_url"])
        headers = (
            {"Authorization": "Bearer " + value["api_key"]}
            if value.get("api_key")
            else {}
        )
        try:
            async with httpx.AsyncClient(
                timeout=5, trust_env=False, follow_redirects=False
            ) as client:
                response = await client.get(endpoint + "/models", headers=headers)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError("Invalid model listing")
                models = [
                    str(m["id"])[:200]
                    for m in payload.get("data", [])
                    if isinstance(m, dict) and "id" in m
                ][:50]
                return {
                    "connected": True,
                    "available": bool(
                        value["model_id"] and value["model_id"] in models
                    ),
                    "models": models,
                }
        except (httpx.HTTPError, ValueError, TypeError):
            return {
                "connected": False,
                "available": False,
                "models": [],
                "error": "Local model endpoint is unavailable or incompatible",
            }

    def save_memory(self, value, identity=None):
        old = self.get("memory", identity) if identity else {}
        if identity is None and len(self.list("memory")) >= 200:
            raise ValueError("At most 200 explicit memories are supported")
        value = dict(value)
        if not value["content"].strip() or not value["title"].strip():
            raise ValueError("Memory title and content cannot be blank")
        if value.get("workspace_id"):
            self.get("workspace", value["workspace_id"])
        value.update(
            id=identity or uuid.uuid4().hex,
            created=old.get("created", timestamp()),
            updated=timestamp(),
        )
        return self.put("memory", value)

    def recall(self, prompt, workspace_id):
        if not self.preferences()["memory_enabled"]:
            return []
        terms = set(re.findall(r"[a-z0-9_]{2,}", prompt.lower()))
        for segment in re.findall(r"[\u4e00-\u9fff]+", prompt):
            terms.update(segment[i : i + 2] for i in range(len(segment) - 1))
        visible = [
            memory
            for memory in self.list("memory")
            if memory["enabled"] and memory.get("workspace_id") in {None, workspace_id}
        ]
        texts = [memory["title"] + "\n" + memory["content"] for memory in visible]
        semantic = retrieval.scores(prompt, texts)
        candidates = []
        for i, (memory, text) in enumerate(zip(visible, texts, strict=True)):
            text = text.lower()
            lexical = sum(term in text for term in terms)
            similarity = semantic[i] if semantic is not None else 0
            if (
                memory["pinned"]
                or lexical
                or similarity >= retrieval.MEMORY_MIN_SIMILARITY
            ):
                candidates.append(
                    (
                        int(memory["pinned"]),
                        similarity,
                        lexical,
                        memory["updated"],
                        memory,
                    )
                )
        results, length = [], 0
        for *_, memory in sorted(candidates, key=lambda item: item[:-1], reverse=True):
            size = len(memory["title"]) + len(memory["content"]) + 40
            if length + size <= 6000 and len(results) < 8:
                results.append(memory)
                length += size
        return results
