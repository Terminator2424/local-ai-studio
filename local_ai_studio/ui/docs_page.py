"""Página «Documentos»: pregunta a tus PDFs. Respuestas con citas de página y comprobación de cifras (todo en local)."""
from __future__ import annotations

import contextlib
import logging
import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..chat.engine import CHAT_MODELS, ChatEngine, Params
from ..chat.store import load_prefs, update_prefs
from ..core.errors import Cancelled
from ..rag import ask
from ..rag.index import EMBED_MODEL, DocIndex
from .htmlview import MdView
from .model_picker import ModelPicker
from .theme import ACCENT, BORDER, MUTED, OK, SURFACE, TEXT, WARN, font, ghost_button, label, primary_button

log = logging.getLogger(__name__)


class DocsPage(ctk.CTkFrame):
    title = "Documentos"

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.cfg = app.cfg
        self.engine = ChatEngine(self.cfg)
        self.index = DocIndex(self.cfg)
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.working = False
        self.mode: str | None = None            # "index" | "ask" mientras hay trabajo en curso
        self.selected: dict[int, ctk.BooleanVar] = {}      # documentos marcados para preguntar (por id)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_left()
        self._build_main()
        self._refresh()
        self.after(120, self._poll)

    def busy(self) -> bool:
        return self.working

    def shutdown(self):
        self.cancel.set()
        with contextlib.suppress(Exception):        # cerrar al salir: si la base ya estaba cerrada no importa
            self.index.close()

    def on_theme(self):
        self.view.on_theme()

    def on_show(self):
        self._refresh()

    # ------------------------------------------------------------------ UI
    def _build_left(self):
        left = ctk.CTkFrame(self, fg_color="transparent", width=290)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        left.grid_propagate(False)
        left.grid_rowconfigure(3, weight=1)
        label(left, "Documentos indexados", 13, "bold").grid(row=0, column=0, sticky="w", pady=(0, 6))
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew")
        primary_button(row, "Indexar PDFs…", self._add_files, width=130, height=34).pack(side="left")
        ghost_button(row, "Carpeta…", self._add_folder, width=90, height=34).pack(side="left", padx=6)
        info = ctk.CTkFrame(left, fg_color="transparent")
        info.grid(row=2, column=0, sticky="ew", pady=(8, 4))
        self.emb_lbl = label(info, "", 11, muted=True, wraplength=270, justify="left")
        self.emb_lbl.pack(anchor="w")
        self.scope_lbl = label(info, "", 11, muted=True, wraplength=270, justify="left")
        self.scope_lbl.pack(anchor="w")
        self.listbox = ctk.CTkScrollableFrame(left, fg_color="transparent", scrollbar_button_color=BORDER, width=262)
        self.listbox.grid(row=3, column=0, sticky="nsew")
        self.prog = ctk.CTkProgressBar(left, height=5, progress_color=ACCENT, fg_color=BORDER)
        self.prog.set(0)
        self.prog.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.status = label(left, "", 11, muted=True, wraplength=270, justify="left")
        self.status.grid(row=5, column=0, sticky="w", pady=(4, 0))

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)
        self.view = MdView(main, self.app, "Indexa uno o varios PDFs y pregúntales lo que quieras.\n\n"
                                           "Cada respuesta cita la página y se comprueba que sus cifras existan en el texto.")
        self.view.grid(row=1, column=0, sticky="nsew")
        top = ctk.CTkFrame(main, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        top.grid_columnconfigure(0, weight=1)
        self.question = ctk.CTkTextbox(top, height=64, wrap="word", fg_color=SURFACE, text_color=TEXT, border_width=1,
                                       border_color=BORDER, corner_radius=10, font=font(14))
        self.question.grid(row=0, column=0, sticky="ew")
        self.question.bind("<Return>", self._on_enter)
        side = ctk.CTkFrame(top, fg_color="transparent")
        side.grid(row=0, column=1, padx=(10, 0), sticky="ns")
        self.picker = ModelPicker(side, [m for m in CHAT_MODELS if "vl" not in m], load_prefs()["docs_model"],
                                  lambda m: update_prefs({"docs_model": m}), width=300, wrap=300)
        self.picker.pack(pady=(0, 6), anchor="e")
        self.model_var = self.picker.var
        self.ask_btn = primary_button(side, "Preguntar", self._ask, width=300, height=34)
        self.ask_btn.pack(anchor="e")

    def _on_enter(self, event):
        if event.state & 0x1:
            return None
        if not self.working:        # una tecla nunca cancela: solo el botón «Detener»
            self._ask()
        return "break"

    def shortcut(self, name: str):
        if name == "run":
            if not self.working:
                self._ask()
        elif name == "open":
            self._add_files()

    # ------------------------------------------------------------- indexar
    def _refresh(self):
        for w in self.listbox.winfo_children():
            w.destroy()
        rows = self.index.docs()
        self.selected = {i: v for i, v in self.selected.items() if i in {r["id"] for r in rows}}
        for r in rows:
            f = ctk.CTkFrame(self.listbox, fg_color=SURFACE, corner_radius=8)
            f.pack(fill="x", pady=2)
            name = r["name"] if len(r["name"]) < 26 else r["name"][:25] + "…"
            var = self.selected.setdefault(r["id"], ctk.BooleanVar(value=True))
            ctk.CTkCheckBox(f, text=name, variable=var, command=self._update_scope, checkbox_width=18, checkbox_height=18,
                            fg_color=ACCENT, hover_color=ACCENT, border_color=BORDER, text_color=TEXT,
                            font=font(12, "bold")).pack(anchor="w", padx=10, pady=(6, 0))
            warn = f" · {r['empty_pages']} págs. sin texto" if r["empty_pages"] else ""
            label(f, f"{r['pages']} págs. · {r['chunks']} fragmentos{warn}", 11, muted=True).pack(anchor="w", padx=36, pady=(0, 6))
            ctk.CTkButton(f, text="×", width=22, height=22, fg_color="transparent", hover_color=BORDER, text_color=MUTED,
                          command=lambda i=r["id"]: self._remove(i)).place(relx=1.0, x=-6, y=4, anchor="ne")
        self._update_scope()
        self.emb_lbl.configure(text="Comprobando el modelo de búsqueda…", text_color=MUTED)
        threading.Thread(target=self._check_embeddings, daemon=True).start()     # Ollama puede tardar: no bloquea la ventana

    def _selected_ids(self) -> list[int]:
        return [i for i, v in self.selected.items() if v.get()]

    def _update_scope(self):
        total, chosen = len(self.selected), len(self._selected_ids())
        self.scope_lbl.configure(text="" if not total else
                                 ("Se pregunta a todos los documentos." if chosen == total else
                                  f"Se pregunta solo a {chosen} de {total} documentos."))

    def _check_embeddings(self):
        names = self.engine.installed()
        self.q.put(("emb", EMBED_MODEL in names or f"{EMBED_MODEL}:latest" in names))

    def _show_embeddings(self, have: bool):
        self.emb_lbl.configure(text=(f"Búsqueda por sentido: activa ({EMBED_MODEL})." if have else
                                     f"Solo búsqueda por palabras. Para buscar también por sentido instala «{EMBED_MODEL}» en Ajustes."),
                               text_color=OK if have else WARN)

    def _remove(self, doc_id: int):
        if self.working:
            return
        if messagebox.askyesno("Quitar del índice", "¿Quitar este documento del índice? (el PDF no se borra)"):
            self.index.remove(doc_id)
            self._refresh()

    def _add_files(self):
        paths = filedialog.askopenfilenames(title="PDFs a indexar", filetypes=[("PDF", "*.pdf")])
        if paths:
            self._start_index([Path(p) for p in paths])

    def _add_folder(self):
        d = filedialog.askdirectory(title="Carpeta con PDFs")
        if d:
            pdfs = sorted(Path(d).rglob("*.pdf"))[:200]
            if not pdfs:
                messagebox.showinfo("Sin PDFs", "No hay PDFs en esa carpeta.")
                return
            self._start_index(pdfs)

    def _start_index(self, paths: list[Path]):
        if self.working or not self.app.begin_job("Indexar documentos"):
            return
        self.working = True
        self.mode = "index"
        self.cancel.clear()
        self.status.configure(text="Indexando…")
        threading.Thread(target=self._index_worker, args=(paths,), daemon=True).start()

    def _index_worker(self, paths: list[Path]):
        try:
            self._index_all(paths)
        finally:
            self.app.end_job("Indexar documentos")

    def _index_all(self, paths: list[Path]):
        done = skipped = failed = 0
        try:
            try:
                self.app.manager.use_llm(EMBED_MODEL)
            except Exception as exc:  # noqa: BLE001
                log.warning("No se pudo preparar la VRAM: %s", exc)
            for n, p in enumerate(paths, 1):
                try:
                    def report(f, s, n=n, p=p):
                        self.q.put(("prog", (n - 1 + f) / len(paths), f"[{n}/{len(paths)}] {p.name}: {s}"))
                    r = self.index.add_pdf(p, report, self.cancel)
                    skipped += r.already
                    done += not r.already
                except Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    self.q.put(("log", f"✖ {p.name}: {exc}"))
            self.q.put(("index_done", f"Listo: {done} indexado(s), {skipped} ya estaban, {failed} con error."))
        except Cancelled:
            self.q.put(("index_done", "Cancelado."))

    # ------------------------------------------------------------ preguntar
    def _ask(self):
        if self.working:
            if self.mode == "ask":
                self.cancel.set()
            else:
                self.status.configure(text="Espera a que termine el indexado para preguntar.")
            return
        q = self.question.get("1.0", "end").strip()
        if not q:
            return
        if not self.index.docs():
            messagebox.showinfo("Sin documentos", "Primero indexa algún PDF.")
            return
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("Ningún documento marcado", "Marca al menos un documento de la lista para preguntarle.")
            return
        scope = None if len(ids) == len(self.selected) else ids          # None = todos
        if not self.app.begin_job("Preguntar a documentos"):
            return
        self.working = True
        self.mode = "ask"
        self.cancel.clear()
        self.ask_btn.configure(text="Detener")
        self.status.configure(text="Buscando en tus documentos…")
        self.prog.configure(mode="indeterminate")
        self.prog.start()
        threading.Thread(target=self._ask_worker, args=(q, self.model_var.get(), scope), daemon=True).start()

    def _ask_worker(self, question: str, model: str, doc_ids: list[int] | None = None):
        try:
            self._answer(question, model, doc_ids)
        finally:
            self.app.end_job("Preguntar a documentos")

    def _answer(self, question: str, model: str, doc_ids: list[int] | None):
        try:
            self.app.manager.use_llm(model)
            hits = self.index.search(question, k=6, doc_ids=doc_ids)
            if not hits:
                self.q.put(("answer", f"{ask.NOT_FOUND}\n\n_No hay fragmentos que se parezcan a la pregunta._"))
                return
            self.q.put(("status", f"Pensando con {model} sobre {len(hits)} fragmentos…"))
            reply = self.engine.stream(model, ask.build_messages(question, hits), Params(0.2, 0.9, 700, 8192), self.cancel)
            checked = ask.check_answer(reply.text, hits)
            src = ask.sources_markdown(hits, checked.cited)
            quotes = "\n".join(f"> **[{i}] p. {h.page}:** {h.text.strip()[:220].replace(chr(10), ' ')}…" for i, h in enumerate(hits, 1)
                               if i in checked.cited)
            self.q.put(("answer", f"{checked.text}\n\n{src}\n\n{quotes}"))
        except Cancelled:
            self.q.put(("answer", "_Cancelado._"))
        except Exception as exc:
            log.exception("Pregunta a documentos")
            self.q.put(("answer", f"**Error:** {type(exc).__name__}: {exc}\n\n¿Está Ollama en marcha?"))

    # ----------------------------------------------------------------- cola
    def _poll(self):
        try:
            for _ in range(40):
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "prog":
                    self.prog.configure(mode="determinate")
                    self.prog.set(msg[1])
                    self.status.configure(text=msg[2][:90])
                elif kind == "status":
                    self.status.configure(text=msg[1])
                elif kind == "log":
                    self.status.configure(text=msg[1][:120])
                elif kind == "emb":
                    self._show_embeddings(msg[1])
                elif kind == "index_done":
                    self.working = False
                    self.mode = None
                    self.prog.set(1)
                    self.status.configure(text=msg[1])
                    self._refresh()
                elif kind == "answer":
                    self.working = False
                    self.mode = None
                    self.prog.stop()
                    self.prog.configure(mode="determinate")
                    self.prog.set(0)
                    self.ask_btn.configure(text="Preguntar")
                    self.status.configure(text="")
                    self.view.show(msg[1])
        except queue.Empty:
            pass
        self.after(120, self._poll)
