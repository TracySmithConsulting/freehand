"""Speech to text, on this computer (Nov 2026).

Voice notes (Telegram) and the console's "Speak" button turn speech into
text here, with faster-whisper (an open Whisper model) running locally.
Nothing is sent to an outside service, and it costs nothing per message.

The model downloads once, the first time it is needed (about 150 MB for
"base"), into FREEHAND_VOICE_MODEL_DIR (compose keeps it in a volume).

Settings (``settings.json`` key ``voice``):
- ``model``: tiny | base | small | medium (default "base"). Bigger is more
  accurate and slower.
- ``language``: e.g. "en"; empty means detect it per note.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import Optional

log = logging.getLogger("freehand.voice")

MAX_AUDIO_BYTES = 25 * 1024 * 1024
_MODEL = None
_MODEL_KEY: Optional[tuple] = None
_LOCK = threading.Lock()


def _settings() -> dict:
    try:
        from core.agent_config import _load_settings
        v = _load_settings().get("voice") or {}
        return v if isinstance(v, dict) else {}
    except Exception:  # settings must never break voice
        return {}


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _model():
    global _MODEL, _MODEL_KEY
    from faster_whisper import WhisperModel
    size = str(_settings().get("model") or "base")
    root = os.environ.get("FREEHAND_VOICE_MODEL_DIR") or None
    key = (size, root)
    with _LOCK:
        if _MODEL is None or _MODEL_KEY != key:
            log.info("loading speech model %s", size)
            _MODEL = WhisperModel(size, device="cpu", compute_type="int8", download_root=root)
            _MODEL_KEY = key
        return _MODEL


def transcribe(audio: bytes, suffix: str = ".ogg") -> dict:
    """{"text", "language"} or {"error"} in plain words. Blocking: call it
    with asyncio.to_thread."""
    if not audio:
        return {"error": "The recording was empty."}
    if len(audio) > MAX_AUDIO_BYTES:
        return {"error": "That recording is too long. Keep voice messages under about 20 minutes."}
    if not available():
        return {"error": "Speech recognition isn't installed in this FreeHand."}
    language = str(_settings().get("language") or "").strip() or None
    path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix or ".ogg", delete=False) as f:
            f.write(audio)
            path = f.name
        segments, info = _model().transcribe(path, language=language, vad_filter=True, beam_size=1)
        text = " ".join(s.text.strip() for s in segments).strip()
    except Exception as e:  # a bad file or a failed model download must not crash a chat
        log.warning("transcription failed: %s", e)
        return {"error": "FreeHand couldn't make out that recording."}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    if not text:
        return {"error": "FreeHand didn't hear any words in that recording."}
    return {"text": text, "language": getattr(info, "language", "") or ""}
