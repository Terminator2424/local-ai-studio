import threading
import customtkinter as ctk
from tkinter import filedialog
from pathlib import Path

from ..audio.transcriber import AudioTranscriber

class AudioPage(ctk.CTkFrame):
    def __init__(self, master, app, **kwargs):
        super().__init__(master, **kwargs)
        self.app = app
        
        self.transcriber = AudioTranscriber(model_size=r"C:\Users\ediso\local-ai-studio\models\faster-whisper-small", device="cuda")
        self._cancel_event = threading.Event()
        self._current_file = None

        # UI
        self.title = ctk.CTkLabel(self, text="Transcribir Audio", font=("Inter", 24, "bold"))
        self.title.pack(pady=20)

        # Botón para cargar (Rojo)
        self.btn_load = ctk.CTkButton(
            self, text="Seleccionar Archivo de Audio/Video", command=self.load_file,
            fg_color="#E53935", hover_color="#C62828"
        )
        self.btn_load.pack(pady=10)

        self.lbl_file = ctk.CTkLabel(self, text="Ningún archivo seleccionado")
        self.lbl_file.pack(pady=5)

        # Botón para transcribir (Rojo)
        self.btn_transcribe = ctk.CTkButton(
            self, text="Transcribir", command=self.start_transcription, state="disabled",
            fg_color="#E53935", hover_color="#C62828"
        )
        self.btn_transcribe.pack(pady=10)

        # Estado
        self.lbl_status = ctk.CTkLabel(self, text="")
        self.lbl_status.pack(pady=5)

        # Caja de texto
        self.txt_result = ctk.CTkTextbox(self, width=600, height=300)
        self.txt_result.pack(pady=10, padx=20, fill="both", expand=True)

        # Botón para copiar (Rojo)
        self.btn_copy = ctk.CTkButton(
            self, text="Copiar Transcripción", command=self.copy_to_clipboard,
            fg_color="#E53935", hover_color="#C62828", state="disabled"
        )
        self.btn_copy.pack(pady=(0, 20))

    def load_file(self):
        filetypes = [("Audio/Video", "*.mp3 *.wav *.mp4 *.m4a *.mkv")]
        filepath = filedialog.askopenfilename(title="Selecciona un archivo", filetypes=filetypes)
        if filepath:
            self._current_file = filepath
            self.lbl_file.configure(text=Path(filepath).name)
            self.btn_transcribe.configure(state="normal")
            self.txt_result.delete("1.0", "end") # Limpia si cambias de archivo
            self.btn_copy.configure(state="disabled")

    def start_transcription(self):
        if not self._current_file:
            return
        
        # REINICIO TOTAL: Limpiamos la caja y los estados antes de empezar
        self.txt_result.delete("1.0", "end")
        self.btn_transcribe.configure(state="disabled")
        self.btn_load.configure(state="disabled")
        self.btn_copy.configure(state="disabled")
        self.lbl_status.configure(text="Cargando modelo y transcribiendo...", text_color=("gray10", "gray90"))
        self._cancel_event.clear()

        def update_ui(text_chunk):
            # Actualiza la caja de texto en tiempo real desde el hilo secundario
            self.after(0, lambda: self._append_text(text_chunk))

        def run():
            try:
                result = self.transcriber.transcribe(self._current_file, self._cancel_event, on_text=update_ui)
                self.after(0, self.show_result, result)
            except Exception as e:
                self.after(0, self.show_error, str(e))

        threading.Thread(target=run, daemon=True).start()

    def _append_text(self, text):
        """Añade texto y hace scroll automático hacia abajo."""
        self.txt_result.insert("end", text)
        self.txt_result.see("end")

    def show_result(self, result):
        self.lbl_status.configure(text=f"Completado. Idioma: {result.language} | Duración: {result.duration:.2f}s")
        self.btn_transcribe.configure(state="normal")
        self.btn_load.configure(state="normal")
        self.btn_copy.configure(state="normal") # Habilitamos el botón de copiar

    def show_error(self, error_msg):
        self.lbl_status.configure(text=f"Error: {error_msg}", text_color="red")
        self.btn_transcribe.configure(state="normal")
        self.btn_load.configure(state="normal")

    def copy_to_clipboard(self):
        """Copia todo el texto de la caja al portapapeles de Windows."""
        text = self.txt_result.get("1.0", "end-1c") # Lee todo el texto
        if text.strip():
            self.clipboard_clear()
            self.clipboard_append(text)
            self.lbl_status.configure(text="¡Texto copiado al portapapeles!", text_color="green")
            # Vuelve a poner el texto normal después de 2 segundos
            self.after(2000, lambda: self.lbl_status.configure(
                text="Completado.", text_color=("gray10", "gray90")
            ))