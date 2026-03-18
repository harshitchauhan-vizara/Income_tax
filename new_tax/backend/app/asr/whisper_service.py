"""
WhisperService — faster-whisper based ASR for TaxBot.

Optimisations in this version:
────────────────────────────────
1. FASTER PARTIAL TRANSCRIPTION:
   - _MIN_PARTIAL_BYTES reduced 16 000 → 8 000 (~0.25 s of audio) so partials
     fire sooner after the user starts speaking.
   - Partial model uses beam_size=1, best_of=1, temperature=0.0 (unchanged) but
     now also sets condition_on_previous_text=False and no_speech_threshold=0.5
     (slightly looser than final) so it returns text earlier.
   - VAD min_silence_duration_ms lowered 400 → 200 ms for partials — lets the
     model commit to words faster without waiting for a long silence.
   - speech_pad_ms lowered 200 → 100 ms for partials — less padding = less
     audio the model has to process per partial run.

2. FIXED asyncio.run() INSIDE EXECUTOR BUG (main.py companion fix):
   transcribe_partial is now a plain sync-compatible coroutine; main.py calls
   it via loop.run_in_executor with a sync wrapper, not nested asyncio.run().

3. CONFIG FIELD NAME FIX (kept from previous version):
   reads `whisper_model_size` correctly.

4. HALLUCINATION DETECTION (kept from previous version).

5. STRONGER VAD for final transcription (kept from previous version):
   threshold 0.45, no_speech_threshold 0.6, compression_ratio_threshold 2.4.
"""

import asyncio
import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Tuple

import numpy as np

from ..config import Settings

logger = logging.getLogger("app.asr.whisper_service")

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper")

# ---------------------------------------------------------------------------
# Domain-specific initial prompts
# ---------------------------------------------------------------------------
_PROMPT_EN = (
    "Income tax India, ITR filing, TDS deduction, TCS, PAN card, Aadhaar linking, "
    "Form 16, Form 26AS, AIS, Annual Information Statement, "
    "Section 80C, Section 80D, Section 87A, Section 10, Section 24, "
    "HRA exemption, LTA, house rent allowance, leave travel allowance, "
    "LTCG, STCG, long term capital gains, short term capital gains, "
    "NRI, HUF, Hindu Undivided Family, EPF, PPF, NPS, ELSS, "
    "capital gains tax, income tax return, new tax regime, old tax regime, "
    "standard deduction, tax deduction, tax exemption, surcharge, education cess, "
    "advance tax, self assessment tax, refund, salary income, "
    "seven lakh, ten lakh, fifteen lakh, fifty lakh, one crore, "
    "tax slab, ITR-1, ITR-2, ITR-3, ITR-4, e-filing, e-verify, "
    "how much tax, tax payable, tax calculation, rebate, "
    "investment, mutual fund, fixed deposit, home loan, insurance premium."
)
_PROMPT_HI = (
    "आयकर भारत, आईटीआर फाइलिंग, टीडीएस कटौती, पैन कार्ड, आधार लिंकिंग, "
    "फॉर्म 16, फॉर्म 26AS, धारा 80C, धारा 80D, धारा 87A, "
    "एचआरए, एलटीए, एलटीसीजी, एसटीसीजी, एनआरआई, "
    "ईपीएफ, पीपीएफ, एनपीएस, ईएलएसएस, पूंजीगत लाभ, "
    "कर रिटर्न, नई कर व्यवस्था, पुरानी कर व्यवस्था, "
    "मानक कटौती, कर कटौती, अधिभार, उपकर, "
    "अग्रिम कर, वापसी, वेतन, सात लाख, दस लाख, पचास लाख, एक करोड़, "
    "कर स्लैब, ई-फाइलिंग, कितना टैक्स, टैक्स छूट, निवेश।"
)
_PROMPTS = {"en": _PROMPT_EN, "hi": _PROMPT_HI, "auto": _PROMPT_EN}

# Minimum audio bytes before attempting partial transcription.
# ~0.25 s at 16 kHz mono int16 = 8 000 bytes  (was 16 000 = 0.5 s)
_MIN_PARTIAL_BYTES = 8_000


