"""Página «Estudiar»: tarjetas, preguntas de práctica y ejercicios paso a paso, con cada igualdad verificada y el código
ejecutable en el sandbox (solo con tu permiso)."""
from __future__ import annotations

import logging
import queue
import threading
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..agent.sandbox import run_python
from ..chat.engine import CHAT_MODELS, ChatEngine, Params
from ..core.utils import Cancelled
from ..rag.index import DocIndex
from ..study import check, tasks
from . import confirm_dialog
from .htmlview import MdView
from .theme import ACCENT, BORDER, MUTED, SURFACE, TEXT, font, ghost_button, label, option_menu, primary_button

log = logging.getLogger("resumidor")
PASTE = "— texto pegado —"


class StudyPage(ctk.CTkFrame):
    title = "Estudiar"

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.cfg = app.cfg
        self.engine = ChatEngine(self.cfg)
        self.index = DocIndex(self.cfg)
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.working = False
        self.cards: list[dict] = []
        self.blocks: list[tuple[str, str]] = []
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_left()
        self._build_main()
        self.after(120, self._poll)

    def busy(self) -> bool:
        return self.working

    def shutdown(self):
        self.cancel.set()
        try:
            self.index.close()
        except Exception:  # noqa: BLE001
            pass

    def on_theme(self):
        self.view.on_theme()

    def on_show(self):
        self._refresh_docs()

    def shortcut(self, name: str):
        if name == "run":
            self._go()

    # ------------------------------------------------------------------ UI
    def _build_left(self):
        left = ctk.CTkScrollableFrame(self, width=310, fg_color="transparent", scrollbar_button_color=BORDER)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        label(left, "Fuente", 13, "bold").pack(anchor="w", padx=6)
        self.doc_menu = option_menu(left, [PASTE], None, self._on_doc)
        self.doc_menu.set(PASTE)
        self.doc_menu.pack(fill="x", padx=6, pady=(4, 0))
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(8, 0))
        label(row, "Páginas", 12).pack(side="left")
        self.p1, self.p2 = ctk.StringVar(value="1"), ctk.StringVar(value="3")
        for var in (self.p1, self.p2):
            ctk.CTkEntry(row, textvariable=var, width=52, height=28, justify="center", fg_color=SURFACE, border_color=BORDER,
                         text_color=TEXT).pack(side="left", padx=(8, 0))
        label(left, "Texto de apoyo (si no eliges un documento)", 12, muted=True).pack(anchor="w", padx=6, pady=(12, 2))
        self.source = ctk.CTkTextbox(left, height=120, wrap="word", fg_color=SURFACE, text_color=TEXT, border_width=1,
                                     border_color=BORDER, font=font(12))
        self.source.pack(fill="x", padx=6)
        label(left, "Tarea", 13, "bold").pack(anchor="w", padx=6, pady=(14, 4))
        self.task_menu = option_menu(left, list(tasks.TASKS), None, self._on_task)
        self.task_menu.set(next(iter(tasks.TASKS)))
        self.task_menu.pack(fill="x", padx=6)
        self.ex_lbl = label(left, "Ejercicio a resolver", 12, muted=True)
        self.exercise = ctk.CTkTextbox(left, height=96, wrap="word", fg_color=SURFACE, text_color=TEXT, border_width=1,
                                       border_color=BORDER, font=font(12))
        label(left, "Modelo", 12, muted=True).pack(anchor="w", padx=6, pady=(14, 2))
        self.model_var = ctk.StringVar(value="qwen2.5:7b")
        option_menu(left, [m for m in CHAT_MODELS if "vl" not in m], self.model_var, None).pack(fill="x", padx=6)
        self.go_btn = primary_button(left, "Generar", self._go, height=42)
        self.go_btn.pack(fill="x", padx=6, pady=(18, 4))
        self.prog = ctk.CTkProgressBar(left, height=5, progress_color=ACCENT, fg_color=BORDER, mode="indeterminate")
        self.status = label(left, "", 12, muted=True, wraplength=290, justify="left")
        self.status.pack(anchor="w", padx=8, pady=(6, 0))
        self.csv_btn = ghost_button(left, "Exportar tarjetas (CSV para Anki)", self._export_csv, height=30)
        self._on_task(self.task_menu.get())

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)
        self.stats = label(main, "", 12, muted=True)
        self.stats.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.view = MdView(main, self.app, "Elige un documento (o pega texto), una tarea y pulsa «Generar».\n\n"
                                           "Las igualdades del resultado se comprueban con código: ✓ verificada, ✗ incorrecta, "
                                           "⚠ sin verificar.")
        self.view.grid(row=1, column=0, sticky="nsew")
        self.code_box = ctk.CTkFrame(main, fg_color=SURFACE, corner_radius=10, border_width=1, border_color=BORDER)
        self.code_out = ctk.CTkTextbox(self.code_box, height=90, wrap="word", fg_color="transparent", text_color=TEXT,
                                       font=ctk.CTkFont("Consolas", 11), state="disabled")

    def _on_task(self, value):
        solve = tasks.TASKS[value] == "solve"
        if solve:
            self.ex_lbl.pack(after=self.task_menu, anchor="w", padx=6, pady=(12, 2))
            self.exercise.pack(after=self.ex_lbl, fill="x", padx=6)
        else:
            self.ex_lbl.pack_forget()
            self.exercise.pack_forget()

    def _refresh_docs(self):
        self._docs = {r["name"]: r["id"] for r in self.index.docs()}
        self.doc_menu.configure(values=[PASTE, *self._docs])

    def _on_doc(self, value):
        if value in getattr(self, "_docs", {}):
            r = next(x for x in self.index.docs() if x["id"] == self._docs[value])
            self.p1.set("1")
            self.p2.set(str(min(r["pages"], 3)))

    # ---------------------------------------------------------------- generar
    def _go(self):
        if self.working:
            self.cancel.set()
            return
        key = tasks.TASKS[self.task_menu.get()]
        name = self.doc_menu.get()
        if name in getattr(self, "_docs", {}):
            try:
                a, b = int(self.p1.get()), int(self.p2.get())
            except ValueError:
                messagebox.showerror("Páginas", "Escribe números de página.")
                return
            if not 1 <= a <= b <= a + 14:
                messagebox.showerror("Páginas", "Elige un rango válido de como mucho 15 páginas.")
                return
            source = self.index.page_text(self._docs[name], a, b)
        else:
            source = self.source.get("1.0", "end").strip()
        if len(source) < 40:
            messagebox.showinfo("Falta la fuente", "Elige un documento indexado o pega algo de texto.")
            return
        exercise = self.exercise.get("1.0", "end").strip() if key == "solve" else ""
        if key == "solve" and not exercise:
            messagebox.showinfo("Falta el ejercicio", "Escribe el ejercicio que quieres resolver.")
            return
        self.working = True
        self.cancel.clear()
        self.go_btn.configure(text="Detener")
        self.csv_btn.pack_forget()
        self.code_box.grid_forget()
        self.prog.pack(fill="x", padx=6, pady=(6, 0))
        self.prog.start()
        self.status.configure(text="Pensando…")
        threading.Thread(target=self._worker, args=(key, source, exercise, self.model_var.get()), daemon=True).start()

    def _worker(self, key: str, source: str, exercise: str, model: str):
        try:
            self.app.manager.use_llm(model)
            reply = self.engine.stream(model, tasks.messages(key, source, exercise), Params(0.3, 0.9, 1200, 8192), self.cancel)
            self.q.put(("done", key, reply.text))
        except Cancelled:
            self.q.put(("error", "Cancelado."))
        except Exception as exc:  # noqa: BLE001
            log.exception("Modo estudiar")
            self.q.put(("error", f"{type(exc).__name__}: {exc}"))

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] in ("done", "error"):
                    self.working = False
                    self.go_btn.configure(text="Generar")
                    self.prog.stop()
                    self.prog.pack_forget()
                    self.status.configure(text="")
                if msg[0] == "done":
                    self._show_result(msg[1], msg[2])
                elif msg[0] == "error":
                    self.view.show(f"**{msg[1]}**")
                elif msg[0] == "code_out":
                    self._set_code_out(msg[1])
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _show_result(self, key: str, text: str):
        self.cards = []
        if key == "flashcards":
            self.cards = tasks.parse_flashcards(text)
            if self.cards:
                self.view.show(tasks.cards_markdown(self.cards))
                self.stats.configure(text=f"{len(self.cards)} tarjetas.")
                self.csv_btn.pack(fill="x", padx=6, pady=(10, 0))
                return
            self.stats.configure(text="El modelo no devolvió un JSON válido; se muestra su texto tal cual.")
            self.view.show(text)
            return
        annotated, st = check.annotate(text)
        self.stats.configure(text=st.summary())
        self.view.show(annotated)
        self.blocks = [(lang, code) for lang, code in check.code_blocks(text)]
        self._show_code_panel()

    # -------------------------------------------------------- código
    def _show_code_panel(self):
        for w in self.code_box.winfo_children():
            w.destroy()
        py = [(i, c) for i, (lang, c) in enumerate(self.blocks) if lang in ("python", "py", "")]
        if not py:
            self.code_box.grid_forget()
            return
        self.code_box.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        top = ctk.CTkFrame(self.code_box, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 0))
        label(top, "Código Python del resultado:", 12, "bold").pack(side="left")
        for n, (i, code) in enumerate(py, 1):
            ghost_button(top, f"Ejecutar bloque {n}…", lambda c=code: self._run_code(c), width=140, height=26).pack(side="left", padx=6)
        label(self.code_box, "Se ejecuta aislado: sin red y sin acceso a tus archivos. Antes verás el código completo.", 11,
              muted=True).pack(anchor="w", padx=10)
        self.code_out = ctk.CTkTextbox(self.code_box, height=90, wrap="word", fg_color="transparent", text_color=TEXT,
                                       font=ctk.CTkFont("Consolas", 11), state="disabled")
        self.code_out.pack(fill="x", padx=10, pady=(4, 8))

    def _run_code(self, code: str):
        if not confirm_dialog.ask(self, "ejecutar_python", code, False):
            self._set_code_out("(Ejecución denegada.)")
            return
        self._set_code_out("Ejecutando…")
        threading.Thread(target=lambda: self.q.put(("code_out", self._fmt(run_python(code)))), daemon=True).start()

    @staticmethod
    def _fmt(r) -> str:
        parts = []
        if r.timed_out:
            parts.append("TIEMPO AGOTADO (10 s)")
        if r.stdout:
            parts.append(r.stdout.rstrip())
        if r.stderr:
            parts.append("ERROR:\n" + r.stderr.rstrip())
        return "\n".join(parts) or "(sin salida)"

    def _set_code_out(self, text: str):
        t = self.code_out._textbox
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", text)
        t.configure(state="disabled")

    def _export_csv(self):
        if not self.cards:
            return
        path = filedialog.asksaveasfilename(title="Exportar tarjetas", defaultextension=".csv", initialfile="tarjetas.csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(tasks.cards_csv(self.cards))
            self.status.configure(text=f"Guardado en {path}")
