"""Barrera de red de ejecución: el proceso no puede conectarse a nada fuera de este equipo.

Defensa en profundidad para «nada sale del equipo». Se instala un gancho de auditoría de Python (PEP 578) que aborta
cualquier `socket.connect` o resolución DNS a un host que no sea de bucle local (127.0.0.0/8, ::1, localhost). Así, aunque
una librería, un documento malicioso o un fallo intenten abrir una conexión, el intento falla con PermissionError.

La única excepción son las descargas de modelos que el usuario confirma: se hacen dentro de `with allow_network(...)`.
Limitaciones (ver SECURITY.md): un gancho de auditoría no cubre código nativo que abra sockets por su cuenta (fuera de
Python) ni otros procesos (Ollama es un proceso aparte, que sí tiene Internet para descargar modelos cuando se lo pides).
"""
from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import sys
import threading

log = logging.getLogger(__name__)

_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", ""}
_lock = threading.Lock()
_allowed = 0               # contador de contextos allow_network abiertos
_installed = False
_blocked: list[str] = []   # intentos bloqueados (para el diagnóstico y las pruebas)


def is_local_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    h = str(host).strip().strip("[]").lower()
    if h in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(h.split("%")[0]).is_loopback
    except ValueError:
        return False


def _address_host(addr):
    """Host de una dirección de socket: tupla (host, puerto, …), cadena (AF_UNIX) o bytes."""
    if isinstance(addr, tuple) and addr:
        return addr[0]
    return None if isinstance(addr, (str, bytes)) else addr


def check_event(event: str, args: tuple) -> None:
    """Lanza PermissionError si el evento de auditoría es una conexión de red no local y no está permitida."""
    if event == "socket.connect":
        host = _address_host(args[1] if len(args) > 1 else None)
    elif event == "socket.getaddrinfo" or event in ("socket.gethostbyname", "socket.gethostbyaddr"):
        host = args[0] if args else None
    else:
        return
    if _allowed or is_local_host(host):
        return
    msg = f"Conexión a «{host}» bloqueada: esta aplicación solo puede hablar con localhost"
    with _lock:
        _blocked.append(msg)
        del _blocked[:-50]
    log.warning(msg)
    raise PermissionError(msg)


def _hook(event, args):
    if event.startswith("socket."):
        check_event(event, args)


def install() -> None:
    """Instala la barrera (una sola vez por proceso; Python no permite quitar un gancho de auditoría)."""
    global _installed
    if _installed:
        return
    # Variables que impiden que las librerías de IA intenten la red por su cuenta
    for k, v in (("HF_HUB_OFFLINE", "1"), ("TRANSFORMERS_OFFLINE", "1"), ("HF_HUB_DISABLE_TELEMETRY", "1"),
                 ("DO_NOT_TRACK", "1"), ("DISABLE_TELEMETRY", "1"), ("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")):
        os.environ.setdefault(k, v)
    sys.addaudithook(_hook)
    _installed = True


def blocked_attempts() -> list[str]:
    return list(_blocked)


@contextlib.contextmanager
def allow_network(reason: str = ""):
    """Permite la red mientras dura el bloque. Solo para descargas que el usuario ha confirmado."""
    global _allowed
    log.info("Red permitida temporalmente: %s", reason)
    with _lock:
        _allowed += 1
    hf_constants = None
    prev = True
    try:
        try:
            from huggingface_hub import constants
            prev = getattr(constants, "HF_HUB_OFFLINE", True)
            setattr(constants, "HF_HUB_OFFLINE", False)  # noqa: B010  (atributo de un módulo opcional, sin tipos)
            hf_constants = constants
        except Exception:  # noqa: BLE001  (huggingface_hub es opcional: sin él no hay nada que reabrir)
            hf_constants = None
        yield
    finally:
        if hf_constants is not None:
            setattr(hf_constants, "HF_HUB_OFFLINE", prev)  # noqa: B010  (atributo de un módulo opcional, sin tipos)
        with _lock:
            _allowed -= 1
        log.info("Red bloqueada de nuevo")
