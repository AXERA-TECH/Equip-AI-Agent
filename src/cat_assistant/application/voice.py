from __future__ import annotations

from dataclasses import dataclass

from cat_assistant.application.loop import AgentLoop
from cat_assistant.domain.models import AssistantResponse, ResponseCategory, Utterance
from cat_assistant.domain.ports import SpeechRecognizerPort, SpeechSynthesizerPort


@dataclass(frozen=True, slots=True)
class VoiceReply:
    response: AssistantResponse
    audio: bytes


class VoiceGateway:
    """Riva-facing boundary. Replace the two speech ports without changing the loop."""

    def __init__(
        self,
        recognizer: SpeechRecognizerPort,
        synthesizer: SpeechSynthesizerPort,
        loop: AgentLoop,
        *,
        minimum_confidence: float = 0.65,
    ) -> None:
        self._recognizer = recognizer
        self._synthesizer = synthesizer
        self._loop = loop
        self._minimum_confidence = minimum_confidence

    async def handle_audio(
        self,
        audio: bytes,
        *,
        session_id: str,
        machine_id: str,
        operator_id: str | None = None,
    ) -> VoiceReply:
        text, confidence = await self._recognizer.transcribe(audio)
        if confidence < self._minimum_confidence or not text.strip():
            response = AssistantResponse(
                "现场噪声较大，我没有听清，请再说一次。",
                ResponseCategory.CLARIFICATION,
            )
        else:
            response = await self._loop.handle(
                Utterance(
                    text=text,
                    session_id=session_id,
                    machine_id=machine_id,
                    operator_id=operator_id,
                    confidence=confidence,
                )
            )
        return VoiceReply(response, await self._synthesizer.synthesize(response.text))
