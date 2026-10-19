import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from faster_whisper import WhisperModel

log = logging.getLogger(__name__)

@dataclass
class TranscribeResult:
    text: str
    language: str
    duration: float

class AudioTranscriber:
    def __init__(self, model_size: str = "small", device: str = "cuda"):
        self.model_size = model_size
        self.device = device
        self.compute_type = "float16" if device == "cuda" else "int8"
        self._model = None

    def load_model(self):
        """Carga el modelo en memoria (solo la primera vez)."""
        if self._model is None:
            log.info(f"Cargando modelo Whisper {self.model_size} en {self.device}...")
            self._model = WhisperModel(
                self.model_size, 
                device=self.device, 
                compute_type=self.compute_type,
                local_files_only=True
            )
        return self._model

    def transcribe(self, audio_path: str | Path, cancel_event: threading.Event = None, on_text=None) -> TranscribeResult:
        """Transcribe un archivo de audio y devuelve el texto."""
        model = self.load_model()
        
        segments, info = model.transcribe(
            str(audio_path), 
            beam_size=5,
            language=None  # Auto-detectar idioma
        )

        text_parts = []
        for segment in segments:
            if cancel_event and cancel_event.is_set():
                log.warning("Transcripción cancelada por el usuario.")
                break
            
            text_parts.append(segment.text)
            
            # Mandamos el texto a la interfaz en tiempo real si nos pasan la función
            if on_text:
                on_text(segment.text + "\n")

        return TranscribeResult(
            text=" ".join(text_parts).strip(),
            language=info.language,
            duration=info.duration
        )