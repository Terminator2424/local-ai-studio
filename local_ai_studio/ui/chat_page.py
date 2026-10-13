"""Página «Chat»: conversación local con Ollama, historial en SQLite, imágenes para el modelo de visión.

La generación corre en un hilo aparte y solo habla con la interfaz por una cola (igual que el resumen de PDF). Mientras el modelo
escribe se repinta únicamente el mensaje en curso; al terminar se redibuja con las fórmulas.
"""
from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..chat import render
from ..agent.loop import Agent
from ..agent.policy import WORKSPACE, Policy, ToolDenied, validate_new_root
from ..chat.engine import CHAT_MODELS, DEFAULT_MODEL, TOOL_MODELS, ChatEngine, Params, Reply, build_messages, est_tokens, is_vision
from ..chat.store import ChatStore, load_prefs, save_prefs, title_from
from ..core.errors import Cancelled
from ..core.textutil import safe_name
from . import confirm_dialog
from .model_picker import ModelPicker
from .theme import (ACCENT, BORDER, FONT, LOG_COLORS, MUTED, SURFACE, TEXT, font, ghost_button, label,
                    primary_button, slider)

try:
    from tkinterweb import HtmlFrame
    HAS_WEB = True
except Exception:  # noqa: BLE001
    HAS_WEB = False

log = logging.getLogger(__name__)
VISION_MODEL = "qwen2.5vl:7b"


