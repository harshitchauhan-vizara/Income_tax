"""
WhisperService — faster-whisper based ASR for TaxBot.

Fixes in this version:
─────────────────────
1. CONFIG FIELD NAME FIX: reads `whisper_model_size` (matches config.py) instead
   of the wrong `whisper_model` field. Previously the model always silently fell
   back to "small" regardless of what was set in config — now correctly loads
   the configured model (e.g. large-v3).

2. HALLUCINATION DETECTION: Whisper on silence/noise with large models produces
   looping garbage like "ौRSA, KURRENT, PM, ौRSA, KURRENT..." with high apparent
   confidence. We detect this with two checks BEFORE returning the transcript:
     a. Repetition check — if any token repeats >= 3 times it's a hallucination loop
     b. Garbage character ratio — if >50% of words are isolated uppercase ASCII
        in otherwise non-ASCII text, reject it

3. LIVE PARTIAL TRANSCRIPTION: new `transcribe_partial()` method that runs on
   the audio buffer so far and returns a best-effort partial transcript while
   the user is still speaking. Uses the small model for speed (~300ms on CPU).
   Called from main.py on each audio chunk (throttled to every ~1s).

4. STRONGER VAD + no-speech threshold: raised no_speech_threshold 0.5→0.6
   and VAD threshold 0.35→0.45 to reject near-silence segments earlier.
   Lowered compression_ratio_threshold 2.8→2.4 to catch repetition loops sooner.
"""

import asyncio
import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Tuple

from ..config import Settings

logger = logging.getLogger("app.asr.whisper_service")

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper")

# ---------------------------------------------------------------------------
# Domain-specific initial prompts
# ---------------------------------------------------------------------------
_PROMPT_EN = (
    "Income tax, ITR, TDS, TCS, PAN card, Aadhaar, Form 16, Form 26AS, AIS, "
    "Section 80C, Section 80D, Section 87A, HRA, LTA, LTCG, STCG, NRI, HUF, "
    "EPF, PPF, NPS, ELSS, capital gains, tax return, new regime, old regime, "
    "deduction, exemption, surcharge, cess, advance tax, refund, salary, lakh, "
    "crore, rupees, tax slab, ITR-1, ITR-2, ITR-3, ITR-4, e-filing."
)
_PROMPT_HI = (
    "आयकर, ITR, TDS, PAN कार्ड, आधार, फॉर्म 16, धारा 80C, धारा 80D, "
    "HRA, LTCG, NRI, EPF, PPF, NPS, कर रिटर्न, नई कर व्यवस्था, कटौती, "
    "अग्रिम कर, वापसी, वेतन, लाख, करोड़, रुपये, कर स्लैब।"
)
_PROMPTS = {"en": _PROMPT_EN, "hi": _PROMPT_HI, "auto": _PROMPT_EN}

# Minimum audio bytes before attempting partial transcription
# ~0.5s at 16kHz mono int16 = 16000 bytes
_MIN_PARTIAL_BYTES = 16_000


def _is_hallucination(text: str) -> bool:
    """
    Detect Whisper hallucination loops before returning a transcript.

    Two patterns caught:
      1. Token repetition: "ौRSA, KURRENT, PM, ौRSA, KURRENT, PM, ..."
         Split on commas — if any token repeats >= 3 times, it's a loop.
      2. Garbage uppercase ratio: random ALL-CAPS ASCII tokens mixed with
         Devanagari characters (e.g. "ौRSA KURRENT PM ौRSA")
         If >50% of words are isolated uppercase ASCII, reject.
    """
    if not text:
        return False

    # Check 1: comma-separated repetition loop
    tokens = [t.strip() for t in re.split(r"[,،、]", text) if t.strip()]
    if len(tokens) >= 6:
        counts: dict[str, int] = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        if max(counts.values()) >= 3:
            logger.warning("Hallucination (repetition loop): %r", text[:80])
            return True

    # Check 2: high ratio of isolated uppercase ASCII words in non-ASCII text
    words = text.split()
    if len(words) >= 4:
        upper_ascii = sum(
            1 for w in words
            if w.isupper() and w.isascii() and len(w) >= 2
        )
        if upper_ascii / len(words) > 0.5:
            logger.warning("Hallucination (garbage uppercase): %r", text[:80])
            return True

    return False


