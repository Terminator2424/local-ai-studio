import threading
import customtkinter as ctk
from tkinter import filedialog
from pathlib import Path
from datetime import datetime

from ..audio.transcriber import AudioTranscriber
from ..chat.engine import ChatEngine, Params

class AudioPage(ctk.CTkFrame):
    def __init__(self, master, app, **kwargs):
        super().__init__(master, **kwargs)
        self.app = app
        
        # Rutas base
        self.project_root = Path(__file__).resolve().parent.parent.parent
        model_path = self.project_root / "models" / "faster-whisper-small"
        self.saves_dir = self.project_root / "transcripciones"
        self.saves_dir.mkdir(exist_ok=True)
        
        self.transcriber = AudioTranscriber(model_size=str(model_path), device="cuda")
        self.engine = ChatEngine(self.app.cfg) # Motor de Ollama para resumir
        
        self._cancel_event = threading.Event()
        self._current_file = None

        # UI
        self.title = ctk.CTkLabel(self, text="Transcribir Audio", font=("Inter", 24, "bold"))
        self.title.pack(pady=20)

        self.btn_load = ctk.CTkButton(
            self, text="Seleccionar Archivo de Audio/Video", command=self.load_file,
            fg_color="#E53935", hover_color="#C62828"
        )
        self.btn_load.pack(pady=10)

        self.lbl_file = ctk.CTkLabel(self, text="Ningún archivo seleccionado")
        self.lbl_file.pack(pady=5)

        self.btn_transcribe = ctk.CTkButton(
            self, text="Transcribir", command=self.start_transcription, state="disabled",
            fg_color="#E53935", hover_color="#C62828"
        )
        self.btn_transcribe.pack(pady=10)

        self.lbl_status = ctk.CTkLabel(self, text="")
        self.lbl_status.pack(pady=5)

        self.txt_result = ctk.CTkTextbox(self, width=600, height=300)
        self.txt_result.pack(pady=10, padx=20, fill="both", expand=True)

        # Frame para botones de acción
        self.actions_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.actions_frame.pack(pady=(0, 20))

        self.btn_summarize = ctk.CTkButton(
            self.actions_frame, text="✨ Resumir con IA", command=self.summarize_text,
            fg_color="#7C3AED", hover_color="#5B21B6", state="disabled"
        )
        self.btn_summarize.pack(side="left", padx=10)

        self.btn_copy = ctk.CTkButton(
            self.actions_frame, text="Copiar Transcripción", command=self.copy_to_clipboard,
            fg_color="#E53935", hover_color="#C62828", state="disabled"
        )
        self.btn_copy.pack(side="left", padx=10)

        self.btn_save = ctk.CTkButton(
            self.actions_frame, text="Guardar .txt", command=self.save_to_file,
            fg_color="#E53935", hover_color="#C62828", state="disabled"
        )
        self.btn_save.pack(side="left", padx=10)

    def load_file(self):
        filetypes = [("Audio/Video", "*.mp3 *.wav *.mp4 *.m4a *.mkv")]
        filepath = filedialog.askopenfilename(title="Selecciona un archivo", filetypes=filetypes)
        if filepath:
            self._current_file = filepath
            self.lbl_file.configure(text=Path(filepath).name)
            self.btn_transcribe.configure(state="normal")
            self.txt_result.delete("1.0", "end")
            self.btn_copy.configure(state="disabled")
            self.btn_save.configure(state="disabled")
            self.btn_summarize.configure(state="disabled")

    def start_transcription(self):
        if not self._current_file:
            return
            
        # SEGURIDAD: Bloqueo de GPU (evita que se procese audio y PDF al mismo tiempo)
        if not self.app.begin_job("Transcripción de Audio"):
            return
            
        self.txt_result.delete("1.0", "end")
        self.btn_transcribe.configure(state="disabled")
        self.btn_load.configure(state="disabled")
        self.btn_copy.configure(state="disabled")
        self.btn_save.configure(state="disabled")
        self.btn_summarize.configure(state="disabled")
        self.lbl_status.configure(text="Cargando modelo y transcribiendo...", text_color=("gray10", "gray90"))
        self._cancel_event.clear()

        def update_ui(text_chunk):
            self.after(0, lambda: self._append_text(text_chunk))

        def run():
            try:
                result = self.transcriber.transcribe(self._current_file, self._cancel_event, on_text=update_ui)
                self.after(0, self.show_result, result)
            except Exception as e:
                self.after(0, self.show_error, str(e))
            finally:
                # SEGURIDAD: Liberamos el bloqueo de la GPU al terminar o fallar
                self.app.end_job("Transcripción de Audio")

        threading.Thread(target=run, daemon=True).start()

    def _append_text(self, text):
        self.txt_result.insert("end", text)
        self.txt_result.see("end")

    def show_result(self, result):
        self.btn_transcribe.configure(state="normal")
        self.btn_load.configure(state="normal")
        self.btn_copy.configure(state="normal")
        self.btn_save.configure(state="normal")
        self.btn_summarize.configure(state="normal")

        base_name = Path(self._current_file).stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        auto_filename = f"{base_name}_{timestamp}.txt"
        
        with open(self.saves_dir / auto_filename, "w", encoding="utf-8") as f:
            f.write(result.text)
        
        self.lbl_status.configure(text=f"Completado y guardado. Idioma: {result.language}", text_color="green")

    def show_error(self, error_msg):
        self.lbl_status.configure(text=f"Error: {error_msg}", text_color="red")
        self.btn_transcribe.configure(state="normal")
        self.btn_load.configure(state="normal")

    def copy_to_clipboard(self):
        text = self.txt_result.get("1.0", "end-1c")
        if text.strip():
            self.clipboard_clear()
            self.clipboard_append(text)
            self.lbl_status.configure(text="¡Texto copiado al portapapeles!", text_color="green")
            self.after(2000, lambda: self.lbl_status.configure(text="Completado.", text_color="green"))

    def save_to_file(self):
        text = self.txt_result.get("1.0", "end-1c")
        if not text.strip():
            return
            
        filepath = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Archivo de texto", "*.txt"), ("Todos los archivos", "*.*")],
            initialfile=f"transcripcion_{datetime.now().strftime('%Y%m%d')}.txt"
        )
        if filepath:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            self.lbl_status.configure(text="Archivo guardado con éxito.", text_color="green")

    def summarize_text(self):
        full_text = self.txt_result.get("1.0", "end-1c")
        if len(full_text) < 50:
            self.lbl_status.configure(text="El texto es demasiado corto para resumir.", text_color="orange")
            return

        # SEGURIDAD: Bloqueo de GPU
        if not self.app.begin_job("Resumen de texto"):
            return

        self.btn_summarize.configure(state="disabled", text="Resumiendo...")
        self.lbl_status.configure(text="La IA está procesando el resumen...", text_color="gray")

        def run_summary():
            try:
                # PROMPT ÉTICO Y ESTÁNDAR
                system_prompt = (
                    "Eres un asistente experto en síntesis. Tu tarea es leer la transcripción "
                    "proporcionada y devolver un resumen ejecutivo con viñetas de los puntos más importantes. "
                    "Debes ser objetivo y neutral."
                )
                user_prompt = f"Aquí está la transcripción:\n\n{full_text}"

                # Usamos Qwen 2.5 que es más objetivo para textos académicos/técnicos que Llama 3
                model = "qwen2.5:7b" 
                params = Params(temperature=0.3)
                
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]

                reply = self.engine.stream(model, messages, params)
                
                self.after(0, self._show_summary_window, reply.text)
                
            except Exception as e:
                self.after(0, lambda: self.lbl_status.configure(text=f"Error al resumir: {e}", text_color="red"))
            finally:
                self.after(0, lambda: self.btn_summarize.configure(state="normal", text="✨ Resumir con IA"))
                self.app.end_job("Resumen de texto")

        threading.Thread(target=run_summary, daemon=True).start()

    def _show_summary_window(self, summary_text):
        self.lbl_status.configure(text="¡Resumen generado!", text_color="green")
        
        win = ctk.CTkToplevel(self)
        win.title("Resumen Ejecutivo")
        win.geometry("600x500")
        win.attributes("-topmost", True)

        txt = ctk.CTkTextbox(win, width=560, height=400, font=("Inter", 14))
        txt.pack(pady=20, padx=20, fill="both", expand=True)
        txt.insert("1.0", summary_text)
        txt.configure(state="disabled")

        def copy_summary():
            win.clipboard_clear()
            win.clipboard_append(summary_text)
            btn_copy.configure(text="¡Copiado!", fg_color="#10B981")

        btn_copy = ctk.CTkButton(win, text="Copiar Resumen", command=copy_summary, fg_color="#E53935", hover_color="#C62828")
        btn_copy.pack(pady=(0, 20))