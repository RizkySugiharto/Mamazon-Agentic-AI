"""Unit tests for voice transcription via 9Router (Groq Whisper) and agent integration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.agent.graph import AgentGraph, DiscordAgentContext
from src.discord_bot import MamazonBot
from src.voice_transcriber import (
    TranscriptionConfigError,
    TranscriptionEmptyError,
    TranscriptionRateLimitError,
    VoiceDisabledError,
    VoiceTranscriber,
)


class TestVoiceTranscriber:
    def test_transcribe_bytes_success(self, monkeypatch):
        monkeypatch.setattr(config, "VOICE_DETECTION_ENABLED", True)
        transcriber = VoiceTranscriber(
            base_url="http://localhost:20128/v1",
            api_key="test-key",
            model="groq/whisper-large-v3-turbo",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"text": "Tolong cek denda di PP 07 Oktober"}

        with patch.object(transcriber.session, "post", return_value=mock_resp) as mock_post:
            result = transcriber.transcribe_bytes(b"dummy-audio-bytes", "test.ogg", "audio/ogg")

            assert result == "Tolong cek denda di PP 07 Oktober"
            mock_post.assert_called_once()
            called_kwargs = mock_post.call_args[1]
            assert called_kwargs["data"]["model"] == "groq/whisper-large-v3-turbo"
            assert "file" in called_kwargs["files"]

    def test_transcribe_missing_api_key(self, monkeypatch):
        monkeypatch.setattr(config, "VOICE_DETECTION_ENABLED", True)
        transcriber = VoiceTranscriber(api_key="")
        with pytest.raises(TranscriptionConfigError):
            transcriber.transcribe_bytes(b"some-bytes")

    def test_transcribe_empty_audio(self, monkeypatch):
        monkeypatch.setattr(config, "VOICE_DETECTION_ENABLED", True)
        transcriber = VoiceTranscriber(api_key="valid-key")
        with pytest.raises(TranscriptionEmptyError):
            transcriber.transcribe_bytes(b"")

    def test_transcribe_disabled_voice(self, monkeypatch):
        monkeypatch.setattr(config, "VOICE_DETECTION_ENABLED", False)
        transcriber = VoiceTranscriber(api_key="valid-key")
        with pytest.raises(VoiceDisabledError):
            transcriber.transcribe_bytes(b"audio-data")

    def test_transcribe_rate_limit_retry(self, monkeypatch):
        monkeypatch.setattr(config, "VOICE_DETECTION_ENABLED", True)
        transcriber = VoiceTranscriber(api_key="valid-key")

        mock_429 = MagicMock()
        mock_429.status_code = 429
        mock_429.headers = {"Retry-After": "0.1"}

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {"text": "Pertanyaan setelah retry"}

        with patch.object(transcriber.session, "post", side_effect=[mock_429, mock_200]):
            with patch("time.sleep", return_value=None):
                result = transcriber.transcribe_bytes(b"audio-bytes")
                assert result == "Pertanyaan setelah retry"

    def test_transcribe_rate_limit_exhausted(self, monkeypatch):
        monkeypatch.setattr(config, "VOICE_DETECTION_ENABLED", True)
        transcriber = VoiceTranscriber(api_key="valid-key")

        mock_429 = MagicMock()
        mock_429.status_code = 429
        mock_429.headers = {"Retry-After": "0.1"}
        mock_429.text = "Rate limit reached"

        with patch.object(transcriber.session, "post", return_value=mock_429):
            with patch("time.sleep", return_value=None):
                with pytest.raises(TranscriptionRateLimitError):
                    transcriber.transcribe_bytes(b"audio-bytes")


class TestDiscordAudioDetection:
    def test_is_audio_attachment(self):
        att_ogg = SimpleNamespace(filename="voice-message.ogg", content_type="audio/ogg")
        att_mp3 = SimpleNamespace(filename="recording.mp3", content_type="application/octet-stream")
        att_m4a = SimpleNamespace(filename="audio.m4a", content_type="audio/mp4")
        att_png = SimpleNamespace(filename="screenshot.png", content_type="image/png")

        assert MamazonBot.is_audio_attachment(att_ogg) is True
        assert MamazonBot.is_audio_attachment(att_mp3) is True
        assert MamazonBot.is_audio_attachment(att_m4a) is True
        assert MamazonBot.is_audio_attachment(att_png) is False

    def test_get_audio_attachment(self):
        att_img = SimpleNamespace(filename="test.png", content_type="image/png")
        att_audio = SimpleNamespace(filename="note.ogg", content_type="audio/ogg")

        msg_audio = SimpleNamespace(flags=SimpleNamespace(voice=False), attachments=[att_img, att_audio])
        msg_voice_flag = SimpleNamespace(flags=SimpleNamespace(voice=True), attachments=[att_audio])
        msg_no_audio = SimpleNamespace(flags=SimpleNamespace(voice=False), attachments=[att_img])

        assert MamazonBot.get_audio_attachment(msg_audio) is att_audio
        assert MamazonBot.get_audio_attachment(msg_voice_flag) is att_audio
        assert MamazonBot.get_audio_attachment(msg_no_audio) is None


class TestAgentVoiceVisibility:
    def test_system_prompt_active_status(self):
        graph = AgentGraph(validator=SimpleNamespace(rulebase={}))
        state = {
            "question": "Bisa kirim voice note?",
            "voice_detection_enabled": True,
            "is_voice": False,
        }
        prompt = graph._system_prompt(state)
        assert "Status penerimaan voice note / pesan suara: AKTIF" in prompt
        assert "CATATAN MASUKAN SUARA" not in prompt

    def test_system_prompt_disabled_status(self):
        graph = AgentGraph(validator=SimpleNamespace(rulebase={}))
        state = {
            "question": "Bisa kirim voice note?",
            "voice_detection_enabled": False,
            "is_voice": False,
        }
        prompt = graph._system_prompt(state)
        assert "Status penerimaan voice note / pesan suara: NONAKTIF" in prompt

    def test_system_prompt_voice_input_guidance(self):
        graph = AgentGraph(validator=SimpleNamespace(rulebase={}))
        state = {
            "question": "Tolong cek buser 01",
            "voice_detection_enabled": True,
            "is_voice": True,
            "voice_transcript": "Tolong cek buser 01",
        }
        prompt = graph._system_prompt(state)
        assert "CATATAN MASUKAN SUARA (VOICE NOTE)" in prompt
        assert "Maklumi jika ada sedikit ketidaktepatan ejaan fonetis" in prompt

    def test_ai_provider_status_exposes_voice_config(self):
        status = config.ai_provider_status()
        assert "voice_detection_enabled" in status
        assert "whisper_model" in status
        assert status["whisper_model"] == config.NINEROUTER_WHISPER_MODEL
