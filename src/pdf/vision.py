"""Modelo de visión (qwen2.5vl:7b): transcripción de páginas con fórmulas / escaneadas y descripción de gráficos."""
from __future__ import annotations

import io
import logging
import threading
from pathlib import Path

from PIL import Image

from ..core.utils import chat_stream, fill

log = logging.getLogger("resumidor")


def prepare_image(path: str | Path, max_side: int) -> bytes:
    """Reduce la imagen (si hace falta) para no saturar la VRAM y la devuelve como PNG."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = max_side / max(w, h)
    if scale < 1:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class Vision:
    def __init__(self, cfg: dict, prompts: dict, lang_name: str, cancel: threading.Event, on_text=None):
        self.cfg = cfg
        self.prompts = prompts
        self.lang_name = lang_name
        self.cancel = cancel
        self.on_text = on_text
        self.model = cfg["ollama"]["vision_model"]
        self.options = dict(cfg["ollama"]["vision_options"])
        self.max_side = int(cfg["extraction"]["vision_max_side"])

    def _ask(self, prompt: str, image_path: str | Path, num_predict: int | None = None) -> str:
        messages = [{
            "role": "user",
            "content": prompt,
            "images": [prepare_image(image_path, self.max_side)],
        }]
        options = dict(self.options)
        if num_predict:
            options["num_predict"] = num_predict
        return chat_stream(self.cfg, self.model, messages, options, self.cancel, self.on_text)

    def transcribe_page(self, image_path: str | Path, page: int) -> str:
        """Transcribe la página completa a Markdown con fórmulas en LaTeX (también sirve de OCR)."""
        prompt = fill(self.prompts["ocr"], page=page, lang_name=self.lang_name)
        return self._ask(prompt, image_path, int(self.cfg["extraction"]["transcribe_tokens"]))

    def describe_figure(self, image_path: str | Path, page: int, doc_title: str, context: str,
                        allow_none: bool = True) -> str:
        """allow_none=False cuando ya se sabe que hay un gráfico (no se permite responder SIN_FIGURAS)."""
        prompt = fill(self.prompts["figure"], page=page, doc_title=doc_title, context=context[:1500],
                      lang_name=self.lang_name, none_rule=self.prompts["figure_none"] if allow_none else "")
        return self._ask(prompt, image_path)
