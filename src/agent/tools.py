"""Herramientas que el asistente puede pedir. Cada una declara su riesgo y quién la valida.

- "leer"  : solo lee dentro de las carpetas permitidas. Se ejecuta sola.
- "puro"  : no toca nada del sistema (calcular, fecha). Se ejecuta sola.
- "escribir" / "ejecutar" / "abrir": cambian algo. NUNCA se ejecutan sin que el usuario vea la acción exacta y la apruebe.

No existe ninguna herramienta de terminal libre, de red, de borrado ni de instalación. Añadir una exige decidir su riesgo aquí.
"""
from __future__ import annotations

import fnmatch
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .policy import MAX_READ, MAX_WRITE, Policy, ToolDenied
from .sandbox import run_python

RISK_AUTO = {"leer", "puro"}
MAX_REVIEW = 20_000      # lo que cambia algo se muestra ENTERO al usuario: si no cabe para revisarlo, no se ejecuta
_NEVER_OPEN = {".exe", ".bat", ".cmd", ".com", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".msp",
               ".scr", ".lnk", ".pif", ".jar", ".hta", ".reg", ".dll", ".sys", ".cpl", ".msc", ".appx", ".py", ".pyw"}
_OPENABLE = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt", ".md", ".csv", ".docx", ".xlsx", ".pptx", ".json"}


@dataclass
class Tool:
    name: str
    description: str
    properties: dict            # nombre -> {"type": ..., "description": ...}
    required: list[str]
    risk: str                   # leer | puro | escribir | ejecutar | abrir
    fn: Callable[[Policy, dict], str]

    @property
    def auto(self) -> bool:
        return self.risk in RISK_AUTO

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": self.properties, "required": self.required}}}

    def check_args(self, args) -> dict:
        """Valida tipos y claves: el modelo puede mandar cualquier cosa."""
        if not isinstance(args, dict):
            raise ToolDenied("Los argumentos deben ser un objeto.")
        out = {}
        for k, spec in self.properties.items():
            if k not in args:
                if k in self.required:
                    raise ToolDenied(f"Falta el argumento «{k}».")
                continue
            v = args[k]
            want = {"string": str, "boolean": bool, "integer": int}[spec["type"]]
            if isinstance(v, bool) and want is not bool or not isinstance(v, want):
                raise ToolDenied(f"El argumento «{k}» debe ser de tipo {spec['type']}.")
            if isinstance(v, str) and len(v) > MAX_WRITE:
                raise ToolDenied(f"El argumento «{k}» es demasiado largo.")
            if isinstance(v, str) and not self.auto and len(v) > MAX_REVIEW:
                raise ToolDenied(f"El argumento «{k}» tiene {len(v)} caracteres: es demasiado largo para que lo revises "
                                 f"antes de aprobarlo (máximo {MAX_REVIEW}). Pide una versión más corta.")
            out[k] = v
        extra = set(args) - set(self.properties)
        if extra:
            raise ToolDenied("Argumentos no reconocidos: " + ", ".join(sorted(map(str, extra))[:5]))
        return out

    def summary(self, args: dict) -> str:
        """Texto para mostrar al usuario ANTES de aprobar: la acción exacta y COMPLETA, con saltos de línea reales.
        Nunca se recorta: lo que se aprueba es lo que se ejecuta."""
        lines = [f"Herramienta: {self.name}"]
        for k, v in args.items():
            if isinstance(v, str) and ("\n" in v or len(v) > 80):
                lines += [f"\n{k}:", v]
            else:
                lines.append(f"{k}: {v!r}")
        return "\n".join(lines)


# ------------------------------------------------------------------- implementaciones
def _listar(pol: Policy, a: dict) -> str:
    p = pol.resolve(a.get("ruta", "."), must_exist=True)
    if not p.is_dir():
        raise ToolDenied(f"No es una carpeta: {p}")
    rows = []
    for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200]:
        try:
            rows.append(f"{'[carpeta]' if e.is_dir() else '[archivo]'} {e.name}" + ("" if e.is_dir() else f"  ({e.stat().st_size} bytes)"))
        except OSError:
            continue
    return f"{p}\n" + ("\n".join(rows) if rows else "(vacía)")


def _leer(pol: Policy, a: dict) -> str:
    p = pol.resolve(a["ruta"], must_exist=True)
    if not p.is_file():
        raise ToolDenied(f"No es un archivo: {p}")
    if p.suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(p) as doc:
            txt = "\n\n".join(f"[página {i + 1}]\n{pg.get_text()}" for i, pg in enumerate(doc))
        return txt[:MAX_READ]
    raw = p.read_bytes()[:MAX_READ + 1]
    if b"\x00" in raw[:4096]:
        raise ToolDenied("Es un archivo binario; solo se leen textos y PDFs.")
    txt = raw[:MAX_READ].decode("utf-8", "replace")
    return txt + ("\n… (recortado)" if len(raw) > MAX_READ else "")


