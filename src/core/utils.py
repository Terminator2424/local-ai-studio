"""Funciones auxiliares: rutas, configuración, logging, Ollama, idioma y exportación."""
from __future__ import annotations

import hashlib
import importlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

# ----------------------------------------------------------------- rutas
# Empaquetado con PyInstaller: los datos del usuario van junto al .exe, no dentro de la carpeta temporal interna
FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent.parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))      # recursos incluidos en el paquete (plantillas)
CONFIG_PATH = BASE_DIR / "config.yaml"
SUMMARIES_DIR = BASE_DIR / "resumenes"
IMAGES_DIR = BASE_DIR / "imagenes"
CACHE_DIR = SUMMARIES_DIR / ".cache"
LOG_PATH = SUMMARIES_DIR / "app.log"

for _d in (SUMMARIES_DIR, IMAGES_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("resumidor")


class Cancelled(Exception):
    """El usuario canceló el proceso."""


class PDFError(Exception):
    """Error al abrir/leer un PDF (corrupto, cifrado, sin texto...)."""


# ------------------------------------------------------- dependencias
def ensure_package(import_name: str, pip_name: str | None = None, required: bool = True) -> bool:
    """Importa un paquete; si falta lo instala con pip automáticamente."""
    try:
        importlib.import_module(import_name)
        return True
    except ImportError:
        pass
    if FROZEN:      # en el .exe sys.executable es la propia aplicación: nunca se ejecuta pip
        if required:
            raise ImportError(f"Falta {import_name} en la versión empaquetada")
        return False
    try:
        print(f"Instalando {pip_name or import_name} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name or import_name])
        importlib.invalidate_caches()
        importlib.import_module(import_name)
        return True
    except Exception as exc:  # noqa: BLE001
        if required:
            raise
        log.warning("No se pudo instalar %s: %s", pip_name or import_name, exc)
        return False


# ------------------------------------------------------ configuración
CONFIG_EXAMPLE = BUNDLE_DIR / "config.example.yaml"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# ---------------------------------------------------------------------------
# SEGURIDAD — Ollama debe escuchar SOLO en localhost.
# Ollama no tiene autenticación: si se arranca con OLLAMA_HOST=0.0.0.0 cualquier equipo de la red (o de internet,
# si el router lo permite) podría usar tu GPU, leer los modelos y consultar tus documentos. Por eso la app fija
# OLLAMA_HOST=127.0.0.1 en su propio proceso y se niega a hablar con un host que no sea loopback.
# ---------------------------------------------------------------------------
os.environ["OLLAMA_HOST"] = "127.0.0.1:11434"


def enforce_local_host(host: str) -> str:
    """Valida que el host de Ollama sea loopback; si no, aborta (nunca se envían documentos a la red)."""
    from urllib.parse import urlparse
    parsed = urlparse(host if "://" in host else f"http://{host}")
    if (parsed.hostname or "") not in LOOPBACK_HOSTS:
        raise RuntimeError(
            f"Host de Ollama no permitido: «{host}». Por seguridad solo se admite localhost/127.0.0.1 "
            "(Ollama no tiene autenticación; exponerlo en la red es un riesgo).")
    return host


def fast_loopback(host: str) -> str:
    """«localhost» → «127.0.0.1». En Windows «localhost» prueba antes IPv6 (::1), donde Ollama no escucha, y cada
    conexión pierde ~2 s esperando; con la IP directa la consulta tarda ~0,2 s. Sigue siendo loopback."""
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(host if "://" in host else f"http://{host}")
    if parsed.hostname != "localhost":
        return host if "://" in host else f"http://{host}"
    netloc = "127.0.0.1" + (f":{parsed.port}" if parsed.port else "")
    return urlunparse(parsed._replace(netloc=netloc))


def load_config(auto_hardware: bool = True) -> dict:
    """Carga config.yaml (si no existe, se crea desde config.example.yaml) y aplica el ajuste automático al hardware."""
    if not CONFIG_PATH.exists() and CONFIG_EXAMPLE.exists():
        CONFIG_PATH.write_text(CONFIG_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    enforce_local_host(cfg["ollama"].get("host", "http://localhost:11434"))
    if auto_hardware:
        try:
            from .hardware_detector import apply_auto_config, installed_models
            cfg = apply_auto_config(cfg, installed_models(cfg))
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo detectar el hardware (se usa config.yaml tal cual): %s", exc)
    return cfg


def setup_logging(extra_handler: logging.Handler | None = None) -> None:
    """Log a resumenes/app.log (rotativo) y, opcionalmente, a un handler de la GUI."""
    root = logging.getLogger("resumidor")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if extra_handler:
        extra_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        root.addHandler(extra_handler)


# ---------------------------------------------------------- generales
def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m:02d}m {s:02d}s"


def fill(template: str, **values) -> str:
    """Sustituye {clave} sin tocar otras llaves del texto (a diferencia de str.format)."""
    for key, val in values.items():
        template = template.replace("{" + key + "}", str(val))
    return template


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]+', "_", name).strip("_")[:80] or "documento"


# --------------------------------------------------------------- idioma
_STOPWORDS = {
    "es": "de la que el en y los del se las por un para con una su al lo como más pero sus ya este sí porque esta entre cuando muy sin sobre también hasta hay donde desde todo nos durante son fue ser".split(),
    "en": "the of and to in is that for it as was with be by on not this are or his from at which but have an had they you were their all we can her has there been if more when will would who so than into also these".split(),
    "fr": "le la les des du de et un une que qui dans pour pas sur avec est sont au aux ce cette ces plus par mais ou ne il elle nous vous leur être avoir comme".split(),
    "pt": "o os as um uma de da do das dos que em para com não por mais como mas foi são ao aos se sua seu também entre sobre quando muito já está".split(),
    "de": "der die das und ist von zu den mit sich des auf für nicht ein eine dem im als auch es an werden aus er hat dass sie nach bei um am sind noch wie einem".split(),
    "it": "il lo la i gli le di che e un una per con non sono del della dei delle più come ma anche è si da nel nella al alla questo questa".split(),
}
LANG_NAMES_ES = {"es": "español", "en": "inglés", "fr": "francés", "pt": "portugués", "de": "alemán", "it": "italiano"}
LANG_NAMES_EN = {"es": "Spanish", "en": "English", "fr": "French", "pt": "Portuguese", "de": "German", "it": "Italian"}


def detect_language(text: str) -> str:
    """Detección ligera por frecuencia de palabras vacías (sin dependencias extra)."""
    words = re.findall(r"[a-záéíóúñüàâçèêëîïôùûœãõß]+", text[:30000].lower())
    if not words:
        return "es"
    best, best_score = "es", -1.0
    for code, stops in _STOPWORDS.items():
        sset = set(stops)
        score = sum(1 for w in words if w in sset) / len(words)
        if score > best_score:
            best, best_score = code, score
    return best


# --------------------------------------------------------------- Ollama
_CLIENTS: dict = {}
_CLIENTS_LOCK = threading.Lock()


def get_client(cfg: dict):
    """Cliente de Ollama (uno por host, reutilizado: crear uno cuesta ~0,3 s y el cliente httpx admite varios hilos)."""
    import ollama
    host = fast_loopback(enforce_local_host(cfg["ollama"].get("host", "http://localhost:11434")))
    with _CLIENTS_LOCK:
        if host not in _CLIENTS:
            _CLIENTS[host] = ollama.Client(host=host)
        return _CLIENTS[host]


def check_ollama(cfg: dict, need_vision: bool = True) -> None:
    """Verifica que Ollama responde y que los modelos existen. Intenta arrancar el servicio."""
    client = get_client(cfg)
    try:
        listing = client.list()
    except Exception:  # noqa: BLE001
        log.info("Ollama no responde; intentando iniciar 'ollama serve'...")
        try:
            # Siempre en loopback (ver el comentario de seguridad de OLLAMA_HOST más arriba); sin shell=True
            subprocess.Popen(["ollama", "serve"], env={**os.environ, "OLLAMA_HOST": "127.0.0.1:11434"},
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("No se pudo conectar con Ollama. Abre la aplicación Ollama e inténtalo de nuevo.") from exc
        for _ in range(20):
            time.sleep(1)
            try:
                listing = client.list()
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            raise RuntimeError("Ollama no responde en 127.0.0.1:11434.")
    names = {m.model for m in listing.models}
    needed = [cfg["ollama"]["text_model"]] + ([cfg["ollama"]["vision_model"]] if need_vision else [])
    for model in needed:
        if model not in names and f"{model}:latest" not in names:
            raise RuntimeError(f"El modelo '{model}' no está descargado. Ejecuta: ollama pull {model}")


def chat_stream(cfg: dict, model: str, messages: list[dict], options: dict,
                cancel: threading.Event | None = None, on_text=None) -> str:
    """Llama a Ollama en streaming, con reintentos y soporte de cancelación.

    on_text(texto_acumulado) se llama periódicamente para mostrar el avance en vivo.
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
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("Fallo en llamada a %s (intento %d/%d): %s", model, attempt + 1, retries + 1, exc)
            time.sleep(2)
    raise RuntimeError(f"Falló la llamada a Ollama ({model}): {last_exc}")


# ---------------------------------------------------------- exportación
_PDF_CSS = """
body { font-family: sans-serif; font-size: 10.5pt; line-height: 1.4; color: #1b1b1b; }
h1 { font-size: 21pt; color: #1e2a78; margin-bottom: 6pt; }
h2 { font-size: 15pt; color: #1e2a78; margin-top: 16pt; margin-bottom: 4pt; border-bottom: 1px solid #b9c0ee; }
h3 { font-size: 12pt; color: #3b4bc8; margin-top: 11pt; margin-bottom: 3pt; }
h4 { font-size: 11pt; color: #3b4bc8; margin-top: 8pt; margin-bottom: 2pt; }
p { margin-top: 3pt; margin-bottom: 3pt; }
li { margin-bottom: 2pt; }
blockquote { color: #555555; margin-left: 8pt; font-size: 9pt; }
table { border-collapse: collapse; margin-top: 4pt; margin-bottom: 4pt; }
th { background-color: #e8ebff; border: 1px solid #8f99d6; padding: 3pt; }
td { border: 1px solid #aab1de; padding: 3pt; }
code, pre { font-family: monospace; font-size: 9.5pt; color: #222a66; }
"""


def export_pdf(markdown_text: str, path: str | Path) -> None:
    """Convierte Markdown a PDF (A4) con PyMuPDF Story. Las fórmulas LaTeX se dibujan como imágenes (matplotlib);
    si alguna no es compatible, se escribe en Unicode legible."""
    import tempfile

    import pymupdf
    from .mdview import md_to_html

    with tempfile.TemporaryDirectory() as tmp:
        html, _ = md_to_html(markdown_text, "light", "Inter", outdir=Path(tmp), safe=True)
        story = pymupdf.Story(html=html, user_css=_PDF_CSS + ".eq { margin-left: 16pt; margin-top: 3pt; margin-bottom: 3pt; }",
                              archive=pymupdf.Archive(tmp))
        writer = pymupdf.DocumentWriter(str(path))
        mediabox = pymupdf.paper_rect("a4")
        where = mediabox + (50, 50, -50, -50)
        more = True
        while more:
            dev = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
        writer.close()