class ChatPage(ctk.CTkFrame):
    title = "Chat"

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.cfg = app.cfg
        self.store = ChatStore()
        self.engine = ChatEngine(self.cfg)
        self.prefs = load_prefs()
        self.policy = Policy(read_roots=[WORKSPACE, *self._saved_roots()], write_roots=[WORKSPACE])
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.conv_id: int | None = None
        self.messages: list = []
        self.attachments: list[str] = []
        self.streaming = False
        self.params_open = False
        self.log_open = False
        self._live_text = ""
        self._token = 0              # invalida los redibujados de fórmulas que llegan tarde
        self._dots = 0
        self._last_model_checked = ""

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_list()
        self._build_main()
        self._refresh_list()
        self.after(100, self._poll)
        self.after(400, self._animate)
        self._show_conversation(None)

    # ----------------------------------------------------------- utilidades
    @property
    def theme_name(self) -> str:
        return "dark" if self.app.dark else "light"

    def on_theme(self):
        t = self._textbox_log()
        for tag, color in LOG_COLORS[self.theme_name].items():
            t.tag_configure(tag, foreground=color)
        self._render_all()

    def _textbox_log(self):
        return self.log_box._textbox

    def busy(self) -> bool:
        return self.streaming

    def shutdown(self):
        self.cancel.set()
        with contextlib.suppress(Exception):        # cerrar al salir: si la base ya estaba cerrada no importa
            self.store.close()

    def shortcut(self, name: str):
        if name == "run":
            if not self.streaming:
                self._send()
        elif name == "save":
            self._export()

    # --------------------------------------------------------------- lista
    def _build_list(self):
        left = ctk.CTkFrame(self, fg_color="transparent", width=250)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 14))
        left.grid_propagate(False)
        left.grid_rowconfigure(1, weight=1)
        primary_button(left, "＋  Nueva conversación", self._new).grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.conv_list = ctk.CTkScrollableFrame(left, fg_color="transparent", scrollbar_button_color=BORDER, width=222)
        self.conv_list.grid(row=1, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)

    def _refresh_list(self):
        for w in self.conv_list.winfo_children():
            w.destroy()
        for c in self.store.conversations():
            sel = c.id == self.conv_id
            row = ctk.CTkFrame(self.conv_list, fg_color=SURFACE if sel else "transparent", corner_radius=8)
            row.pack(fill="x", pady=1)
            btn = ctk.CTkButton(row, text=c.title if len(c.title) < 26 else c.title[:25] + "…", anchor="w", height=30,
                                fg_color="transparent", hover_color=BORDER, text_color=TEXT, font=font(12),
                                command=lambda i=c.id: self._open(i))
            btn.pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="×", width=24, height=24, fg_color="transparent", hover_color=BORDER, text_color=MUTED,
                          command=lambda i=c.id: self._delete(i)).pack(side="right", padx=(0, 4))

    def _delete(self, conv_id: int):
        if self.streaming:
            return
        if messagebox.askyesno("Borrar conversación", "¿Borrar esta conversación y sus mensajes?"):
            self.store.delete(conv_id)
            if conv_id == self.conv_id:
                self._new()
            self._refresh_list()

    def _open(self, conv_id: int):
        if self.streaming:
            return
        conv = self.store.get(conv_id)
        if conv is None:
            return
        self.conv_id = conv_id
        self.model_var.set(conv.model if conv.model in CHAT_MODELS else self.prefs["model"])
        self._set_system(conv.system_prompt)
        self._show_conversation(conv_id)
        self._refresh_list()
        self._on_model(self.model_var.get())

    def _new(self):
        if self.streaming:
            return
        self.conv_id = None
        self.attachments.clear()
        self._refresh_attachments()
        self._show_conversation(None)
        self._refresh_list()
        self.input.focus_set()

    # ---------------------------------------------------------- zona principal
    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(main, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)
        self.picker = ModelPicker(head, list(CHAT_MODELS), self.prefs["model"], self._on_model, width=320, wrap=420)
        self.picker.grid(row=0, column=0, sticky="nw")
        self.model_var = self.picker.var            # mismo get()/set() que una StringVar, pero con el id del modelo
        actions = ctk.CTkFrame(head, fg_color="transparent")        # dos filas: caben aunque la ventana sea estrecha
        actions.grid(row=0, column=2, sticky="ne")
        ghost_button(actions, "Regenerar", self._regenerate, width=104, height=30).grid(row=0, column=0, padx=(0, 6), pady=(2, 4))
        ghost_button(actions, "Exportar .md", self._export, width=116, height=30).grid(row=0, column=1, pady=(2, 4))
        self.tools_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(actions, text="Herramientas", variable=self.tools_var, command=self._on_tools, progress_color=ACCENT,
                      font=font(12), text_color=TEXT, width=104).grid(row=1, column=0, sticky="w", padx=(2, 6))
        self.params_btn = ctk.CTkButton(actions, text="Parámetros  ▸", width=116, height=30, fg_color="transparent", hover_color=SURFACE,
                                        text_color=MUTED, font=font(12), command=self._toggle_params)
        self.params_btn.grid(row=1, column=1)
        self.tools_bar = ctk.CTkFrame(head, fg_color=SURFACE, corner_radius=8)
        self.tools_lbl = ctk.CTkLabel(self.tools_bar, text="", justify="left", anchor="w", font=font(11), text_color=MUTED)
        self.tools_lbl.pack(side="left", padx=10, pady=6)
        ghost_button(self.tools_bar, "Añadir carpeta de lectura…", self._add_root, width=190, height=26).pack(side="right", padx=6)

        self.params_box = ctk.CTkFrame(main, fg_color=SURFACE, corner_radius=10, border_width=1, border_color=BORDER)
        self._build_params(self.params_box)

        card = ctk.CTkFrame(main, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER)
        card.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)
        self.web = None
        if HAS_WEB:
            try:
                self.web = HtmlFrame(card, messages_enabled=False)
                self.web.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
            except Exception as exc:  # noqa: BLE001
                log.warning("tkinterweb no disponible en el chat: %s", exc)
        self.view = ctk.CTkTextbox(card, wrap="word", fg_color="transparent", font=font(14), text_color=TEXT, border_spacing=14)
        if self.web is None:
            self.view.grid(row=0, column=0, sticky="nsew")

        state = ctk.CTkFrame(main, fg_color="transparent")
        state.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        state.grid_columnconfigure(1, weight=1)
        self.typing = ctk.CTkLabel(state, text="", width=110, anchor="w", font=font(12, "bold"), text_color=ACCENT)
        self.typing.grid(row=0, column=0)
        self.progress = ctk.CTkProgressBar(state, height=4, mode="indeterminate", progress_color=ACCENT, fg_color=BORDER)
        self.stats = ctk.CTkLabel(state, text="", anchor="e", font=font(12), text_color=MUTED)
        self.stats.grid(row=0, column=2, sticky="e")
        self.attach_row = ctk.CTkFrame(main, fg_color="transparent")

        box = ctk.CTkFrame(main, fg_color="transparent")
        box.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        box.grid_columnconfigure(0, weight=1)
        self.input = ctk.CTkTextbox(box, height=70, wrap="word", fg_color=SURFACE, text_color=TEXT, border_width=1,
                                    border_color=BORDER, corner_radius=10, font=font(14))
        self.input.grid(row=0, column=0, sticky="ew")
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Escape>", lambda e: self.cancel.set() if self.streaming else None)
        btns = ctk.CTkFrame(box, fg_color="transparent")
        btns.grid(row=0, column=1, padx=(8, 0), sticky="ns")
        self.attach_btn = ghost_button(btns, "Imagen…", self._attach, width=96, height=30)
        self.attach_btn.pack(pady=(0, 6))
        self.send_btn = primary_button(btns, "Enviar", self._send, width=96, height=34)
        self.send_btn.pack()

        self.log_btn = ctk.CTkButton(main, text="Registro  ▸", anchor="w", height=24, fg_color="transparent", hover_color=SURFACE,
                                     text_color=MUTED, font=font(12), command=self._toggle_log)
        self.log_btn.grid(row=6, column=0, sticky="w", pady=(4, 0))
        self.log_box = ctk.CTkTextbox(main, height=110, fg_color=SURFACE, corner_radius=8, text_color=TEXT, border_width=1,
                                      border_color=BORDER, font=ctk.CTkFont("Consolas", 11), state="disabled", wrap="word")
        self.on_theme()
        self._on_model(self.model_var.get())

    def _build_params(self, box):
        box.grid_columnconfigure(1, weight=1)
        box.grid_columnconfigure(3, weight=1)
        self.temp_var = ctk.DoubleVar(value=self.prefs["temperature"])
        self.topp_var = ctk.DoubleVar(value=self.prefs["top_p"])
        self.tokens_var = ctk.StringVar(value=str(self.prefs["max_tokens"]))
        self.ctx_var = ctk.StringVar(value=str(self.prefs["num_ctx"]))
        self.temp_lbl = label(box, "", 12, muted=True, width=40)
        self.topp_lbl = label(box, "", 12, muted=True, width=40)
        label(box, "Temperatura", 12).grid(row=0, column=0, padx=(14, 8), pady=(10, 2), sticky="w")
        slider(box, from_=0, to=2, variable=self.temp_var, command=lambda _: self._upd_lbl()).grid(
            row=0, column=1, sticky="ew", pady=(10, 2))
        self.temp_lbl.grid(row=0, column=2, padx=6, pady=(10, 2))
        label(box, "top_p", 12).grid(row=1, column=0, padx=(14, 8), pady=2, sticky="w")
        slider(box, from_=0, to=1, variable=self.topp_var, command=lambda v: self._upd_lbl()).grid(row=1, column=1, sticky="ew", pady=2)
        self.topp_lbl.grid(row=1, column=2, padx=6, pady=2)
        label(box, "Tokens máx.", 12).grid(row=0, column=3, padx=(24, 8), pady=(10, 2), sticky="e")
        ctk.CTkEntry(box, textvariable=self.tokens_var, width=80, height=28, justify="center", fg_color=BORDER,
                     border_width=0, text_color=TEXT).grid(row=0, column=4, padx=(0, 14), pady=(10, 2))
        label(box, "Contexto", 12).grid(row=1, column=3, padx=(24, 8), pady=2, sticky="e")
        ctk.CTkEntry(box, textvariable=self.ctx_var, width=80, height=28, justify="center", fg_color=BORDER,
                     border_width=0, text_color=TEXT).grid(row=1, column=4, padx=(0, 14), pady=2)
        label(box, "System prompt", 12).grid(row=2, column=0, padx=(14, 8), pady=(6, 2), sticky="nw")
        self.system_box = ctk.CTkTextbox(box, height=58, wrap="word", fg_color=BORDER, text_color=TEXT, border_width=0,
                                         font=font(12))
        self.system_box.grid(row=2, column=1, columnspan=4, sticky="ew", padx=(0, 14), pady=(6, 12))
        self._set_system(self.prefs["system_prompt"])
        self._upd_lbl()

    def _upd_lbl(self):
        self.temp_lbl.configure(text=f"{self.temp_var.get():.2f}")
        self.topp_lbl.configure(text=f"{self.topp_var.get():.2f}")

    def _set_system(self, text: str):
        self.system_box.delete("1.0", "end")
        self.system_box.insert("1.0", text)

    def _toggle_params(self):
        self.params_open = not self.params_open
        if self.params_open:
            self.params_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        else:
            self.params_box.grid_forget()
        self.params_btn.configure(text="Parámetros  ▾" if self.params_open else "Parámetros  ▸")

    def _toggle_log(self):
        self.log_open = not self.log_open
        if self.log_open:
            self.log_box.grid(row=7, column=0, sticky="ew", pady=(4, 0))
        else:
            self.log_box.grid_forget()
        self.log_btn.configure(text="Registro  ▾" if self.log_open else "Registro  ▸")

    def _params(self) -> Params | None:
        try:
            mt, cx = int(self.tokens_var.get()), int(self.ctx_var.get())
            if not (16 <= mt <= 32768 and 512 <= cx <= 131072):
                raise ValueError
        except ValueError:
            messagebox.showerror("Parámetros no válidos", "Tokens máximos entre 16 y 32768, y contexto entre 512 y 131072.")
            return None
        return Params(round(self.temp_var.get(), 2), round(self.topp_var.get(), 2), mt, cx)

    def _save_prefs(self, p: Params):
        self.prefs.update(model=self.model_var.get(), temperature=p.temperature, top_p=p.top_p, max_tokens=p.max_tokens,
                          num_ctx=p.num_ctx, system_prompt=self.system_box.get("1.0", "end").strip())
        save_prefs(self.prefs)

    # -------------------------------------------------------------- modelo
    def _on_model(self, model: str):
        installed = self.engine.installed()
        self.picker.refresh_note(missing=bool(installed) and model not in installed)
        self.attach_btn.configure(state="normal" if is_vision(model) else "disabled")
        if not is_vision(model) and self.attachments:
            self.attachments.clear()
            self._refresh_attachments()

    def _ensure_model(self, model: str) -> bool:
        """Si el modelo no está instalado, pide permiso y lo descarga (bloquea hasta terminar, con progreso en pantalla)."""
        if model in self.engine.installed():
            return True
        if not messagebox.askyesno("Descargar modelo", f"«{model}» no está instalado.\n\nSe descargará desde ollama.com "
                                   "(varios GB). Los documentos y mensajes no salen del equipo.\n\n¿Descargarlo ahora?"):
            return False
        self._start_stream_ui(f"Descargando {model}…")
        threading.Thread(target=self._pull_worker, args=(model,), daemon=True).start()
        return False

    def _pull_worker(self, model: str):
        try:
            self.engine.pull(model, lambda f, s: self.q.put(("pull", f, f"{model}: {s}")), self.cancel)
            self.q.put(("pull_done", model))
        except Cancelled:
            self.q.put(("pull_error", "Descarga cancelada"))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("pull_error", f"{type(exc).__name__}: {exc}"))

    # ------------------------------------------------------------ imágenes
    def _attach(self):
        paths = filedialog.askopenfilenames(title="Imagen para el modelo", filetypes=[("Imágenes", "*.png *.jpg *.jpeg *.webp")])
        for p in paths:
            if p not in self.attachments:
                self.attachments.append(p)
        self._refresh_attachments()

    def _refresh_attachments(self):
        for w in self.attach_row.winfo_children():
            w.destroy()
        if not self.attachments:
            self.attach_row.grid_forget()
            return
        self.attach_row.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        for p in self.attachments:
            chip = ctk.CTkFrame(self.attach_row, fg_color=SURFACE, corner_radius=8, border_width=1, border_color=BORDER)
            chip.pack(side="left", padx=(0, 6))
            ctk.CTkLabel(chip, text=Path(p).name[:28], font=font(12), text_color=TEXT).pack(side="left", padx=(8, 2), pady=2)
            ctk.CTkButton(chip, text="×", width=22, height=22, fg_color="transparent", hover_color=BORDER, text_color=MUTED,
                          command=lambda p=p: (self.attachments.remove(p), self._refresh_attachments())).pack(side="left")

    # ------------------------------------------------------------- mostrar
    def _show_conversation(self, conv_id: int | None):
        self.messages = self.store.messages(conv_id) if conv_id else []
        self.stats.configure(text="")
        self._render_all()

    def _render_all(self):
        if self.web is None:
            self._render_text()
            return
        theme = self.theme_name
        self._token += 1
        token = self._token
        if not self.messages:
            self.web.load_html(render.empty_html(theme, FONT))
            return
        self.web.load_html(render.skeleton(theme, FONT, render.conversation_html(self.messages, theme, FONT, formulas=False)))
        self._scroll_end()

        def work():
            try:
                self.q.put(("html", token, render.conversation_html(self.messages, theme, FONT, formulas=True)))
            except Exception as exc:  # noqa: BLE001
                log.warning("No se pudieron dibujar las fórmulas del chat: %s", exc)
        threading.Thread(target=work, daemon=True).start()

    def _render_text(self):
        t = self.view._textbox
        t.configure(state="normal")
        t.delete("1.0", "end")
        for m in self.messages:
            t.insert("end", f"{render.ROLE_NAMES.get(m.role, m.role)}:\n{m.content}\n\n")
        t.configure(state="disabled")
        t.see("end")

    def _scroll_end(self):
        if self.web is not None:
            self.after(60, lambda: self.web.yview_moveto(1.0))

    def _set_msgs_html(self, html: str):
        try:
            self.web.document.getElementById("msgs").innerHTML = html
        except Exception:  # noqa: BLE001
            self.web.load_html(render.skeleton(self.theme_name, FONT, html))

    # ------------------------------------------------------------- enviar
    def _on_enter(self, event):
        if event.state & 0x1:       # Mayús+Enter: salto de línea
            return None
        if not self.streaming:      # una tecla nunca cancela: solo el botón «Detener» (o Esc) interrumpe la respuesta
            self._send()
        return "break"

    def _send(self):
        if self.streaming:
            self.cancel.set()
            return
        text = self.input.get("1.0", "end").strip()
        if not text:
            return
        model = self.model_var.get()
        images = list(self.attachments)
        if images and not is_vision(model):
            model = VISION_MODEL
            self.model_var.set(model)
            self._on_model(model)
        params = self._params()
        if params is None:
            return
        if not self._ensure_model(model):
            return
        system = self.system_box.get("1.0", "end").strip()
        if self.conv_id is None:
            self.conv_id = self.store.create(model, system, title_from(text))
        else:
            self.store.update(self.conv_id, model=model, system_prompt=system)
        history = self.store.messages(self.conv_id)
        self.store.add(self.conv_id, "user", text, images)
        self.messages = self.store.messages(self.conv_id)
        self._save_prefs(params)
        self.input.delete("1.0", "end")
        self.attachments.clear()
        self._refresh_attachments()
        self._refresh_list()
        self._start_generation(model, system, history, text, images, params)

    def _regenerate(self):
        if self.streaming or self.conv_id is None:
            return
        msgs = self.store.messages(self.conv_id)
        if not msgs or msgs[-1].role != "assistant":
            return
        self.store.drop_last(self.conv_id, "assistant")
        msgs = self.store.messages(self.conv_id)
        last_user = msgs[-1]
        params = self._params()
        if params is None:
            return
        self.messages = msgs
        model = self.model_var.get()
        if not self._ensure_model(model):
            return
        self._start_generation(model, self.system_box.get("1.0", "end").strip(), msgs[:-1], last_user.content,
                               last_user.images, params)

    def _start_generation(self, model, system, history, text, images, params: Params):
        msgs = build_messages(system, history, text, images, is_vision(model))
        self._live_text = ""
        self._start_stream_ui("escribiendo")
        if self.web is not None:
            theme = self.theme_name
            body = render.conversation_html(self.messages, theme, FONT, formulas=False)
            body += render.message_html("assistant", "…", theme, FONT, formulas=False, element_id="live")
            self.web.load_html(render.skeleton(theme, FONT, body))
            self._scroll_end()
        else:
            self._render_text()
        self._log(f"▶ Petición a {model}  (T {params.temperature}, top_p {params.top_p}, máx {params.max_tokens}, ctx {params.num_ctx})")
        self.cancel.clear()
        if self.tools_var.get():
            threading.Thread(target=self._agent_worker, args=(model, msgs, params), daemon=True).start()
        else:
            threading.Thread(target=self._worker, args=(model, msgs, params), daemon=True).start()

    def _worker(self, model: str, msgs: list, params: Params):
        t0 = time.time()
        try:
            self.q.put(("status", "preparando la VRAM…"))
            self.app.manager.use_llm(model)
            self.q.put(("status", "escribiendo"))
            reply = self.engine.stream(model, msgs, params, self.cancel, on_text=lambda t: self.q.put(("text", t)))
            self.q.put(("done", reply, time.time() - t0))
        except Cancelled:
            self.q.put(("cancelled", self._live_text))
        except Exception as exc:
            log.exception("Error en el chat")
            self.q.put(("error", f"{type(exc).__name__}: {exc}"))

    def _agent_worker(self, model: str, msgs: list, params: Params):
        t0 = time.time()
        try:
            self.q.put(("status", "preparando la VRAM…"))
            self.app.manager.use_llm(model)
            self.q.put(("status", "trabajando con herramientas"))
            agent = Agent(self.cfg, self.policy, self._confirm_from_worker)
            text = agent.run(model, msgs, params.options(), self.cancel, lambda e: self.q.put(("status", e)))
            el = time.time() - t0
            self.q.put(("done", Reply(text, est_tokens(text), el), el))
        except Cancelled:
            self.q.put(("cancelled", ""))
        except Exception as exc:
            log.exception("Error del asistente con herramientas")
            msg = f"{type(exc).__name__}: {exc}"
            if "does not support tools" in str(exc):
                msg = f"El modelo {model} no admite herramientas. Usa Llama 3.1, Qwen 2.5, Qwen Coder o Mistral."
            self.q.put(("error", msg))

    def _confirm_from_worker(self, tool: str, summary: str, tainted: bool) -> bool:
        """Se llama desde el hilo del asistente: la ventana se muestra en el hilo principal y el hilo espera la decisión."""
        holder: dict[str, Any] = {"ok": False, "ev": threading.Event()}
        self.q.put(("confirm", tool, summary, tainted, holder))
        holder["ev"].wait(timeout=confirm_dialog.TIMEOUT_MS / 1000 + 20)
        self._log(("✔ Permitida: " if holder["ok"] else "⚠ Denegada: ") + summary[:100])
        return bool(holder["ok"])

    def _saved_roots(self) -> list:
        out = []
        for r in self.prefs.get("read_roots", []):
            try:
                out.append(validate_new_root(r))
            except (ToolDenied, OSError) as exc:
                log.info("Carpeta de lectura guardada descartada (%s): %s", r, exc)
        return out

    def _on_tools(self):
        if self.tools_var.get():
            if self.model_var.get() not in TOOL_MODELS:
                self.model_var.set(DEFAULT_MODEL)
                self._on_model(DEFAULT_MODEL)
            self.tools_bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            self._refresh_tools_label()
        else:
            self.tools_bar.grid_forget()

    def _refresh_tools_label(self):
        reads = [str(r) for r in self.policy.read_roots]
        extra = "\nTambién puede LEER: " + "; ".join(reads[1:]) if len(reads) > 1 else ""
        self.tools_lbl.configure(text="Escribe y ejecuta solo con tu permiso.  Trabajo: " + str(WORKSPACE) + extra)

    def _add_root(self):
        path = filedialog.askdirectory(title="Carpeta que el asistente podrá LEER (no escribir)")
        if not path:
            return
        try:
            root = validate_new_root(path)
        except ToolDenied as exc:
            messagebox.showwarning("Carpeta no permitida", str(exc))
            return
        ask_text = (f"El asistente podrá LEER archivos de:\n\n{root}\n\n"
                    "No podrá escribir, borrar ni ejecutar nada ahí. Las claves y credenciales siguen bloqueadas.\n\n¿Permitir?")
        if not messagebox.askyesno("Permitir lectura", ask_text):
            return
        if root not in self.policy.read_roots:
            self.policy.read_roots.append(root)
            self.prefs["read_roots"] = [str(r) for r in self.policy.read_roots[1:]]
            save_prefs(self.prefs)
        self._refresh_tools_label()

    def _start_stream_ui(self, text: str):
        self.streaming = True
        self.send_btn.configure(text="Detener")
        self.typing.configure(text=text)
        self.progress.grid(row=0, column=1, sticky="ew", padx=10)
        self.progress.start()

    def _end_stream_ui(self):
        self.streaming = False
        self.send_btn.configure(text="Enviar")
        self.typing.configure(text="")
        self.progress.stop()
        self.progress.grid_forget()

    def _animate(self):
        if self.streaming and self.typing.cget("text").startswith(("escribiendo", "preparando")):
            self._dots = (self._dots + 1) % 4
            base = self.typing.cget("text").rstrip(".")
            self.typing.configure(text=base + "." * self._dots)
        self.after(400, self._animate)

    # --------------------------------------------------------------- cola
    def _poll(self):
        latest_text = None
        try:
            for _ in range(80):
                msg = self.q.get_nowait()
                if msg[0] == "text":
                    latest_text = msg[1]          # solo se pinta el último texto de cada ciclo
                else:
                    if latest_text is not None:
                        self._on_text(latest_text)
                        latest_text = None
                    self._handle(msg)
        except queue.Empty:
            pass
        if latest_text is not None:
            self._on_text(latest_text)
        self.after(120, self._poll)

    def _on_text(self, text: str):
        self._live_text = text
        if self.web is not None:
            try:
                self.web.document.getElementById("live").innerHTML = render.body_html(text, self.theme_name, FONT, False)
                self.web.yview_moveto(1.0)
            except Exception:  # noqa: BLE001
                body = render.conversation_html(self.messages, self.theme_name, FONT, formulas=False)
                body += render.message_html("assistant", text, self.theme_name, FONT, formulas=False, element_id="live")
                self.web.load_html(render.skeleton(self.theme_name, FONT, body))
        else:
            t = self.view._textbox
            t.configure(state="normal")
            t.delete("1.0", "end")
            for m in self.messages:
                t.insert("end", f"{render.ROLE_NAMES.get(m.role, m.role)}:\n{m.content}\n\n")
            t.insert("end", f"Asistente:\n{text}")
            t.configure(state="disabled")
            t.see("end")

    def _handle(self, msg):
        kind = msg[0]
        if kind == "status":
            self.typing.configure(text=msg[1])
        elif kind == "done":
            _, reply, wall = msg
            self._finish(reply.text)
            self.stats.configure(text=f"{reply.tokens} tokens · {reply.tokens_per_s:.1f} tok/s · {wall:.1f} s")
            self._log(f"✔ {reply.tokens} tokens en {reply.seconds:.1f} s ({reply.tokens_per_s:.1f} tok/s); "
                      f"prompt {reply.prompt_tokens} tokens; total {wall:.1f} s")
        elif kind == "confirm":
            _, tool, summary, tainted, holder = msg
            holder["ok"] = confirm_dialog.ask(self, tool, summary, tainted)
            holder["ev"].set()
        elif kind == "cancelled":
            self._finish((msg[1] + "\n\n_(respuesta interrumpida)_") if msg[1].strip() else "")
            self._log("⚠ Respuesta interrumpida")
        elif kind == "error":
            self._end_stream_ui()
            self._log(f"✖ {msg[1]}")
            self._render_all()
            messagebox.showerror("Error en el chat", msg[1] + "\n\n¿Está Ollama en marcha? Se usa solo en 127.0.0.1.")
        elif kind == "html":
            if msg[1] == self._token and self.web is not None and not self.streaming:
                self._set_msgs_html(msg[2])
        elif kind == "pull":
            self.typing.configure(text=msg[2][:60])
            if msg[1] is not None:
                self.progress.stop()
                self.progress.configure(mode="determinate")
                self.progress.set(msg[1])
        elif kind == "pull_done":
            self.progress.configure(mode="indeterminate")
            self._end_stream_ui()
            self._on_model(msg[1])
            self._log(f"✔ Modelo {msg[1]} descargado")
            messagebox.showinfo("Modelo descargado", f"{msg[1]} está listo. Vuelve a pulsar Enviar.")
        elif kind == "pull_error":
            self.progress.configure(mode="indeterminate")
            self._end_stream_ui()
            self._log(f"✖ {msg[1]}")
            messagebox.showerror("No se pudo descargar", msg[1])

    def _finish(self, text: str):
        self._end_stream_ui()
        if text.strip() and self.conv_id is not None:
            self.store.add(self.conv_id, "assistant", text)
        self.messages = self.store.messages(self.conv_id) if self.conv_id else []
        self._refresh_list()
        self._render_all()

    # ------------------------------------------------------------ exportar
    def _export(self):
        if self.conv_id is None:
            messagebox.showinfo("Nada que exportar", "Todavía no hay ninguna conversación.")
            return
        conv = self.store.get(self.conv_id)
        path = filedialog.asksaveasfilename(title="Exportar conversación", initialfile=safe_name(conv.title) + ".md",
                                            defaultextension=".md", filetypes=[("Markdown", "*.md")])
        if path:
            Path(path).write_text(self.store.export_markdown(self.conv_id), encoding="utf-8")
            self._log(f"✔ Exportada a {path}")

    def _log(self, text: str):
        tag = "ok" if text.startswith("✔") else "run" if text.startswith("▶") else "warn" if text.startswith("⚠") else \
            "err" if text.startswith("✖") else None
        t = self._textbox_log()
        t.configure(state="normal")
        t.insert("end", f"{time.strftime('%H:%M:%S')}  ", "time")
        t.insert("end", text + "\n", tag or ())
        t.see("end")
        t.configure(state="disabled")
