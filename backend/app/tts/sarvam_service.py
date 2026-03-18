"""
Sarvam TTS Service — uses official sarvamai SDK v0.1.27+
pip install -U sarvamai

SDK signature (verified):
  client.text_to_speech.convert(
      text: str,                        ← single string, NOT a list
      target_language_code: str,
      speaker: str,
      model: "bulbul:v2" | "bulbul:v3",
      pitch, pace, loudness,
      speech_sample_rate: int,
      enable_preprocessing: bool,
      output_audio_codec: str,
  ) -> TextToSpeechResponse(audios: List[str])   ← base64 WAV strings

Voice parameter guide (bulbul:v2 only):
  pitch:    -0.1 → deep/flat  |  0.0 → neutral  |  +0.1 → warm/expressive
  pace:      0.5 → very slow  |  0.85 → natural  |  1.0 → normal  |  1.5 → fast
  loudness:  1.0 → soft       |  1.2 → clear     |  1.5 → loud

Note: bulbul:v3 ignores pitch and loudness — use bulbul:v2 for human-like control.

Speed optimisations (updated):
- Module-level ThreadPoolExecutor(_EXECUTOR) reused across calls — avoids
  thread-creation overhead on every synthesis request.
- synthesize_sentence is now a thin async wrapper; all blocking SDK work runs
  on _EXECUTOR so it never blocks the event loop.
- No logic changes — all parameters, language resolution, and speaker
  resolution are identical to the previous version.
"""

import asyncio
import base64
import logging
from concurrent.futures import ThreadPoolExecutor

from ..config import Settings

logger = logging.getLogger("app.tts.sarvam_service")

# Module-level executor shared across all TTS synthesis calls.
# max_workers=4 matches the typical number of concurrent TTS batches per response.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sarvam_tts")

_LANG_MAP: dict[str, str] = {
    "en": "en-IN",
    "hi": "hi-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "bn": "bn-IN",
    "gu": "gu-IN",
    "od": "od-IN",
    "pa": "pa-IN",
}

# Per-language speaker config attr names — maps language code → settings field
_SPEAKER_ATTR_MAP: dict[str, str] = {
    "en": "sarvam_speaker_en",
    "hi": "sarvam_speaker_hi",
    "ta": "sarvam_speaker_ta",
}


class SarvamTTSService:

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client  = None

    # ── public interface ────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool((self.settings.sarvam_api_key or "").strip())

    def _get_client(self):
        if self._client is None:
            try:
                from sarvamai import SarvamAI  # pylint: disable=import-outside-toplevel
                self._client = SarvamAI(api_subscription_key=self.settings.sarvam_api_key)
                logger.info("SarvamAI client initialised OK")
            except ImportError as exc:
                raise RuntimeError("sarvamai SDK not installed. Run: pip install -U sarvamai") from exc
        return self._client

    def _resolve_language_code(self, language: str) -> str:
        lang = (language or "en").lower().strip()
        if "-" in lang:
            return lang
        return _LANG_MAP.get(lang, self.settings.sarvam_language_code)

    def _resolve_speaker(self, language: str) -> str:
        """
        Pick the best speaker for the given language.
        Looks up sarvam_speaker_en / sarvam_speaker_hi / sarvam_speaker_ta from settings.
        Falls back to the global sarvam_speaker default if the per-language one is empty.
        """
        lang = (language or "en").lower().strip()
        attr = _SPEAKER_ATTR_MAP.get(lang)
        if attr:
            per_lang_speaker = (getattr(self.settings, attr, "") or "").strip()
            if per_lang_speaker:
                return per_lang_speaker
        return (self.settings.sarvam_speaker or "meera").strip()

    @staticmethod
    def _decode_audio(b64_audio: str) -> bytes:
        if not b64_audio:
            return b""
        if "," in b64_audio:                      # strip data-URI prefix if present
            b64_audio = b64_audio.split(",", 1)[1]
        return base64.b64decode(b64_audio)

    def _synthesize_sync(self, sentence: str, language: str) -> bytes:
        """
        Blocking SDK call — runs on _EXECUTOR, never on the event loop.
        Extracted from synthesize_sentence to keep async wrapper minimal.
        """
        language_code = self._resolve_language_code(language)
        speaker       = self._resolve_speaker(language)

        logger.info(
            "Sarvam TTS → lang=%s speaker=%s model=%s pace=%.2f pitch=%.2f loudness=%.2f len=%d",
            language_code, speaker, self.settings.sarvam_model,
            self.settings.sarvam_speech_rate, self.settings.sarvam_pitch,
            self.settings.sarvam_loudness, len(sentence),
        )

        client = self._get_client()

        params = {
            "text": sentence,
            "target_language_code": language_code,
            "speaker": speaker,
            "model": self.settings.sarvam_model,
            "pace": self.settings.sarvam_speech_rate,
            "speech_sample_rate": self.settings.sarvam_target_sample_rate,
            "enable_preprocessing": True,
            "output_audio_codec": "wav",
        }

        if self.settings.sarvam_model == "bulbul:v2":
            params["pitch"]    = self.settings.sarvam_pitch
            params["loudness"] = self.settings.sarvam_loudness

        response = client.text_to_speech.convert(**params)

        audios = response.audios if hasattr(response, "audios") else []
        if not audios:
            logger.error("Sarvam TTS: empty audios in response")
            return b""

        audio_bytes = self._decode_audio(audios[0])
        logger.info("Sarvam TTS OK — bytes=%d", len(audio_bytes))
        return audio_bytes

    async def synthesize_sentence(self, text: str, language: str = "en") -> bytes:
        sentence = (text or "").strip()
        if not sentence:
            return b""
        if not self.enabled:
            logger.warning("Sarvam TTS: SARVAM_API_KEY not set")
            return b""

        try:
            return await asyncio.get_event_loop().run_in_executor(
                _EXECUTOR,
                lambda: self._synthesize_sync(sentence, language),
            )
        except RuntimeError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Sarvam TTS FAILED — %s: %s", type(exc).__name__, exc)
            return b""

    @staticmethod
    def to_base64(audio_bytes: bytes) -> str:
        if not audio_bytes:
            return ""
        return base64.b64encode(audio_bytes).decode("utf-8")