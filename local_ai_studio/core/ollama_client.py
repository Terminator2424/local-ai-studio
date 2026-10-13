"""Cliente de Ollama: siempre en loopback, una sola conexión compartida, reintentos y cancelación.

SEGURIDAD — Ollama no tiene autenticación: si se arranca con OLLAMA_HOST=0.0.0.0 cualquier equipo de la red (o de
internet, si el router lo permite) podría usar tu GPU, leer los modelos y consultar tus documentos. Por eso la
aplicación fija OLLAMA_HOST=127.0.0.1 en su propio proceso y se niega a hablar con un host que no sea loopback.
"""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import time
from urllib.parse import urlparse, urlunparse

from .errors import Cancelled

log = logging.getLogger(__name__)

os.environ["OLLAMA_HOST"] = "127.0.0.1:11434"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
DEFAULT_HOST = "http://localhost:11434"
_PERMANENT_ERRORS = {400, 401, 403, 404}        # modelo inexistente, petición inválida…: reintentar no sirve


def enforce_local_host(host: str) -> str:
    """Valida que el host de Ollama sea loopback; si no, aborta (nunca se envían documentos a la red)."""
    parsed = urlparse(host if "://" in host else f"http://{host}")
    if (parsed.hostname or "") not in LOOPBACK_HOSTS:
        raise RuntimeError(
            f"Host de Ollama no permitido: «{host}». Por seguridad solo se admite localhost/127.0.0.1 "
            "(Ollama no tiene autenticación; exponerlo en la red es un riesgo).")
    return host


def fast_loopback(host: str) -> str:
    """«localhost» → «127.0.0.1». En Windows «localhost» prueba antes IPv6 (::1), donde Ollama no escucha, y cada
    conexión pierde ~2 s esperando; con la IP directa la consulta tarda ~0,2 s. Sigue siendo loopback."""
    parsed = urlparse(host if "://" in host else f"http://{host}")
    if parsed.hostname != "localhost":
        return host if "://" in host else f"http://{host}"
    netloc = "127.0.0.1" + (f":{parsed.port}" if parsed.port else "")
    return urlunparse(parsed._replace(netloc=netloc))


_CLIENTS: dict = {}
_CLIENTS_LOCK = threading.Lock()


def get_client(cfg: dict):
    """Cliente de Ollama (uno por host, reutilizado: crear uno cuesta ~0,3 s y el cliente httpx admite varios hilos)."""
    import ollama
    host = fast_loopback(enforce_local_host(cfg["ollama"].get("host", DEFAULT_HOST)))
    with _CLIENTS_LOCK:
        if host not in _CLIENTS:
            _CLIENTS[host] = ollama.Client(host=host)
        return _CLIENTS[host]


def close_clients() -> None:
    """Cierra las conexiones abiertas (al salir de la aplicación)."""
    with _CLIENTS_LOCK:
        for client in _CLIENTS.values():
            http = getattr(client, "_client", None)
            if http is not None:
                with contextlib.suppress(Exception):
                    http.close()
        _CLIENTS.clear()


def check_ollama(cfg: dict, need_vision: bool = True) -> None:
    """Verifica que Ollama responde y que los modelos existen. Intenta arrancar el servicio."""
    client = get_client(cfg)
    try:
        listing = client.list()
    except Exception:  # noqa: BLE001  (cualquier fallo de conexión significa «no responde»)
        log.info("Ollama no responde; intentando iniciar 'ollama serve'...")
        try:
            # Siempre en loopback (ver la nota de seguridad de arriba); sin shell=True
            subprocess.Popen(["ollama", "serve"], env={**os.environ, "OLLAMA_HOST": "127.0.0.1:11434"},
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise RuntimeError("No se pudo conectar con Ollama. Abre la aplicación Ollama e inténtalo de nuevo.") from exc
        for _ in range(20):
            time.sleep(1)
            try:
                listing = client.list()
                break
            except Exception:  # noqa: BLE001, S112  (aún arrancando: se vuelve a intentar)
                continue
        else:
            raise RuntimeError("Ollama no responde en 127.0.0.1:11434.") from None
    names = {m.model for m in listing.models}
    needed = [cfg["ollama"]["text_model"]] + ([cfg["ollama"]["vision_model"]] if need_vision else [])
    for model in needed:
        if model not in names and f"{model}:latest" not in names:
            raise RuntimeError(f"El modelo '{model}' no está descargado. Ejecuta: ollama pull {model}")


def chat_stream(cfg: dict, model: str, messages: list[dict], options: dict,
                cancel: threading.Event | None = None, on_text=None) -> str:
    """Llama a Ollama en streaming, con reintentos y soporte de cancelación.

    on_text(texto_acumulado) se llama periódicamente para mostrar el avance en vivo. Los errores permanentes (modelo
    inexistente, petición inválida) no se reintentan; los de red o sobrecarga sí, con una pausa entre intentos.
    """
    client = get_client(cfg)
    retries = int(cfg["ollama"].get("retries", 2))
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        parts: list[str] = []
        try:
            stream = client.chat(model=model, messages=messages, stream=True, options=options,
                                 keep_alive=cfg["ollama"].get("keep_alive", "10m"))
            last_emit = 0.0
            for part in stream:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                parts.append(part.message.content or "")
                now = time.time()
                if on_text and now - last_emit > 0.4:
                    on_text("".join(parts))
                    last_emit = now
            text = "".join(parts).strip()
            if on_text:
                on_text(text)
            if not text:
                raise RuntimeError("El modelo devolvió una respuesta vacía.")
            return text
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001  (red, servidor, modelo…: se decide abajo si se reintenta)
            last_exc = exc
            log.warning("Fallo en llamada a %s (intento %d/%d): %s", model, attempt + 1, retries + 1, exc)
            if getattr(exc, "status_code", None) in _PERMANENT_ERRORS:
                break
            if attempt < retries:
                time.sleep(2)
    raise RuntimeError(f"Falló la llamada a Ollama ({model}): {last_exc}")
