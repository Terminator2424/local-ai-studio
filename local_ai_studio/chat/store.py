"""Historial del chat en SQLite (local, en conversations/chat.db, fuera de git).

Cada conversación guarda el modelo y el system prompt con los que se creó; cada mensaje, su rol, su texto y,
si los hay, las imágenes adjuntas (ruta del archivo). Todo se escribe con consultas parametrizadas.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ..core.paths import BASE_DIR
from .roles import DEFAULT_ROLE, ROLES

log = logging.getLogger(__name__)

CONV_DIR = BASE_DIR / "conversations"
DB_PATH = CONV_DIR / "chat.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    model TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    images TEXT NOT NULL DEFAULT '[]',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, id);
"""


@dataclass
class Conversation:
    id: int
    title: str
    model: str
    system_prompt: str
    created: float
    updated: float


@dataclass
class Message:
    id: int
    role: str
    content: str
    images: list[str]
    created: float


class ChatStore:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_SCHEMA)
        self._last = 0.0

    def _now(self) -> float:
        """Hora actual estrictamente creciente: el reloj de Windows tiene ~15 ms de resolución."""
        self._last = max(time.time(), self._last + 1e-6)
        return self._last

    def close(self):
        self.db.close()

    # ---------------------------------------------------------- conversaciones
    def create(self, model: str, system_prompt: str = "", title: str = "Nueva conversación") -> int:
        now = self._now()
        cur = self.db.execute(
            "INSERT INTO conversations (title, model, system_prompt, created, updated) VALUES (?, ?, ?, ?, ?)",
            (title, model, system_prompt, now, now))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def conversations(self) -> list[Conversation]:
        rows = self.db.execute("SELECT * FROM conversations ORDER BY updated DESC, id DESC").fetchall()
        return [Conversation(**dict(r)) for r in rows]

    def get(self, conv_id: int) -> Conversation | None:
        r = self.db.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        return Conversation(**dict(r)) if r else None

    def update(self, conv_id: int, *, title: str | None = None, model: str | None = None,
               system_prompt: str | None = None):
        sql = {"title": "UPDATE conversations SET title = ?, updated = ? WHERE id = ?",
               "model": "UPDATE conversations SET model = ?, updated = ? WHERE id = ?",
               "system_prompt": "UPDATE conversations SET system_prompt = ?, updated = ? WHERE id = ?"}
        for col, val in (("title", title), ("model", model), ("system_prompt", system_prompt)):
            if val is not None:
                self.db.execute(sql[col], (val, self._now(), conv_id))
        self.db.commit()

    def delete(self, conv_id: int):
        self.db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        self.db.commit()

    # ---------------------------------------------------------------- mensajes
    def add(self, conv_id: int, role: str, content: str, images: list[str] | None = None) -> int:
        now = self._now()
        cur = self.db.execute(
            "INSERT INTO messages (conv_id, role, content, images, created) VALUES (?, ?, ?, ?, ?)",
            (conv_id, role, content, json.dumps(images or []), now))
        self.db.execute("UPDATE conversations SET updated = ? WHERE id = ?", (now, conv_id))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def messages(self, conv_id: int) -> list[Message]:
        rows = self.db.execute("SELECT * FROM messages WHERE conv_id = ? ORDER BY id", (conv_id,)).fetchall()
        return [Message(r["id"], r["role"], r["content"], json.loads(r["images"]), r["created"]) for r in rows]

    def drop_last(self, conv_id: int, role: str):
        """Quita el último mensaje del rol dado (para regenerar una respuesta)."""
        r = self.db.execute("SELECT id FROM messages WHERE conv_id = ? AND role = ? ORDER BY id DESC LIMIT 1",
                            (conv_id, role)).fetchone()
        if r:
            self.db.execute("DELETE FROM messages WHERE id = ?", (r["id"],))
            self.db.commit()

    # --------------------------------------------------------------- exportar
    def export_markdown(self, conv_id: int) -> str:
        conv = self.get(conv_id)
        if conv is None:
            raise KeyError(conv_id)
        out = [f"# {conv.title}", "", f"_Modelo: {conv.model} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(conv.created))}_", ""]
        if conv.system_prompt.strip():
            out += ["> **System prompt:** " + conv.system_prompt.strip().replace("\n", "\n> "), ""]
        for m in self.messages(conv_id):
            who = "Tú" if m.role == "user" else "Asistente" if m.role == "assistant" else "Sistema"
            out += [f"## {who}", ""]
            if m.images:
                out += [f"_Imágenes: {', '.join(Path(p).name for p in m.images)}_", ""]
            out += [m.content.strip(), ""]
        return "\n".join(out).rstrip() + "\n"


def title_from(text: str, limit: int = 48) -> str:
    """Título corto para una conversación a partir de su primer mensaje."""
    t = " ".join(text.split())
    return (t[:limit - 1] + "…") if len(t) > limit else (t or "Nueva conversación")


# --------------------------------------------------------------- preferencias
PREFS_PATH = CONV_DIR / "prefs.json"
DEFAULT_MODEL = "llama3.1:8b"
PREFS_VERSION = 2       # v2: el modelo predeterminado pasó a llama3.1:8b (las preferencias antiguas se actualizan una vez)
DEFAULT_PREFS = {"prefs_version": PREFS_VERSION, "model": DEFAULT_MODEL, "docs_model": DEFAULT_MODEL, "study_model": DEFAULT_MODEL,
                 "temperature": 0.7, "top_p": 0.9,
                 "max_tokens": 1024, "num_ctx": 8192, "read_roots": [],
                 "system_prompt": ROLES[DEFAULT_ROLE][1]}


def load_prefs(path: Path = PREFS_PATH) -> dict:
    """Preferencias del chat. Los valores ausentes, con tipo erróneo o fuera de rango vuelven al valor por defecto."""
    prefs = dict(DEFAULT_PREFS)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return prefs
    if not isinstance(data, dict):
        return prefs
    limits = {"temperature": (0.0, 2.0), "top_p": (0.0, 1.0), "max_tokens": (16, 32768), "num_ctx": (512, 131072),
              "prefs_version": (0, 1000)}
    for k, default in DEFAULT_PREFS.items():
        v = data.get(k)
        if isinstance(default, list):
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                prefs[k] = v[:20]
        elif isinstance(default, str):
            if isinstance(v, str) and v.strip():
                prefs[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            lo, hi = limits[k]
            if lo <= v <= hi:
                prefs[k] = float(v) if isinstance(default, float) else int(v)
    if data.get("prefs_version") != PREFS_VERSION:          # guardadas por una versión anterior
        prefs["model"] = DEFAULT_MODEL
    prefs["prefs_version"] = PREFS_VERSION
    return prefs


def update_prefs(changes: dict, path: Path = PREFS_PATH) -> dict:
    """Cambia solo `changes` sobre lo que haya en disco. Cada página (chat, documentos, estudiar) guarda lo suyo sin pisar
    lo de las demás, que es lo que ocurriría si cada una escribiera su copia completa."""
    prefs = load_prefs(path)
    prefs.update({k: v for k, v in changes.items() if k in DEFAULT_PREFS})
    save_prefs(prefs, path)
    return prefs


def save_prefs(prefs: dict, path: Path = PREFS_PATH) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({k: prefs[k] for k in DEFAULT_PREFS if k in prefs}, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        log.warning("No se pudieron guardar las preferencias del chat: %s", exc)