def _buscar(pol: Policy, a: dict) -> str:
    pat = a["patron"].strip()
    if not pat or any(c in pat for c in "\\/:"):
        raise ToolDenied("El patrón es solo un nombre o comodín (por ejemplo «*.pdf»), sin rutas.")
    found = []
    for root in pol.read_roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if fnmatch.fnmatch(f.lower(), pat.lower()):
                    found.append(str(Path(dirpath, f)))
                    if len(found) >= 100:
                        return "\n".join(found) + "\n… (100 resultados)"
    return "\n".join(found) if found else "Sin resultados."


def _escribir(pol: Policy, a: dict) -> str:
    p = pol.resolve(a["ruta"], write=True)
    if p.suffix.lower() in _NEVER_OPEN:
        raise ToolDenied(f"No se crean archivos ejecutables ni de script ({p.suffix}).")
    if len(a["contenido"].encode("utf-8")) > MAX_WRITE:
        raise ToolDenied("El contenido es demasiado grande.")
    if p.exists() and not a.get("sobrescribir", False):
        raise ToolDenied(f"Ya existe {p.name}. Pide expresamente sobrescribir para reemplazarlo.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a["contenido"], encoding="utf-8")
    return f"Escrito {p} ({len(a['contenido'])} caracteres)."


def _ejecutar(pol: Policy, a: dict) -> str:
    r = run_python(a["codigo"], timeout=10)
    parts = [f"código de salida: {r.returncode}", f"tiempo: {r.seconds}s"]
    if r.timed_out:
        parts.append("TIEMPO AGOTADO (10 s)")
    if r.stdout:
        parts.append("salida:\n" + r.stdout)
    if r.stderr:
        parts.append("errores:\n" + r.stderr)
    if r.truncated:
        parts.append("(salida recortada)")
    return "\n".join(parts)


def _calcular(pol: Policy, a: dict) -> str:
    import sympy as sp
    from ..pdf.symcheck import Ambiguous, parse
    try:
        v = parse(a["expresion"])
    except (Ambiguous, ValueError, TypeError, ZeroDivisionError, RecursionError) as exc:
        raise ToolDenied(f"No se puede leer la expresión sin ambigüedad: {exc}") from exc
    r = sp.simplify(v)
    return f"{r}" + (f"  ≈ {sp.N(r, 12)}" if r.is_number and not r.is_Integer else "")


def _fecha(pol: Policy, a: dict) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S (hora local del equipo)")


def _abrir(pol: Policy, a: dict) -> str:
    p = pol.resolve(a["ruta"], must_exist=True)
    if p.is_file() and (p.suffix.lower() in _NEVER_OPEN or p.suffix.lower() not in _OPENABLE):
        raise ToolDenied(f"No se abren archivos {p.suffix or 'sin extensión'}: solo documentos e imágenes.")
    os.startfile(str(p))          # noqa: S606 (ruta validada: dentro de las carpetas permitidas, tipo permitido)
    return f"Abierto {p}"


S = {"type": "string"}
TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("listar_carpeta", "Lista los archivos de una carpeta permitida.", {"ruta": {**S, "description": "Carpeta (por defecto la de trabajo)"}},
         [], "leer", _listar),
    Tool("leer_archivo", "Lee un archivo de texto o PDF de una carpeta permitida.", {"ruta": {**S, "description": "Ruta del archivo"}},
         ["ruta"], "leer", _leer),
    Tool("buscar_archivos", "Busca archivos por nombre (admite comodines como *.pdf) en las carpetas permitidas.",
         {"patron": {**S, "description": "Nombre o comodín"}}, ["patron"], "leer", _buscar),
    Tool("calcular", "Calcula o simplifica una expresión matemática en LaTeX o con símbolos (por ejemplo \\frac{1}{3}(27+110)).",
         {"expresion": {**S, "description": "Expresión"}}, ["expresion"], "puro", _calcular),
    Tool("fecha_hora", "Devuelve la fecha y la hora actuales.", {}, [], "puro", _fecha),
    Tool("escribir_archivo", "Crea un archivo de texto en la carpeta de trabajo. El usuario debe aprobarlo.",
         {"ruta": {**S, "description": "Ruta del archivo"}, "contenido": {**S, "description": "Texto"},
          "sobrescribir": {"type": "boolean", "description": "Solo si el usuario pidió reemplazar uno existente"}},
         ["ruta", "contenido"], "escribir", _escribir),
    Tool("ejecutar_python", "Ejecuta código Python en un entorno aislado, sin red ni acceso a tus archivos. El usuario debe aprobarlo.",
         {"codigo": {**S, "description": "Código Python"}}, ["codigo"], "ejecutar", _ejecutar),
    Tool("abrir_archivo", "Abre un documento o imagen de una carpeta permitida con su programa predeterminado. El usuario debe aprobarlo.",
         {"ruta": {**S, "description": "Ruta del archivo o carpeta"}}, ["ruta"], "abrir", _abrir),
)}


def schemas() -> list[dict]:
    return [t.schema() for t in TOOLS.values()]
