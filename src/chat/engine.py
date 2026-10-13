"""Motor del chat: conversación con Ollama en streaming, con parámetros, imágenes y estadísticas de velocidad.

Solo habla con Ollama en 127.0.0.1 (get_client aplica enforce_local_host): ningún prompt ni imagen sale del equipo.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core.utils import Cancelled, get_client
from .store import Message

log = logging.getLogger("resumidor")

# Modelos ofrecidos en el selector. La nota dice para qué sirve cada uno con 8 GB de VRAM (cuantización Q4_K_M).
CHAT_MODELS = {
    "qwen2.5:7b": "General y muy bueno en español",
    "llama3.1:8b": "Razonamiento general",
    "gemma2:9b": "Buena calidad; justo de VRAM (se reparte con la CPU)",
    "mistral:7b": "Ligero y rápido",
    "qwen2.5-coder:7b": "Código",
    "qwen2.5vl:7b": "Visión: analiza imágenes",
}
# Los que admiten llamadas a herramientas en Ollama (gemma2 y los de visión no)
TOOL_MODELS = {"qwen2.5:7b", "llama3.1:8b", "qwen2.5-coder:7b", "mistral:7b"}
EXTRA_PULLS = {"nomic-embed-text"}
ALLOWED_PULLS = set(CHAT_MODELS) | EXTRA_PULLS     # lista cerrada: ningún texto externo puede añadir modelos
DEFAULT_SYSTEM = "Eres un asistente útil y conciso. Responde en el idioma del usuario. Si no estás seguro de un dato, dilo."


def is_vision(model: str) -> bool:
    m = model.lower()
    return "vl" in m.split(":")[0] or "llava" in m or "vision" in m


@dataclass
class Params:
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 1024
    num_ctx: int = 8192

    def options(self) -> dict:
        return {"temperature": float(self.temperature), "top_p": float(self.top_p),
                "num_predict": int(self.max_tokens), "num_ctx": int(self.num_ctx)}


@dataclass
class Reply:
    text: str
    tokens: int = 0
    seconds: float = 0.0
    prompt_tokens: int = 0
    stats: dict = field(default_factory=dict)

    @property
    def tokens_per_s(self) -> float:
        return self.tokens / self.seconds if self.seconds > 0 else 0.0


def est_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.6))


def trim_history(messages: list[dict], num_ctx: int, reserve: int) -> list[dict]:
    """Quita los mensajes más antiguos (nunca el system ni el último) hasta que quepan en el contexto."""
    budget = max(512, int(num_ctx * 0.8) - reserve)
    system = [m for m in messages[:1] if m["role"] == "system"]
    rest = messages[len(system):]
    while len(rest) > 1 and sum(est_tokens(m["content"]) for m in system + rest) > budget:
        rest.pop(0)
    return system + rest


def build_messages(system: str, history: list[Message], user_text: str, images: list[str] | None,
                   vision: bool) -> list[dict]:
    msgs: list[dict] = []
    if system.strip():
        msgs.append({"role": "system", "content": system.strip()})
    for m in history:
        d = {"role": m.role, "content": m.content}
        if vision and m.images:
            d["images"] = [_read(p) for p in m.images if Path(p).is_file()]
        msgs.append(d)
    last = {"role": "user", "content": user_text}
    if vision and images:
        last["images"] = [_read(p) for p in images if Path(p).is_file()]
    msgs.append(last)
    return msgs


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


class ChatEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def installed(self) -> set[str]:
        try:
            return {m.model for m in get_client(self.cfg).list().models}
        except Exception:  # noqa: BLE001
            return set()

    def stream(self, model: str, messages: list[dict], params: Params, cancel: threading.Event | None = None,
               on_text=None, keep_alive: str | int = "5m") -> Reply:
        """Genera la respuesta en streaming. on_text(texto_acumulado) se llama mientras llega."""
        client = get_client(self.cfg)
        messages = trim_history(messages, params.num_ctx, params.max_tokens)
        parts: list[str] = []
        final = None
        t0 = time.time()
        last_emit = 0.0
        for part in client.chat(model=model, messages=messages, stream=True, options=params.options(),
                                keep_alive=keep_alive):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            parts.append(part.message.content or "")
            if getattr(part, "done", False):
                final = part
            now = time.time()
            if on_text and now - last_emit > 0.08:
                on_text("".join(parts))
                last_emit = now
        text = "".join(parts)
        if on_text:
            on_text(text)
        elapsed = time.time() - t0
        tokens = int(getattr(final, "eval_count", 0) or 0)
        eval_ns = int(getattr(final, "eval_duration", 0) or 0)
        secs = eval_ns / 1e9 if eval_ns else elapsed
        reply = Reply(text, tokens or est_tokens(text), secs, int(getattr(final, "prompt_eval_count", 0) or 0),
                      {"total_s": elapsed})
        log.info("Chat %s: %d tokens en %.1f s (%.1f tok/s)", model, reply.tokens, secs, reply.tokens_per_s)
        return reply

    def pull(self, model: str, on_progress=None, cancel: threading.Event | None = None) -> None:
        """Descarga un modelo de Ollama (solo se llama tras la confirmación del usuario).
        on_progress(fracción 0-1 o None, texto) informa del avance."""
        if model not in ALLOWED_PULLS:
            raise PermissionError(f"Modelo no permitido: «{model}». Solo se descargan los de la lista de la aplicación.")
        for part in get_client(self.cfg).pull(model, stream=True):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            total, done = getattr(part, "total", None), getattr(part, "completed", None)
            frac = (done / total) if total and done is not None else None
            if on_progress:
                on_progress(frac, part.status or "")
        log.info("Modelo %s descargado", model)
