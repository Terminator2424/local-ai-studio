"""Selector de modelo con descripción: cada opción dice para qué sirve y, debajo, el detalle del elegido."""
from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from ..chat.engine import MODEL_INFO, model_detail, model_label
from .theme import MUTED, WARN, label, option_menu


class _IdVar:
    """Con la misma interfaz que ctk.StringVar, pero trabaja con el id de Ollama (no con el texto mostrado)."""

    def __init__(self, picker: ModelPicker):
        self._p = picker

    def get(self) -> str:
        return self._p.get()

    def set(self, model: str) -> None:
        self._p.set(model)


class ModelPicker(ctk.CTkFrame):
    """Combo «Nombre — para qué sirve» + una línea de detalle (tamaño, capacidades, si falta instalarlo)."""

    def __init__(self, master, models: list[str], default: str, command: Callable[[str], None] | None = None,
                 width: int = 300, wrap: int = 420):
        super().__init__(master, fg_color="transparent")
        self._models = [m for m in models if m in MODEL_INFO] or list(models)
        self._by_label = {model_label(m): m for m in self._models}
        self._command = command
        self._current = default if default in self._models else self._models[0]
        self.var = _IdVar(self)
        self.menu = option_menu(self, list(self._by_label), None, self._on_pick, width=width)
        self.menu.pack(anchor="w")
        self.note = label(self, "", 11, muted=True, wraplength=wrap, justify="left")
        self.note.pack(anchor="w", padx=2, pady=(3, 0))
        self.set(self._current)

    def get(self) -> str:
        return self._current

    def set(self, model: str, notify: bool = False) -> None:
        if model not in self._models:
            return
        self._current = model
        self.menu.set(model_label(model))
        self.refresh_note()
        if notify and self._command:
            self._command(model)

    def refresh_note(self, missing: bool | None = None) -> None:
        """Actualiza el detalle. `missing` lo decide quien conoce los modelos instalados (así no se consulta a Ollama aquí)."""
        detail = model_detail(self._current)
        if missing:
            self.note.configure(text=f"{detail}\nNo instalado: se descargará al usarlo.", text_color=WARN)
        else:
            self.note.configure(text=detail, text_color=MUTED)

    def _on_pick(self, shown: str) -> None:
        self._current = self._by_label.get(shown, self._current)
        self.refresh_note()
        if self._command:
            self._command(self._current)
