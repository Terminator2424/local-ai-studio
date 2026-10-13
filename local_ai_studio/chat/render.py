"""HTML del chat para el visor (tkinterweb): una conversación como lista de mensajes con Markdown y LaTeX.

Todo el texto de los mensajes pasa por el modo seguro de md_to_html (sin HTML crudo, imágenes externas ni enlaces):
la salida de un modelo no es de fiar. Los mensajes terminados se renderizan con las fórmulas dibujadas; el que se está
escribiendo va con fórmulas en Unicode (rápido) y se redibuja al terminar.
"""
from __future__ import annotations

from functools import lru_cache

from ..core.mdview import THEMES, _css, md_to_html

ROLE_NAMES = {"user": "Tú", "assistant": "Asistente", "system": "Sistema"}


def chat_css(theme: str, font: str) -> str:
    t = THEMES[theme]
    return _css(t, font) + f"""
body {{ margin:6px 4px; }}
.msg {{ margin:12px 0; padding:2px 14px 6px; }}
.user {{ background:{t['card']}; border:1px solid {t['border']}; }}
.asst {{ border-left:3px solid {t['link']}; }}
.who {{ color:{t['muted']}; font-size:11px; margin:6px 0 0; }}
.attach {{ color:{t['muted']}; font-size:12px; margin:2px 0; }}
"""


def skeleton(theme: str, font: str, body: str = "") -> str:
    return (f"<html><head><meta charset='utf-8'><style>{chat_css(theme, font)}</style></head>"
            f"<body><div id='msgs'>{body}</div></body></html>")


@lru_cache(maxsize=256)
def _body(content: str, theme: str, font: str, formulas: bool) -> str:
    return md_to_html(content or " ", theme, font, formulas=formulas, safe=True)[0]


def body_html(content: str, theme: str, font: str, formulas: bool) -> str:
    """Interior HTML de un mensaje (Markdown + LaTeX en modo seguro), con caché."""
    return _body(content, theme, font, formulas)


def message_html(role: str, content: str, theme: str, font: str, *, formulas: bool = True,
                 images: tuple[str, ...] = (), element_id: str | None = None) -> str:
    cls = "user" if role == "user" else "asst"
    who = ROLE_NAMES.get(role, role)
    att = ""
    if images:
        names = ", ".join(i.replace("\\", "/").rsplit("/", 1)[-1] for i in images)
        att = f"<div class='attach'>Imagen: {_esc(names)}</div>"
    ident = f" id='{element_id}'" if element_id else ""
    return f"<div class='msg {cls}'><div class='who'>{who}</div>{att}<div{ident}>{_body(content, theme, font, formulas)}</div></div>"


def conversation_html(messages, theme: str, font: str, *, formulas: bool = True) -> str:
    """messages: objetos con .role, .content y .images. Devuelve solo el interior de <div id='msgs'>."""
    return "".join(message_html(m.role, m.content, theme, font, formulas=formulas, images=tuple(m.images or ()))
                   for m in messages if m.role != "system")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


EMPTY_HINT = "Escribe un mensaje para empezar. Todo se queda en tu equipo."


def empty_html(theme: str, font: str) -> str:
    t = THEMES[theme]
    return skeleton(theme, font, f"<p style='color:{t['muted']};text-align:center;margin-top:90px'>{EMPTY_HINT}</p>")