class WhisperService:

    def __init__(self, settings: Settings) -> None:
        self.settings       = settings
        self._model         = None  # full model for final transcription
        self._partial_model = None  # small model for live partials
        self._status        = {"available": False, "model": None, "last_error": None}

    # ── public ────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return dict(self._status)

    async def warm_up(self) -> None:
        """Preload both models at startup so the first request is instant."""
        try:
            await asyncio.get_event_loop().run_in_executor(_EXECUTOR, self._load_model)
            await asyncio.get_event_loop().run_in_executor(_EXECUTOR, self._load_partial_model)
            logger.info("Whisper warm-up complete (full + partial models loaded)")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Whisper warm-up failed: %s", exc)

    def _load_model(self):
        """Full-quality model for final transcription after audio_end."""
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # pylint: disable=import-outside-toplevel

            # FIX 1: correct field name is whisper_model_SIZE (not whisper_model)
            model_size   = getattr(self.settings, "whisper_model_size",   "large-v3")
            device       = getattr(self.settings, "whisper_device",       "cpu")
            compute_type = getattr(self.settings, "whisper_compute_type", "int8")

            logger.info("Loading Whisper model=%s device=%s compute_type=%s",
                        model_size, device, compute_type)
            self._model = WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
                num_workers=2,
            )
            self._status = {"available": True, "model": model_size, "last_error": None}
            logger.info("Whisper model loaded OK: %s", model_size)
        except Exception as exc:  # pylint: disable=broad-except
            self._status = {"available": False, "model": None, "last_error": str(exc)}
            logger.exception("Whisper model load failed: %s", exc)
            raise
        return self._model

    def _load_partial_model(self):
        """
        Small fast model for live partial transcription while the user speaks.
        Always 'small' regardless of config — partials need to be ~300ms on CPU.
        Falls back to None silently if it can't load (partials just won't show).
        """
        if self._partial_model is not None:
            return self._partial_model
        try:
            from faster_whisper import WhisperModel  # pylint: disable=import-outside-toplevel
            device = getattr(self.settings, "whisper_device", "cpu")
            logger.info("Loading Whisper partial model=small device=%s", device)
            self._partial_model = WhisperModel("small", device=device, compute_type="int8")
            logger.info("Whisper partial model loaded OK")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Whisper partial model failed to load (live partials disabled): %s", exc)
            self._partial_model = None
        return self._partial_model

    # ── Final transcription ────────────────────────────────────────────────

    async def transcribe(
        self,
        audio_bytes: bytes,
        language: str = "auto",
    ) -> Tuple[str, str, float]:
        """
        Full-quality transcription on the complete audio buffer (called on audio_end).
        Returns: (transcript, detected_language, confidence)
        """
        if not audio_bytes:
            return "", language if language != "auto" else "en", 0.0

        try:
            model = await asyncio.get_event_loop().run_in_executor(
                _EXECUTOR, self._load_model
            )
        except Exception:
            return "", "en", 0.0

        lang_code      = language.lower() if language and language != "auto" else None
        initial_prompt = _PROMPTS.get(language, _PROMPT_EN)

        try:
            transcript, detected_lang, confidence = await asyncio.get_event_loop().run_in_executor(
                _EXECUTOR,
                lambda: self._transcribe_sync(model, audio_bytes, lang_code, initial_prompt),
            )

            # FIX 2: reject hallucinations before returning
            if _is_hallucination(transcript):
                logger.warning("Final transcript rejected as hallucination")
                return "", detected_lang, 0.0

            logger.info("Whisper transcribed lang=%s confidence=%.2f text=%r",
                        detected_lang, confidence, transcript[:80])
            return transcript, detected_lang, confidence

        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Whisper transcription failed: %s", exc)
            return "", "en", 0.0

    # ── Live partial transcription ─────────────────────────────────────────

    async def transcribe_partial(
        self,
        audio_bytes: bytes,
        language: str = "auto",
    ) -> str:
        """
        Fast best-effort transcription on audio captured so far.
        Uses the small model — ~300ms on CPU.
        Called from main.py on each audio chunk (throttled to every ~1s).
        Returns empty string if audio is too short, silent, or looks like garbage.
        """
        if not audio_bytes or len(audio_bytes) < _MIN_PARTIAL_BYTES:
            return ""

        model = self._partial_model
        if model is None:
            return ""

        lang_code      = language.lower() if language and language != "auto" else None
        initial_prompt = _PROMPTS.get(language, _PROMPT_EN)

        try:
            transcript, _, confidence = await asyncio.get_event_loop().run_in_executor(
                _EXECUTOR,
                lambda: self._transcribe_sync(model, audio_bytes, lang_code, initial_prompt),
            )
            if confidence < 0.4 or _is_hallucination(transcript):
                return ""
            return transcript
        except Exception:  # pylint: disable=broad-except
            return ""

    # ── private ───────────────────────────────────────────────────────────

    def _transcribe_sync(
        self,
        model,
        audio_bytes: bytes,
        language: str | None,
        initial_prompt: str,
    ) -> Tuple[str, str, float]:
        """Synchronous transcription — runs in executor thread."""
        audio_file = io.BytesIO(audio_bytes)

        segments, info = model.transcribe(
            audio_file,
            language=language,
            initial_prompt=initial_prompt,
            beam_size=1,
            best_of=1,
            temperature=0.0,
            vad_filter=True,
            vad_parameters={
                # FIX 4: raised 0.35→0.45 — rejects near-silence earlier
                "threshold":               0.45,
                "min_silence_duration_ms": 400,
                "min_speech_duration_ms":  200,
                "speech_pad_ms":           200,
            },
            word_timestamps=False,
            condition_on_previous_text=False,
            # FIX 4: raised 0.5→0.6 — more aggressive no-speech rejection
            no_speech_threshold=0.6,
            log_prob_threshold=-0.8,
            # FIX 4: lowered 2.8→2.4 — catches repetition loops sooner
            compression_ratio_threshold=2.4,
        )

        texts = [seg.text for seg in segments]
        transcript    = " ".join(texts).strip()
        detected_lang = info.language or (language or "en")
        confidence    = float(info.language_probability or 0.0)

        return transcript, detected_lang, confidence