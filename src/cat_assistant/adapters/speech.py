from __future__ import annotations


class Utf8TestRecognizer:
    """Test adapter: treats UTF-8 bytes as a transcript; not an audio recognizer."""

    async def transcribe(self, audio: bytes) -> tuple[str, float]:
        return audio.decode("utf-8"), 1.0


class Utf8TestSynthesizer:
    """Test adapter: emits UTF-8 text bytes; replace with NVIDIA Riva TTS."""

    async def synthesize(self, text: str) -> bytes:
        return text.encode("utf-8")

