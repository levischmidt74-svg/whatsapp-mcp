"""Tests for media download and voice message transcription."""

import sys
import types

import pytest
import requests

import main as mcp_main
import transcribe
import whatsapp


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestDownloadMedia:
    def test_success_returns_path(self, monkeypatch):
        monkeypatch.setattr(
            whatsapp.requests,
            "post",
            lambda *a, **kw: FakeResponse(200, {"success": True, "message": "ok", "path": "/store/a.ogg"}),
        )
        assert whatsapp.download_media("MSG", "chat@s.whatsapp.net") == ("/store/a.ogg", "ok")

    def test_bridge_error_message_is_surfaced(self, monkeypatch):
        monkeypatch.setattr(
            whatsapp.requests,
            "post",
            lambda *a, **kw: FakeResponse(
                500, {"success": False, "message": "Failed to download media: incomplete media information"}
            ),
        )
        path, message = whatsapp.download_media("MSG", "chat@s.whatsapp.net")
        assert path is None
        assert "incomplete media information" in message

    def test_non_json_response(self, monkeypatch):
        monkeypatch.setattr(
            whatsapp.requests, "post", lambda *a, **kw: FakeResponse(400, None, "Invalid request format\n")
        )
        path, message = whatsapp.download_media("MSG", "chat@s.whatsapp.net")
        assert path is None
        assert message == "Bridge returned HTTP 400: Invalid request format"

    def test_bridge_unreachable(self, monkeypatch):
        def boom(*a, **kw):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(whatsapp.requests, "post", boom)
        path, message = whatsapp.download_media("MSG", "chat@s.whatsapp.net")
        assert path is None
        assert "Could not reach the WhatsApp bridge" in message

    def test_does_not_write_to_stdout(self, monkeypatch, capsys):
        # stdout is the MCP JSON-RPC channel; any stray output corrupts it.
        monkeypatch.setattr(
            whatsapp.requests, "post", lambda *a, **kw: FakeResponse(200, {"success": True, "path": "/x.ogg"})
        )
        whatsapp.download_media("MSG", "chat@s.whatsapp.net")
        monkeypatch.setattr(whatsapp.requests, "post", lambda *a, **kw: FakeResponse(500, {"message": "nope"}))
        whatsapp.download_media("MSG", "chat@s.whatsapp.net")
        assert capsys.readouterr().out == ""


@pytest.fixture
def fake_whisper(monkeypatch):
    """Install a fake faster_whisper module and reset the cached model."""
    calls = {}

    class FakeModel:
        def __init__(self, name, device, compute_type):
            calls["init"] = (name, device, compute_type)

        def transcribe(self, path, language=None, vad_filter=False):
            calls["transcribe"] = (path, language, vad_filter)
            segments = (types.SimpleNamespace(text=t) for t in [" Hey, it's me. ", " Call me back. "])
            info = types.SimpleNamespace(language="en", language_probability=0.98765, duration=4.26)
            return segments, info

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(transcribe, "_model", None)
    return calls


class TestTranscribeAudio:
    def test_transcribes_and_joins_segments(self, fake_whisper, tmp_path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"OggS")

        result = transcribe.transcribe_audio(str(audio), language="en")

        assert result == {
            "text": "Hey, it's me. Call me back.",
            "language": "en",
            "language_probability": 0.988,
            "duration_seconds": 4.3,
        }
        assert fake_whisper["transcribe"] == (str(audio), "en", True)

    def test_model_is_loaded_once(self, fake_whisper, tmp_path, monkeypatch):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"OggS")
        transcribe.transcribe_audio(str(audio))
        first = transcribe._model
        transcribe.transcribe_audio(str(audio))
        assert transcribe._model is first

    def test_missing_file(self, fake_whisper):
        with pytest.raises(FileNotFoundError):
            transcribe.transcribe_audio("/nonexistent/voice.ogg")

    def test_missing_dependency(self, monkeypatch, tmp_path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"OggS")
        monkeypatch.setitem(sys.modules, "faster_whisper", None)  # makes the import fail
        monkeypatch.setattr(transcribe, "_model", None)
        with pytest.raises(transcribe.TranscriptionUnavailableError, match="--extra"):
            transcribe.transcribe_audio(str(audio))


class TestTranscribeVoiceMessageTool:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(mcp_main, "whatsapp_download_media", lambda m, c: ("/store/a.ogg", "ok"))
        monkeypatch.setattr(
            mcp_main,
            "transcribe_audio",
            lambda path, language=None: {"text": "hello", "language": "en", "duration_seconds": 1.0},
        )
        result = mcp_main.transcribe_voice_message("MSG", "chat@s.whatsapp.net")
        assert result == {
            "success": True,
            "file_path": "/store/a.ogg",
            "text": "hello",
            "language": "en",
            "duration_seconds": 1.0,
        }

    def test_download_failure(self, monkeypatch):
        monkeypatch.setattr(mcp_main, "whatsapp_download_media", lambda m, c: (None, "not a media message"))
        result = mcp_main.transcribe_voice_message("MSG", "chat@s.whatsapp.net")
        assert result == {"success": False, "message": "Failed to download media: not a media message"}

    def test_backend_not_installed(self, monkeypatch):
        monkeypatch.setattr(mcp_main, "whatsapp_download_media", lambda m, c: ("/store/a.ogg", "ok"))

        def unavailable(path, language=None):
            raise transcribe.TranscriptionUnavailableError(transcribe.INSTALL_HINT)

        monkeypatch.setattr(mcp_main, "transcribe_audio", unavailable)
        result = mcp_main.transcribe_voice_message("MSG", "chat@s.whatsapp.net")
        assert result["success"] is False
        assert "uv sync --extra transcribe" in result["message"]
        assert result["file_path"] == "/store/a.ogg"

    def test_download_media_tool_surfaces_reason(self, monkeypatch):
        monkeypatch.setattr(mcp_main, "whatsapp_download_media", lambda m, c: (None, "client is not connected"))
        result = mcp_main.download_media("MSG", "chat@s.whatsapp.net")
        assert result == {"success": False, "message": "Failed to download media: client is not connected"}
