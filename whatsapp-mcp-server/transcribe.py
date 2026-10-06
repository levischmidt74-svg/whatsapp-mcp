"""Local speech-to-text for WhatsApp voice messages.

Uses faster-whisper, an optional dependency (`uv sync --extra transcribe`).
Audio never leaves the machine; the Whisper model is downloaded once from
Hugging Face on first use and cached.
"""

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

TRANSCRIBE_MODEL = os.getenv("WHATSAPP_TRANSCRIBE_MODEL", "base")
TRANSCRIBE_DEVICE = os.getenv("WHATSAPP_TRANSCRIBE_DEVICE", "auto")
TRANSCRIBE_COMPUTE_TYPE = os.getenv("WHATSAPP_TRANSCRIBE_COMPUTE_TYPE", "int8")

INSTALL_HINT = (
    "Voice message transcription needs the optional 'transcribe' extra. "
    "Install it with `uv sync --extra transcribe` in whatsapp-mcp-server/ and add "
    '"--extra", "transcribe" before "run" in your MCP client config args.'
)

_model = None
_model_lock = threading.Lock()


class TranscriptionUnavailableError(RuntimeError):
    """Raised when the transcription backend is not installed."""


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise TranscriptionUnavailableError(INSTALL_HINT) from e
            logger.info("Loading Whisper model %r on %s", TRANSCRIBE_MODEL, TRANSCRIBE_DEVICE)
            _model = WhisperModel(TRANSCRIBE_MODEL, device=TRANSCRIBE_DEVICE, compute_type=TRANSCRIBE_COMPUTE_TYPE)
        return _model


def transcribe_audio(file_path: str, language: str | None = None) -> dict[str, Any]:
    """Transcribe an audio file (WhatsApp voice notes are Opus .ogg).

    Args:
        file_path: Path to the audio file
        language: Optional ISO 639-1 code (e.g. "en", "de"); auto-detected when None

    Returns:
        A dict with the transcript text, detected language and duration in seconds.

    Raises:
        FileNotFoundError: If the file does not exist
        TranscriptionUnavailableError: If faster-whisper is not installed
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    model = _get_model()
    segments, info = model.transcribe(file_path, language=language, vad_filter=True)
    # segments is a lazy generator; joining it runs the actual decoding.
    text = " ".join(segment.text.strip() for segment in segments).strip()

    return {
        "text": text,
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "duration_seconds": round(info.duration, 1),
    }
