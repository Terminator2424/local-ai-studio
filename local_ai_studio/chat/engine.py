"""Motor del chat: conversación con Ollama en streaming, con parámetros, imágenes y estadísticas de velocidad.

Solo habla con Ollama en 127.0.0.1 (get_client aplica enforce_local_host): ningún prompt ni imagen sale del equipo.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import Cancelled
from ..core.ollama_client import get_client
from .roles import DEFAULT_ROLE, ROLES
from .store import DEFAULT_MODEL, Message  # noqa: F401  (DEFAULT_MODEL se reexporta para la interfaz)

log = logging.getLogger(__name__)

# Modelos ofrecidos en el selector: id de Ollama -> (nombre, para qué sirve en una línea, detalle).
# Los tamaños son los de la cuantización Q4_K_M que descarga Ollama; el detalle supone una GPU de 8 GB.
MODEL_INFO = {
    "llama3.1:8b": ("Llama 3.1 8B", "general (por defecto)",
                    "Meta · 4,9 GB · razona bien, sigue instrucciones y admite herramientas. Buen equilibrio entre calidad y velocidad."),
    "qwen2.5:7b": ("Qwen 2.5 7B", "español y datos",
                   "Alibaba · 4,7 GB · el más sólido en español y en tablas/datos; admite herramientas."),
    "gemma2:9b": ("Gemma 2 9B", "redacción cuidada, más lento",
                  "Google · 5,4 GB · escribe muy bien, pero en 8 GB de VRAM se reparte con la CPU. No admite herramientas."),
    "mistral:7b": ("Mistral 7B", "ligero y rápido",
                   "Mistral AI · 4,4 GB · el más rápido de la lista; razona menos que los demás; admite herramientas."),
    "qwen2.5-coder:7b": ("Qwen 2.5 Coder 7B", "programación",
                         "Alibaba · 4,7 GB · especializado en escribir, explicar y depurar código; admite herramientas."),
    "qwen2.5vl:7b": ("Qwen 2.5 VL 7B", "visión: lee imágenes",
                     "Alibaba · 6,0 GB · el único que entiende imágenes (fotos, capturas, PDF escaneados). No admite herramientas."),
}
CHAT_MODELS = {k: v[1] for k, v in MODEL_INFO.items()}      # id -> nota corta (compatibilidad)
# Los que admiten llamadas a herramientas en Ollama (gemma2 y los de visión no)
TOOL_MODELS = {"qwen2.5:7b", "llama3.1:8b", "qwen2.5-coder:7b", "mistral:7b"}


def model_label(model: str) -> str:
    """Texto del selector: «Llama 3.1 8B — uso general (predeterminado)»."""
    info = MODEL_INFO.get(model)
    return f"{info[0]} — {info[1]}" if info else model


def model_detail(model: str) -> str:
    info = MODEL_INFO.get(model)
    return info[2] if info else ""
EXTRA_PULLS = {"nomic-embed-text"}
ALLOWED_PULLS = set(CHAT_MODELS) | EXTRA_PULLS     # lista cerrada: ningún texto externo puede añadir modelos
DEFAULT_SYSTEM = ROLES[DEFAULT_ROLE][1]


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
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if vision and m.images:
            d["images"] = [_read(p) for p in m.images if Path(p).is_file()]
        msgs.append(d)
    last: dict[str, Any] = {"role": "user", "content": user_text}
    if vision and images:
        last["images"] = [_read(p) for p in images if Path(p).is_file()]
    msgs.append(last)
    return msgs


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


class ChatEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._installed: tuple[float, set[str]] | None = None

    def installed(self, max_age: float = 10.0) -> set[str]:
        """Modelos instalados en Ollama. Se recuerda `max_age` segundos para no consultar al servidor en cada clic."""
        now = time.monotonic()
        if self._installed is not None and now - self._installed[0] < max_age:
            return set(self._installed[1])
        try:
            found = {m.model for m in get_client(self.cfg).list().models}
        except Exception:  # noqa: BLE001
            return set()                    # sin respuesta: no se recuerda, se reintentará en la siguiente consulta
        self._installed = (now, found)
        return set(found)

    def forget_installed(self) -> None:
        self._installed = None

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
        self.forget_installed()
        log.info("Modelo %s descargado", model)
