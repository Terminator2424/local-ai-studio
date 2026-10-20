"""Índice local de documentos (RAG): fragmentos por página con búsqueda híbrida."""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from ..core.errors import Cancelled
from ..core.ollama_client import get_client
from ..core.paths import BASE_DIR
from ..core.textutil import sha256_file

log = logging.getLogger(__name__)
DB_PATH = BASE_DIR / "conversations" / "rag.db"
EMBED_MODEL = "nomic-embed-text"
CHUNK = 900
OVERLAP = 120
MAX_PAGES = 1500

# --- SEGURIDAD: Prevención de Path Traversal ---
def _is_safe_path(path: str | Path, base_dir: str | Path) -> bool:
    try:
        path_obj = Path(path).resolve()
        base_dir_obj = Path(base_dir).resolve()
        return base_dir_obj in path_obj.parents or path_obj == base_dir_obj
    except Exception:
        return False

def _validate_and_get_safe_path(path: str | Path, base_dir: str | Path | None = None) -> Path:
    if base_dir is None:
        base_dir = Path.home()
    if not _is_safe_path(path, base_dir):
        raise ValueError(f"Acceso denegado: la ruta '{path}' no está dentro del directorio permitido '{base_dir}'")
    return Path(path).resolve()
# -----------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, name TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE, pages INTEGER NOT NULL, empty_pages INTEGER NOT NULL DEFAULT 0, indexed REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    page INTEGER NOT NULL, idx INTEGER NOT NULL, text TEXT NOT NULL, vec BLOB);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, page);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
"""

@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    doc: str
    page: int
    text: str
    score: float

@dataclass
class IndexResult:
    doc_id: int
    name: str
    pages: int
    chunks: int
    empty_pages: int
    embedded: bool
    already: bool = False

def fts_query(q: str) -> str:
    words = re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ.,]+", q, flags=re.UNICODE)
    toks = [w.strip(".,") for w in words]
    toks = [t[:40] for t in dict.fromkeys(toks) if len(t) >= 2 or t.isdigit()][:40]
    return " OR ".join('"' + t.replace('"', '') + '"' for t in toks)

def split_text(text: str, size: int = CHUNK, overlap: int = OVERLAP) -> list[str]:
    from ..pdf.chunking import RecursiveSplitter
    docs = RecursiveSplitter(size, overlap, ["\n\n", "\n", ". ", "; ", ", ", " ", ""]).create_documents([text])
    return [d.page_content for d in docs if len(d.page_content.strip()) >= 20]


class DocIndex:
    def __init__(self, cfg: dict | None = None, path: str | Path = DB_PATH,
                 embed_fn: Callable[[list[str]], list[list[float]]] | None = None):
        self.cfg = cfg
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_SCHEMA)
        self._embed_fn = embed_fn
        self._lock = threading.RLock()
        self._matrix = None
        self._ids: list[int] = []
        self._doc_of = None

    def close(self):
        self.db.close()

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        try:
            if self._embed_fn is not None:
                return self._embed_fn(texts)
            if self.cfg is None:
                return None
            return [list(v) for v in get_client(self.cfg).embed(model=EMBED_MODEL, input=texts).embeddings]
        except Exception as exc:
            log.info("Sin embeddings (%s): solo búsqueda léxica", str(exc)[:80])
            return None

    def add_pdf(self, path: str | Path, progress: Callable[[float, str], None] | None = None,
                cancel: threading.Event | None = None) -> IndexResult:
        import numpy as np
        import pymupdf

        from ..pdf.extractor import _clean
        from ..pdf.layout import layout_text

        # VALIDACIÓN DE SEGURIDAD
        path = _validate_and_get_safe_path(path)

        digest = sha256_file(path)
        with self._lock:
            row = self.db.execute("SELECT * FROM docs WHERE sha256 = ?", (digest,)).fetchone()
            if row:
                n = self.db.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (row["id"],)).fetchone()[0]
                return IndexResult(row["id"], row["name"], row["pages"], n, row["empty_pages"], True, already=True)
        doc = pymupdf.open(str(path))
        try:
            if doc.needs_pass or doc.page_count == 0:
                raise ValueError("El PDF está cifrado o vacío.")
            if doc.page_count > MAX_PAGES:
                raise ValueError(f"El PDF tiene {doc.page_count} páginas (máximo {MAX_PAGES}).")
            pieces: list[tuple[int, int, str]] = []
            empty = 0
            for i in range(doc.page_count):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if progress:
                    progress(0.5 * i / doc.page_count, f"Leyendo página {i + 1} de {doc.page_count}")
                try:
                    text = _clean(layout_text(doc[i]))
                except Exception:
                    text = ""
                parts = split_text(text) if len(text.strip()) >= 20 else []
                if not parts:
                    empty += 1
                pieces += [(i + 1, k, t) for k, t in enumerate(parts)]
            pages = doc.page_count
        finally:
            doc.close()
        vecs: list[bytes | None] = [None] * len(pieces)
        embedded = False
        for s in range(0, len(pieces), 16):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if progress:
                progress(0.5 + 0.5 * s / max(1, len(pieces)), f"Embeddings {s}/{len(pieces)}")
            out = self.embed([p[2] for p in pieces[s:s + 16]])
            if out is None:
                break
            for k, emb in enumerate(out):
                a = np.asarray(emb, dtype=np.float32)
                n = np.linalg.norm(a)
                vecs[s + k] = (a / n if n else a).tobytes()
            embedded = True
        with self._lock, self.db:
            cur = self.db.execute("INSERT INTO docs (path, name, sha256, pages, empty_pages, indexed) VALUES (?, ?, ?, ?, ?, ?)",
                                  (str(path), path.name, digest, pages, empty, time.time()))
            doc_id = int(cur.lastrowid or 0)
            for (pg, k, t), blob in zip(pieces, vecs, strict=True):
                c = self.db.execute("INSERT INTO chunks (doc_id, page, idx, text, vec) VALUES (?, ?, ?, ?, ?)", (doc_id, pg, k, t, blob))
                self.db.execute("INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", (c.lastrowid, t))
            self._matrix = None
        return IndexResult(doc_id, path.name, pages, len(pieces), empty, embedded)

    def docs(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute("SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS chunks FROM docs d "
                                   "ORDER BY indexed DESC").fetchall()

    def remove(self, doc_id: int):
        with self._lock, self.db:
            ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,))]
            self.db.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in ids])
            self.db.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
            self._matrix = None

    def page_text(self, doc_id: int, first: int, last: int, limit: int = 12000) -> str:
        with self._lock:
            rows = self.db.execute("SELECT page, text FROM chunks WHERE doc_id = ? AND page BETWEEN ? AND ? ORDER BY page, idx",
                                   (doc_id, first, last)).fetchall()
        out, cur = [], None
        for r in rows:
            if r["page"] != cur:
                out.append(f"\n[p. {r['page']}]")
                cur = r["page"]
            out.append(r["text"])
        return "\n".join(out)[:limit]

    def _load_matrix(self):
        import numpy as np
        with self._lock:
            if self._matrix is None:
                rows = self.db.execute("SELECT id, doc_id, vec FROM chunks WHERE vec IS NOT NULL").fetchall()
                vecs = [(r["id"], r["doc_id"], np.frombuffer(r["vec"], dtype=np.float32)) for r in rows]
                if vecs:
                    dim = Counter(len(v) for _, _, v in vecs).most_common(1)[0][0]
                    vecs = [t for t in vecs if len(t[2]) == dim]
                self._ids = [t[0] for t in vecs]
                self._doc_of = np.array([t[1] for t in vecs], dtype=np.int64)
                self._matrix = np.vstack([t[2] for t in vecs]) if vecs else np.zeros((0, 1), np.float32)
            return self._matrix, self._ids, self._doc_of

    def _lexical(self, query: str, doc_ids: list[int] | None) -> list[int]:
        q = fts_query(query)
        if not q:
            return []
        sql = "SELECT chunks_fts.rowid FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?"
        args: list = [q]
        if doc_ids is not None:
            sql += " AND c.doc_id IN (" + ",".join("?" * len(doc_ids)) + ")"
            args += list(doc_ids)
        sql += " ORDER BY bm25(chunks_fts) LIMIT 30"
        try:
            with self._lock:
                return [r[0] for r in self.db.execute(sql, args)]
        except sqlite3.OperationalError:
            return []

    def search(self, query: str, k: int = 6, doc_ids: list[int] | None = None) -> list[Hit]:
        import numpy as np
        if doc_ids is not None and not doc_ids:
            return []
        ranks: dict[int, float] = {}
        for pos, cid in enumerate(self._lexical(query, doc_ids)):
            ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (60 + pos)
        qv = self.embed([query])
        if qv is not None:
            m, ids, doc_of = self._load_matrix()
            if len(ids) and m.shape[1] == len(qv[0]):
                v = np.asarray(qv[0], dtype=np.float32)
                n = np.linalg.norm(v)
                scores = m @ (v / n if n else v)
                if doc_ids is not None:
                    scores = np.where(np.isin(doc_of, doc_ids), scores, -np.inf)
                top = [int(j) for j in np.argsort(-scores)[:30] if np.isfinite(scores[j])]
                for pos, j in enumerate(top):
                    ranks[ids[j]] = ranks.get(ids[j], 0.0) + 1.0 / (60 + pos)
        hits: list[Hit] = []
        with self._lock:
            for cid, sc in sorted(ranks.items(), key=lambda kv: -kv[1]):
                r = self.db.execute("SELECT c.id, c.doc_id, c.page, c.text, d.name FROM chunks c JOIN docs d ON d.id = c.doc_id "
                                    "WHERE c.id = ?", (cid,)).fetchone()
                if r and (doc_ids is None or r["doc_id"] in doc_ids):
                    hits.append(Hit(r["id"], r["doc_id"], r["name"], r["page"], r["text"], sc))
                if len(hits) >= k:
                    break
        return hits
