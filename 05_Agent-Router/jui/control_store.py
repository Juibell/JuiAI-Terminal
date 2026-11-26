"""Versioned control-center state. Existing library/task databases remain authoritative."""

import json
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import STATE, load_secrets, load_settings
from .control_local_models import (
    QWEN_ALIAS,
    QWEN_BASE,
    QWEN_CONTEXT,
    QWEN_ID,
    QWEN_NAME,
)
from .credentials import private_directory
from .library import Library, timestamp

ROLES = ("main", "decision", "code", "reasoning", "vision", "summary", "asr", "tts")
AGENTS = ("openclaw", "opencode", "hermes")


class Vault:
    def __init__(self, directory):
        self.directory = private_directory(directory)

    def path(self, identity):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity):
            raise ValueError("Invalid secret reference")
        return self.directory / (identity + ".bin")

    def put(self, identity, value):
        import win32crypt

        encrypted = win32crypt.CryptProtectData(
            value.encode(), "JuiAI", None, None, None, 1
        )
        target = self.path(identity)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, target)
        if self.get(identity) != value:
            raise RuntimeError("Credential verification failed")

    def get(self, identity):
        import win32crypt

        return win32crypt.CryptUnprotectData(
            self.path(identity).read_bytes(), None, None, None, 1
        )[1].decode()