def _is_hallucination(text: str) -> bool:
    """
    Detect Whisper hallucination loops before returning a transcript.

    Two patterns caught:
      1. Token repetition: "ौRSA, KURRENT, PM, ौRSA, KURRENT, PM, ..."
         Split on commas — if any token repeats >= 3 times it's a loop.
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


def _webm_to_pcm(audio_bytes: bytes) -> np.ndarray | None:
    """
    Convert WebM/Opus bytes (from MediaRecorder) to a 16 kHz mono float32 PCM
    numpy array that faster-whisper can process directly.

    Why this is needed:
      MediaRecorder.start(250) streams 250ms WebM chunks. Each chunk after the
      first lacks the WebM EBML header, so PyAV cannot open it as a standalone
      file (InvalidDataError: '<none>'). We concatenate all chunks into one
      complete WebM container on the backend (via audio_buffers), but we still
      need to decode the WebM to raw PCM before passing to Whisper.

    Returns None on any decode error so callers can skip gracefully.
    """
    try:
        import av  # pylint: disable=import-outside-toplevel
        buf = io.BytesIO(audio_bytes)
        frames = []
        with av.open(buf, mode="r", metadata_errors="ignore") as container:
            for stream in container.streams:
                if stream.type == "audio":
                    # Resample to 16 kHz mono float32 — what Whisper expects
                    resampler = av.AudioResampler(
                        format="fltp",      # float32 planar
                        layout="mono",
                        rate=16000,
                    )
                    for packet in container.demux(stream):
                        for frame in packet.decode():
                            for rf in resampler.resample(frame):
                                frames.append(rf.to_ndarray()[0])
                    break
        if not frames:
            return None
        return np.concatenate(frames)
    except Exception:  # pylint: disable=broad-except
        return None


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
        Always 'small' regardless of config — partials need to be ~300 ms on CPU.
        Falls back to None silently if it can't load.
        """
        if self._partial_model is not None:
            return self._partial_model
        try:
            from faster_whisper import WhisperModel  # pylint: disable=import-outside-toplevel
            device = getattr(self.settings, "whisper_device", "cpu")
            logger.info("Loading Whisper partial model=tiny device=%s", device)
            self._partial_model = WhisperModel("tiny", device=device, compute_type="int8")
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

        # For Indian English audio, explicitly setting lang_code="en" outperforms
        # "auto" because Whisper's language detector can misidentify accented
        # English as Hindi or another language, causing garbled output.
        if language == "auto":
            lang_code = "en"   # default to English; Whisper will still handle Hindi correctly
        else:
            lang_code = language.lower()
        initial_prompt = _PROMPTS.get(language, _PROMPT_EN)

        try:
            transcript, detected_lang, confidence = await asyncio.get_event_loop().run_in_executor(
                _EXECUTOR,
                lambda: self._transcribe_sync(model, audio_bytes, lang_code, initial_prompt),
            )

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

    def transcribe_partial_sync(
        self,
        audio_bytes: bytes,
        language: str = "auto",
    ) -> str:
        """
        Synchronous fast best-effort transcription on audio captured so far.
        Uses the small model — ~200–300 ms on CPU.

        Called from main.py via run_in_executor (NOT via asyncio.run inside
        an executor, which was the previous bug causing nested-event-loop errors).

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
            transcript, _, confidence = self._transcribe_partial_sync(
                model, audio_bytes, lang_code, initial_prompt
            )
            if confidence < 0.20 or _is_hallucination(transcript):
                return ""
            return transcript
        except Exception:  # pylint: disable=broad-except
            return ""

    # kept for backward-compat if anything else calls it
    async def transcribe_partial(
        self,
        audio_bytes: bytes,
        language: str = "auto",
    ) -> str:
        """Async wrapper — delegates to the sync version via executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _EXECUTOR,
            lambda: self.transcribe_partial_sync(audio_bytes, language),
        )

    # ── private ───────────────────────────────────────────────────────────

    def _transcribe_sync(
        self,
        model,
        audio_bytes: bytes,
        language: str | None,
        initial_prompt: str,
    ) -> Tuple[str, str, float]:
        """Synchronous final-quality transcription — runs in executor thread."""
        # Try to decode WebM/Opus → PCM first (MediaRecorder sends WebM).
        # Fall back to passing raw bytes if conversion fails (WAV/PCM input).
        pcm = _webm_to_pcm(audio_bytes)
        audio_input = pcm if pcm is not None else io.BytesIO(audio_bytes)

        segments, info = model.transcribe(
            audio_input,
            language=language,
            initial_prompt=initial_prompt,
            beam_size=1,
            best_of=1,
            temperature=0.0,
            vad_filter=True,
            vad_parameters={
                "threshold":               0.45,
                "min_silence_duration_ms": 400,
                "min_speech_duration_ms":  200,
                "speech_pad_ms":           200,
            },
            word_timestamps=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-0.8,
            compression_ratio_threshold=2.4,
        )

        texts         = [seg.text for seg in segments]
        transcript    = " ".join(texts).strip()
        detected_lang = info.language or (language or "en")
        confidence    = float(info.language_probability or 0.0)

        return transcript, detected_lang, confidence

    def _transcribe_partial_sync(
        self,
        model,
        audio_bytes: bytes,
        language: str | None,
        initial_prompt: str,
    ) -> Tuple[str, str, float]:
        """
        Synchronous partial transcription — optimised for speed over accuracy.

        Key differences from _transcribe_sync:
          • VAD min_silence_duration_ms: 400 → 200 ms  (commits faster)
          • VAD speech_pad_ms: 200 → 100 ms            (less padding to process)
          • no_speech_threshold: 0.6 → 0.5             (slightly more permissive)
          • condition_on_previous_text=False            (no context overhead)
        """
        # Decode WebM → PCM (same as final transcription)
        pcm = _webm_to_pcm(audio_bytes)
        audio_input = pcm if pcm is not None else io.BytesIO(audio_bytes)

        segments, info = model.transcribe(
            audio_input,
            language=language,
            initial_prompt=initial_prompt,
            beam_size=1,
            best_of=1,
            temperature=0.0,
            vad_filter=True,
            vad_parameters={
                "threshold":               0.45,
                # Faster commit — don't wait 400 ms of silence before deciding
                "min_silence_duration_ms": 200,
                "min_speech_duration_ms":  150,
                # Less padding = fewer audio samples to process
                "speech_pad_ms":           100,
            },
            word_timestamps=False,
            condition_on_previous_text=False,
            # Slightly more permissive for partials so speech is detected sooner
            no_speech_threshold=0.5,
            log_prob_threshold=-0.8,
            compression_ratio_threshold=2.4,
        )

        texts         = [seg.text for seg in segments]
        transcript    = " ".join(texts).strip()
        detected_lang = info.language or (language or "en")
        confidence    = float(info.language_probability or 0.0)

        return transcript, detected_lang, confidence