class ControlStore:
    def __init__(self, directory=None, library=None, vault=None):
        self.directory = Path(directory or STATE)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "control.sqlite"
        self.library = library or Library()
        self.vault = vault or Vault(self.directory / "vault")
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS objects(kind TEXT,id TEXT,data TEXT,PRIMARY KEY(kind,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,kind TEXT,data TEXT)"
            )
            db.execute("PRAGMA user_version=1")
        self.seed()
        if QWEN_ID in load_settings().get("local_presets", []):
            self.seed_qwen()
        self.import_legacy()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db
        finally:
            db.close()

    def last_event(self):
        with self.connect() as db:
            return db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]

    def get(self, kind, identity):
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM objects WHERE kind=? AND id=?", (kind, identity)
            ).fetchone()
        if not row:
            raise KeyError(identity)
        return json.loads(row[0])

    def list(self, kind):
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT data FROM objects WHERE kind=? ORDER BY rowid", (kind,)
                )
            ]

    def put(self, kind, value):
        value = {**value, "updated": timestamp()}
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO objects VALUES(?,?,?)",
                (kind, value["id"], json.dumps(value, ensure_ascii=False)),
            )
        return value

    def remove(self, kind, identity):
        with self.connect() as db:
            db.execute("DELETE FROM objects WHERE kind=? AND id=?", (kind, identity))

    def event(self, kind, **data):
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO events(at,kind,data) VALUES(?,?,?)",
                (timestamp(), kind, json.dumps(data, ensure_ascii=False)),
            )
            return cursor.lastrowid

    def events(self, after=0):
        with self.connect() as db:
            rows = db.execute(
                "SELECT seq,at,kind,data FROM events WHERE seq>? ORDER BY seq LIMIT 200",
                (after,),
            ).fetchall()
        return [
            {"seq": seq, "at": at, "kind": kind, "data": json.loads(data)}
            for seq, at, kind, data in rows
        ]

    def seed(self):
        first_start = not self.list("roles")
        settings = load_settings()
        presets = set(settings.get("local_presets", []))
        providers = [
            {
                "id": "bonsai",
                "name": "Bonsai / llama.cpp",
                "protocol": "openai",
                "base_url": settings["model"]["base_url"],
                "local": True,
                "enabled": True,
                "secret_ref": "builtin-model",
            },
            {
                "id": "firered",
                "name": "FireRedASR2-LLM",
                "protocol": "native-asr",
                "base_url": "",
                "local": True,
                "enabled": True,
            },
            {
                "id": "fish",
                "name": "Fish Audio S2-Pro",
                "protocol": "native-tts",
                "base_url": settings["voice"]["tts"]["base_url"],
                "local": True,
                "enabled": True,
            },
        ]
        models = [
            {
                "id": "bonsai",
                "provider_id": "bonsai",
                "name": "Bonsai 2 27B PQ2_0",
                "model_id": settings["model"]["id"],
                "context": settings["model"]["context"],
                "max_tokens": 3072,
                "temperature": 0.8,
                "timeout": 240,
                "stream": True,
                "enabled": True,
                "capabilities": ["text", "tools"],
            },
            {
                "id": "firered",
                "provider_id": "firered",
                "name": "FireRedASR2-LLM",
                "model_id": "firered-asr2-llm",
                "enabled": True,
                "capabilities": ["asr"],
            },
            {
                "id": "fish",
                "provider_id": "fish",
                "name": "Fish Audio S2-Pro",
                "model_id": "fish-s2-pro",
                "voice": "default",
                "speed": 1.0,
                "enabled": True,
                "capabilities": ["tts"],
            },
        ]
        providers = [p for p in providers if p["id"] in presets]
        models = [m for m in models if m["id"] in presets]
        # Existing library profiles are imported once, on the first desktop startup.
        for item in self.library.list("model") if first_start else []:
            identity = "import-" + item["id"]
            ref = None
            if item.get("api_key"):
                ref = "provider-" + identity
                self.vault.put(ref, item["api_key"])
            providers.append(
                {
                    "id": identity,
                    "name": item["name"],
                    "protocol": "openai",
                    "base_url": item["base_url"],
                    "local": True,
                    "enabled": item["enabled"],
                    "secret_ref": ref,
                }
            )
            models.append(
                {
                    "id": identity,
                    "provider_id": identity,
                    "name": item["name"],
                    "model_id": item["model_id"],
                    "enabled": item["enabled"],
                    "context": 8192,
                    "max_tokens": 2048,
                    "temperature": 0.7,
                    "timeout": 180,
                    "stream": False,
                    "capabilities": ["text"],
                }
            )
        # Seeding and its marker are atomic; a failed migration never hides old data.
        with self.connect() as db:
            for kind, items in (
                ("provider", providers),
                ("model", models),
                (
                    "roles",
                    [
                        {
                            "id": "global",
                            "bindings": {
                                role: model_id
                                for role, model_id in (
                                    ("main", "bonsai"),
                                    ("asr", "firered"),
                                    ("tts", "fish"),
                                )
                                if model_id in presets
                            },
                            "agents": {},
                        }
                    ],
                ),
            ):
                for item in items:
                    db.execute(
                        "INSERT OR IGNORE INTO objects VALUES(?,?,?)",
                        (kind, item["id"], json.dumps(item, ensure_ascii=False)),
                    )

    def seed_qwen(self):
        # Apply the same first-class profile to new and existing databases.
        provider = {
            "id": QWEN_ID,
            "name": "Qwen3.8 / llama.cpp",
            "protocol": "openai",
            "base_url": QWEN_BASE,
            "local": True,
            "enabled": True,
            "secret_ref": "builtin-model",
        }
        model = {
            "id": QWEN_ID,
            "provider_id": QWEN_ID,
            "name": QWEN_NAME,
            "model_id": QWEN_ALIAS,
            "context": QWEN_CONTEXT,
            "max_tokens": 1024,
            "temperature": 0.7,
            "timeout": 240,
            "stream": True,
            "enabled": True,
            "capabilities": ["text", "tools"],
        }
        with self.connect() as db:
            for kind, item in (("provider", provider), ("model", model)):
                db.execute(
                    "INSERT OR IGNORE INTO objects VALUES(?,?,?)",
                    (kind, QWEN_ID, json.dumps(item, ensure_ascii=False)),
                )
            row = db.execute(
                "SELECT data FROM objects WHERE kind='model' AND id=?", (QWEN_ID,)
            ).fetchone()
            existing = json.loads(row[0])
            stock_before_tools = {**model, "capabilities": ["text"]}
            if {
                k: v for k, v in existing.items() if k != "updated"
            } == stock_before_tools:
                existing["capabilities"] = model["capabilities"]
                db.execute(
                    "UPDATE objects SET data=? WHERE kind='model' AND id=?",
                    (json.dumps(existing, ensure_ascii=False), QWEN_ID),
                )

    def import_legacy(self):
        legacy = self.directory / "tasks.sqlite"
        if not legacy.exists():
            return
        try:
            with sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True) as old:
                tasks = [
                    json.loads(row[0]) for row in old.execute("SELECT data FROM tasks")
                ]
        except sqlite3.OperationalError:
            return
        by_id = {item["id"]: item for item in tasks}
        with self.connect() as db:
            for task in sorted(tasks, key=lambda item: item.get("created", "")):
                root, seen = task, set()
                while root.get("parent_id") in by_id and root["id"] not in seen:
                    seen.add(root["id"])
                    root = by_id[root["parent_id"]]
                cid = "legacy-" + root["id"]
                if db.execute(
                    "SELECT 1 FROM objects WHERE kind='legacy_deleted' AND id=?", (cid,)
                ).fetchone():
                    continue
                conversation = {
                    "id": cid,
                    "title": root.get("title") or root["prompt"][:60],
                    "workspace_id": root.get("workspace_id", "default"),
                    "favorite": root.get("favorite", False),
                    "created": root.get("created"),
                    "updated": task.get("updated", task.get("created")),
                    "legacy": True,
                }
                db.execute(
                    "INSERT OR IGNORE INTO objects VALUES('conversation',?,?)",
                    (cid, json.dumps(conversation, ensure_ascii=False)),
                )
                for role, text in (
                    ("user", task["prompt"]),
                    ("assistant", task.get("output", "")),
                ):
                    message = {
                        "id": task["id"] + "-" + role,
                        "conversation_id": cid,
                        "role": role,
                        "content": text,
                        "created": task.get("created"),
                        "run_id": task["id"],
                        "sensitive": task.get("mode") != "chat"
                        or bool(task.get("memory_ids")),
                    }
                    db.execute(
                        "INSERT OR IGNORE INTO objects VALUES('message',?,?)",
                        (message["id"], json.dumps(message, ensure_ascii=False)),
                    )

    def public_provider(self, value):
        return {
            **{k: v for k, v in value.items() if k != "secret_ref"},
            "has_key": bool(value.get("secret_ref")),
        }

    def key(self, provider):
        ref = provider.get("secret_ref")
        return (
            load_secrets()["model_key"]
            if ref == "builtin-model"
            else self.vault.get(ref)
            if ref
            else ""
        )

    def resolve(self, role, roles=None, override=None):
        roles = roles or self.get("roles", "global")
        identity = override or roles["bindings"].get(role)
        if not identity and role in {"decision", "code", "reasoning", "summary"}:
            identity = roles["bindings"].get("main")
        if not identity:
            raise ValueError("Model role is not configured: " + role)
        model = self.get("model", identity)
        provider = self.get("provider", model["provider_id"])
        needed = role if role in {"asr", "tts", "vision"} else "text"
        if (
            not model.get("enabled")
            or not provider.get("enabled")
            or needed not in model.get("capabilities", [])
        ):
            raise ValueError(
                "The selected model is disabled or lacks the required capability: "
                + role
            )
        return {**model, "provider": provider}

    def policy(self, workspace_id):
        try:
            return self.get("policy", workspace_id)
        except KeyError:
            return {
                "id": workspace_id,
                "allow_cloud_context": False,
                "allow_cloud_audio": False,
            }

    def conversation(self, workspace_id=None):
        workspace = (
            self.library.get("workspace", workspace_id)
            if workspace_id
            else self.library.active_workspace()
        )
        return self.put(
            "conversation",
            {
                "id": uuid.uuid4().hex,
                "workspace_id": workspace["id"],
                "title": "New chat",
                "favorite": False,
                "created": timestamp(),
            },
        )

    def messages(self, conversation_id):
        return [
            m for m in self.list("message") if m["conversation_id"] == conversation_id
        ]

    def desktop_token(self):
        try:
            return self.vault.get("desktop-auth")
        except FileNotFoundError:
            token = secrets.token_urlsafe(48)
            self.vault.put("desktop-auth", token)
            return token
