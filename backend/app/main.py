import asyncio
import base64
import json
import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .asr.whisper_service import WhisperService
from .config import Settings, get_settings
from .llm.llm_service import LLMService
from .rag.rag_pipeline import RAGPipeline
from .rag.retriever import RetrieverService
from .rag.vectorstore import VectorStoreService
from .tts.sarvam_service import SarvamTTSService
from .utils.language_detector import detect_supported_language
from .websocket_manager import WebSocketManager


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    )


settings: Settings = get_settings()
setup_logging()
logger = logging.getLogger("app.main")

# Per-session timestamps for throttling live partial transcription
_partial_times: dict[str, float] = {}


def sanitize_assistant_output(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"analysis<\|message\|>[\s\S]*?final<\|message\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^final<\|message\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^analysis<\|message\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|channel\|>analysis[\s\S]*?<\|end\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|start\|>assistant", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|end\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|channel\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"^\s*[-*]\s+", "- ", cleaned, flags=re.M)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def sanitize_stream_token(token: str) -> str:
    cleaned = token or ""
    cleaned = re.sub(r"analysis<\|message\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"final<\|message\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|start\|>assistant", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|end\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|channel\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|message\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\banalysis\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^\s*analysis\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bfinal\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|[^|>]*$", "", cleaned)
    cleaned = re.sub(r"^[^<]*\|>", "", cleaned)
    cleaned = re.sub(r"<\|[^>]*\|?>", "", cleaned)
    return cleaned.strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting application")
    vectorstore = VectorStoreService(settings)
    await vectorstore.ensure_index()

    retriever        = RetrieverService(settings, vectorstore)
    llm_service      = LLMService(settings)
    rag_pipeline     = RAGPipeline(settings, retriever, llm_service)
    whisper_service  = WhisperService(settings)
    sarvam_tts       = SarvamTTSService(settings)
    websocket_manager = WebSocketManager(settings)

    app.state.vectorstore       = vectorstore
    app.state.retriever         = retriever
    app.state.llm_service       = llm_service
    app.state.rag_pipeline      = rag_pipeline
    app.state.whisper_service   = whisper_service
    app.state.sarvam_tts        = sarvam_tts
    app.state.websocket_manager = websocket_manager

    await whisper_service.warm_up()

    yield

    logger.info("Shutting down application")


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/health/asr")
async def health_asr() -> dict:
    whisper_service: WhisperService = app.state.whisper_service
    return {"status": "ok", "asr": whisper_service.get_status()}


@app.get("/health/tts")
async def health_tts() -> dict:
    sarvam_tts: SarvamTTSService = app.state.sarvam_tts
    return {
        "status": "ok",
        "provider": "sarvam",
        "sarvam_enabled": sarvam_tts.enabled,
        "sarvam_model": settings.sarvam_model,
        "sarvam_speaker": settings.sarvam_speaker,
        "sarvam_language_code": settings.sarvam_language_code,
        "sarvam_sample_rate": settings.sarvam_target_sample_rate,
        "sarvam_api_key_configured": bool((settings.sarvam_api_key or "").strip()),
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    manager:         WebSocketManager = app.state.websocket_manager
    whisper_service: WhisperService   = app.state.whisper_service
    rag_pipeline:    RAGPipeline      = app.state.rag_pipeline
    sarvam_tts:      SarvamTTSService = app.state.sarvam_tts

    await manager.connect(websocket)
    session_id = manager.get_session_id(websocket)
    await manager.send_json(websocket, {"type": "session_updated", "session_id": session_id})
    await manager.send_json(
        websocket,
        {
            "type": "tts_provider",
            "provider": "sarvam" if sarvam_tts.enabled else "unavailable",
        },
    )
    logger.info("WebSocket connected session=%s", session_id)

    try:
        while True:
            message = await websocket.receive()
            if not manager.allow_request(session_id):
                await manager.send_json(
                    websocket,
                    {"type": "error", "message": "Rate limit exceeded. Please slow down."},
                )
                continue

            if message.get("bytes") is not None:
                chunk = message["bytes"]
                manager.append_audio_chunk(session_id, chunk)

                # Live partial STT — show the user what they are saying while speaking.
                # Throttled to once per second per session to avoid CPU overload.
                now = time.monotonic()
                if now - _partial_times.get(session_id, 0.0) >= 1.0:
                    _partial_times[session_id] = now
                    current_audio = bytes(manager.audio_buffers.get(session_id, bytearray()))
                    partial_text = await whisper_service.transcribe_partial(current_audio, "auto")
                    await manager.send_json(
                        websocket, {"type": "partial_transcript", "text": partial_text}
                    )
                else:
                    await manager.send_json(websocket, {"type": "partial_transcript", "text": ""})
                continue

            text_data = message.get("text")
            if text_data is None:
                continue

            try:
                payload = json.loads(text_data)
            except json.JSONDecodeError:
                await manager.send_json(websocket, {"type": "error", "message": "Invalid JSON payload."})
                continue

            msg_type = payload.get("type", "text")

            if msg_type == "ping":
                await manager.send_json(websocket, {"type": "pong"})
                continue

            if msg_type == "audio_end":
                preferred_language = payload.get("language", "auto")
                raw_audio = manager.pop_audio_buffer(session_id)
                if not raw_audio:
                    await manager.send_json(websocket, {"type": "error", "message": "No audio received."})
                    continue

                # Tell frontend: audio received, now transcribing — show spinner/indicator
                await manager.send_json(websocket, {"type": "transcribing"})

                transcript, detected_lang, confidence = await whisper_service.transcribe(raw_audio, preferred_language)
                if confidence < settings.whisper_confidence_threshold:
                    await manager.send_json(
                        websocket,
                        {
                            "type": "asr_error",
                            "message": "Could you please repeat your question clearly?",
                            "language": preferred_language if preferred_language in {"en", "hi"} else "en",
                        },
                    )
                    continue
                if not transcript.strip():
                    asr_status = whisper_service.get_status()
                    if not asr_status.get("available"):
                        await manager.send_json(
                            websocket,
                            {
                                "type": "asr_error",
                                "message": "ASR service is unavailable. Install and configure faster-whisper.",
                                "details": asr_status.get("last_error", ""),
                            },
                        )
                    else:
                        await manager.send_json(
                            websocket,
                            {
                                "type": "asr_error",
                                "message": "Unable to transcribe audio. Please speak clearly and try again.",
                                "details": asr_status.get("last_error", ""),
                            },
                        )
                    continue

                from .llm.llm_service import detect_language as _detect_lang_asr
                script_lang_asr = _detect_lang_asr(transcript)
                has_latin_asr   = bool(re.search(r'[a-zA-Z]', transcript))
                if script_lang_asr == "hi":
                    lang = "hi"
                elif has_latin_asr:
                    lang = "en"
                else:
                    lang = detect_supported_language(transcript, fallback="en")
                    if lang not in {"en", "hi"}:
                        lang = "en"

                # FIX Bug 1: Send user_transcript IMMEDIATELY after transcription.
                # This makes the user's question appear in the UI before the LLM
                # starts generating — previously it was shown only after handle_query
                # returned, which is after TTS completes (many seconds too late).
                await manager.send_json(
                    websocket,
                    {
                        "type": "user_transcript",
                        "session_id": session_id,
                        "text": transcript,
                        "language": lang,
                    },
                )

                # Signal frontend to open/prepare the assistant response box
                # before the first LLM token arrives.
                await manager.send_json(websocket, {"type": "assistant_start"})

                await handle_query(
                    websocket, manager, rag_pipeline, session_id,
                    transcript, lang,
                    enable_voice=True,
                    streaming_tts=False,
                )
                continue

            if msg_type == "new_session":
                rag_pipeline.reset_session(session_id)
                _partial_times.pop(session_id, None)
                session_id = manager.reset_session(websocket)
                await manager.send_json(
                    websocket,
                    {
                        "type": "session_updated",
                        "session_id": session_id,
                        "message": "New session started.",
                    },
                )
                continue

            if msg_type == "clear_session":
                rag_pipeline.reset_session(session_id)
                await manager.send_json(
                    websocket,
                    {
                        "type": "session_cleared",
                        "session_id": session_id,
                        "message": "Session cleared.",
                    },
                )
                continue

            if msg_type == "text":
                user_text = payload.get("text", "").strip()
                if not user_text:
                    await manager.send_json(websocket, {"type": "error", "message": "Empty text message."})
                    continue

                preferred_language = payload.get("language", "auto")
                from .llm.llm_service import detect_language as _detect_lang

                script_lang = _detect_lang(user_text)
                has_latin = bool(re.search(r'[a-zA-Z]', user_text))

                if script_lang == "hi":
                    lang = "hi"
                elif has_latin:
                    lang = "en"
                else:
                    lang = detect_supported_language(user_text, fallback="en")
                    if lang not in {"en", "hi"}:
                        lang = "en"

                enable_voice = payload.get("enableVoice", False)

                await manager.send_json(
                    websocket,
                    {
                        "type": "user_transcript",
                        "session_id": session_id,
                        "text": user_text,
                        "language": lang,
                    },
                )
                # Signal frontend to open/prepare the assistant response box immediately
                await manager.send_json(websocket, {"type": "assistant_start"})

                await handle_query(
                    websocket, manager, rag_pipeline, session_id,
                    user_text, lang,
                    enable_voice=enable_voice,
                    streaming_tts=enable_voice,
                )
                continue

            if msg_type == "tts_request":
                tts_text     = payload.get("text", "").strip()
                tts_language = payload.get("language", "en").lower()

                if tts_text and sarvam_tts.enabled:
                    try:
                        audio_bytes = await sarvam_tts.synthesize_sentence(tts_text, tts_language)
                        if audio_bytes:
                            await manager.send_json(
                                websocket,
                                {
                                    "type":         "tts_chunk",
                                    "audio_base64": base64.b64encode(audio_bytes).decode("utf-8"),
                                    "mime":         "audio/wav",
                                    "provider":     "sarvam",
                                    "streaming":    True,
                                },
                            )
                            logger.info(
                                "Streaming TTS chunk sent session=%s lang=%s len=%d bytes=%d",
                                session_id, tts_language, len(tts_text), len(audio_bytes),
                            )
                    except Exception as exc:  # pylint: disable=broad-except
                        logger.warning("Streaming TTS chunk failed session=%s error=%s", session_id, exc)
                continue

            await manager.send_json(
                websocket, {"type": "error", "message": f"Unsupported message type: {msg_type}"}
            )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected session=%s", session_id)
        _partial_times.pop(session_id, None)
        manager.disconnect(websocket)
    except RuntimeError as exc:
        if "disconnect message" in str(exc):
            logger.info("WebSocket already disconnected session=%s", session_id)
            manager.disconnect(websocket)
        else:
            logger.exception("WebSocket runtime error session=%s error=%s", session_id, exc)
            manager.disconnect(websocket)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("WebSocket error session=%s error=%s", session_id, exc)
        manager.disconnect(websocket)


def _split_into_tts_sentences(text: str, min_len: int = 80) -> list[str]:
    """
    Split text into sentence batches suitable for Sarvam TTS.
    Short sentences are merged until min_len is reached to avoid
    choppy audio from Sarvam's per-request latency overhead.
    """
    raw = re.split(r'(?<=[.!?।])\s+', text.strip())
    batches: list[str] = []
    current = ""
    for sentence in raw:
        s = sentence.strip()
        if not s:
            continue
        current = (current + " " + s).strip() if current else s
        if len(current) >= min_len:
            batches.append(current)
            current = ""
    if current:
        batches.append(current)
    return batches


def _extract_clean_answer(raw: str) -> str:
    """
    Strip the LLM chain-of-thought meta block from the full raw output.
    """
    text = raw or ""

    if "final<|message|>" in text:
        _, _, after = text.partition("final<|message|>")
        return after.strip()

    m = re.search(r"final\s*<\|message\|>(.*)", text, re.I | re.S)
    if m:
        return m.group(1).strip()

    if "<|end|>" in text:
        _, _, after = text.partition("<|end|>")
        return after.strip()

    cleaned = text
    cleaned = re.sub(r"analysis<\|message\|>[\s\S]*?final<\|message\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|channel\|>analysis[\s\S]*?<\|end\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|start\|>assistant", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|end\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|channel\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|message\|>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<\|[^>]*\|?>", "", cleaned)
    cleaned = re.sub(r"^\s*(channel\s*)?(analysis\s*)+", "", cleaned, flags=re.I)
    return cleaned.strip()


def _sanitize_for_tts(text: str) -> str:
    """
    Convert all non-speakable content to natural spoken words for Sarvam TTS.

      Step 1  URLs           https://incometax.gov.in  → "for more details visit incometax dot gov dot in"
      Step 2  % ranges       (5%‑30%)                  → "between 5 percent to 30 percent"
      Step 3  ₹ lakh ranges  (₹3‑7 L)                  → "between 3 laakh to 7 laakh"
      Step 4  ₹ amounts      ₹14,25,000                → "rupees 14 laakh 25 thousand"
      Step 5  Slab lists     0‑5‑10‑15‑20‑30%          → "zero, five, ..., thirty percent"
      Step 6  Lone dashes    remaining ‑                → " to "
      Step 7  Section IDs    87A, 80C                  → "87 A", "80 C"
      Step 8  Markdown       **bold**                  → "bold"
      Step 9  Whitespace     normalise

    Note: "laakh" is used throughout (not "lakh") because Sarvam pronounces
    "laakh" correctly as the Indian word, while "lakh" can sound like "lack".
    """
    _ONES = ["zero","one","two","three","four","five","six","seven","eight","nine",
             "ten","eleven","twelve","thirteen","fourteen","fifteen","sixteen",
             "seventeen","eighteen","nineteen"]
    _TENS = ["","","twenty","thirty","forty","fifty","sixty","seventy","eighty","ninety"]
    _DASH = r'[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\-]'

    def _nw(n: int) -> str:
        if n < 20:  return _ONES[n]
        if n < 100: return _TENS[n//10] + ("" if n%10==0 else " "+_ONES[n%10])
        return str(n)

    def _indian_amount(raw: str) -> str:
        """14,25,000 → "14 laakh twenty five thousand" """
        try:
            n = int(raw.replace(",", ""))
        except ValueError:
            return raw
        if n == 0: return "zero"
        parts = []
        cr = n // 10_000_000; n %= 10_000_000
        lk = n // 100_000;    n %= 100_000
        th = n // 1_000;      n %= 1_000
        hu = n // 100;        n %= 100
        if cr: parts.append(f"{cr} crore")
        if lk: parts.append(f"{lk} laakh")
        if th: parts.append(f"{_nw(th)} thousand")
        if hu: parts.append(f"{_nw(hu)} hundred")
        if n:  parts.append(_nw(n))
        return " ".join(parts)

    def _expand_pct_range(m):
        """(5%‑30%) → "between 5 percent to 30 percent" """
        return f"between {m.group(1)} percent to {m.group(2)} percent"

    def _expand_lakh_range(m):
        """(₹3‑7 L) → "between 3 laakh to 7 laakh" """
        a, b = m.group(1), m.group(2)
        sfx  = (m.group(3) or "").strip()
        if sfx in ("L","l","lakh"):    return f"between {a} laakh to {b} laakh"
        if sfx in ("Cr","cr","crore"): return f"between {a} crore to {b} crore"
        return f"between {a} to {b}"

    def _expand_slab(m):
        """0‑5‑10‑15‑20‑30% → "zero, five, ..., thirty percent" """
        nums  = re.split(_DASH, m.group(1))
        words = [_nw(int(n)) for n in nums if n.isdigit()]
        if not words: return m.group(0)
        if len(words) == 1: return words[0] + " percent"
        return ", ".join(words[:-1]) + ", and " + words[-1] + " percent"

    def _url_speech(m):
        domain = re.sub(r'https?://(www\.)?', '', m.group(0)).rstrip('/').split('/')[0]
        return "for more details visit " + domain.replace('.', ' dot ')

    t = text or ""

    # Step 1: URLs
    t = re.sub(r'\b(?:visit|see|check|go to|refer to)\s+https?://\S+',
               lambda m: _url_speech(re.search(r'https?://\S+', m.group(0))), t)
    t = re.sub(r'https?://\S+', _url_speech, t)

    # Step 2: Percentage ranges  (5%‑30%)  →  "between 5 percent to 30 percent"
    t = re.sub(r'(\d[\d.]*)\s*%\s*' + _DASH + r'\s*(\d[\d.]*)\s*%', _expand_pct_range, t)

    # Step 3: ₹ lakh/crore ranges  (₹3‑7 L)  →  "between 3 laakh to 7 laakh"
    t = re.sub(r'₹\s*(\d[\d,]*)\s*' + _DASH + r'\s*(\d[\d.,]*)\s*(L|Cr|lakh|crore)?',
               _expand_lakh_range, t)

    # Step 4: Plain ₹ amounts  →  spoken Indian numbers
    t = re.sub(r'₹\s*(\d[\d,]*)', lambda m: "rupees " + _indian_amount(m.group(1)), t)

    # Step 5: Slab lists  0‑5‑10‑15‑20‑30%
    t = re.sub(r'(\d+(?:' + _DASH + r'\d+){2,})%', _expand_slab, t)

    # Step 6: Remaining digit‑digit  →  "X to Y"
    t = re.sub(r'(\d)\s*' + _DASH + r'\s*(\d)', r'\1 to \2', t)

    # Step 7: Any leftover unicode dashes  →  comma pause
    for ch in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212":
        t = t.replace(ch, ", ")

    # Step 8: Strip markdown bold/italic
    t = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", t)
    t = re.sub(r"_{1,2}([^_]+)_{1,2}", r"\1", t)

    # Step 9: Section codes  87A → "87 A"
    t = re.sub(r'\b(\d+)([A-Z]{1,3})\b', r'\1 \2', t)

    # Step 10: Whitespace
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n+", " ", t)
    return t.strip()


def _safe_append(buf: str, new_text: str) -> str:
    """
    Append new_text to buf ensuring a word-boundary space where needed.
    Fixes "taxif" (token "tax" + token "if" with no space between them)
    and "lakh.New" (punctuation end + letter start).
    """
    if buf and new_text:
        last  = buf[-1]
        first = new_text[0]
        needs_space = (
            (last.isalnum() and first.isalnum()) or
            (last in ".!?," and first.isalpha() and not new_text.startswith(" "))
        )
        if needs_space:
            return buf + " " + new_text
    return buf + new_text


async def handle_query(
    websocket: WebSocket,
    manager: WebSocketManager,
    rag_pipeline: RAGPipeline,
    session_id: str,
    query: str,
    language: str,
    enable_voice: bool = False,
    streaming_tts: bool = False,
) -> None:
    """
    Concurrent pipeline — text streaming and TTS run at the same time.
    TTS receives only sanitized-for-speech text (numbers expanded, symbols
    stripped, URLs removed) to prevent letter-by-letter hallucination.
    """
    sarvam_tts: SarvamTTSService = getattr(websocket.app.state, "sarvam_tts")

    do_tts = enable_voice and sarvam_tts.enabled

    # Raised from 120 → 200: fewer, larger TTS batches = fewer API calls = faster audio
    MIN_BATCH_CHARS = 250
    SENTENCE_END_RE = re.compile(r'(?<=[.!?।])\s+')

    # Max seconds to wait for a single out-of-order TTS batch before skipping it
    MAX_WAIT_S = 8.0

    tts_queue: asyncio.Queue = asyncio.Queue()

    # ── Track A: stream tokens live to frontend AND push TTS batches ────────
    async def _stream_tokens() -> str:
        raw_chunks:       list[str] = []
        post_marker_raw:  list[str] = []
        final_marker_seen = False
        tts_buf           = ""
        batch_idx         = 0

        def _push_batch(text: str) -> None:
            nonlocal batch_idx
            # Sanitize for speech BEFORE queuing — removes URLs, fixes symbols,
            # strips anything that makes Sarvam spell letter-by-letter
            cleaned = _sanitize_for_tts(text)
            if cleaned:
                tts_queue.put_nowait((batch_idx, cleaned))
                batch_idx += 1

        def _maybe_flush_tts(new_text: str) -> None:
            nonlocal tts_buf
            # Use _safe_append to prevent token-joining bugs like "taxif"
            tts_buf = _safe_append(tts_buf, new_text)
            if len(tts_buf) >= MIN_BATCH_CHARS:
                parts = SENTENCE_END_RE.split(tts_buf)
                if len(parts) > 1:
                    # Only flush complete sentences — never cut mid-word
                    _push_batch(" ".join(parts[:-1]))
                    tts_buf = parts[-1]
                # If no sentence boundary yet, keep accumulating.
                # Do NOT split at word boundary — that causes letter-by-letter
                # artifacts when a word is cut across two TTS batches.

        # Stream tokens live — with strict pre/post marker separation.
        #
        # CRITICAL RULES (fixes Bug 2 + Bug 3):
        #   1. Pre-marker tokens (LLM chain-of-thought / analysis zone) are
        #      NEVER sent to the frontend and NEVER fed to TTS.
        #      Sending them caused: garbage text in the response box, and TTS
        #      speaking the LLM's internal reasoning instead of the answer.
        #   2. TTS only receives clean post-marker tokens — the actual answer.
        #   3. The assistant_token stream only starts after the final marker is
        #      confirmed, so the response box stays empty until real content arrives.
        #
        # If the LLM never emits a final marker (Format C — no meta block),
        # the entire output is treated as the answer and streamed normally.
        async for token in rag_pipeline.answer_stream(
            query=query, session_id=session_id, language_hint=language
        ):
            if not token:
                continue

            raw_chunks.append(token)
            full_so_far = "".join(raw_chunks)

            if not final_marker_seen and "final<|message|>" in full_so_far:
                # Marker just arrived — extract only the content after it
                final_marker_seen = True
                _, _, after = full_so_far.partition("final<|message|>")
                if after:
                    post_marker_raw.append(after)
                    clean_tok = sanitize_stream_token(after)
                    if clean_tok:
                        # Now safe to stream to frontend and TTS
                        await manager.send_json(websocket, {"type": "assistant_token", "token": clean_tok + " "})
                        if do_tts:
                            _maybe_flush_tts(clean_tok + " ")
                # Pre-marker tokens are silently discarded — never sent anywhere

            elif final_marker_seen:
                # Post-marker: clean and stream everything
                post_marker_raw.append(token)
                clean_tok = sanitize_stream_token(token)
                if clean_tok:
                    await manager.send_json(websocket, {"type": "assistant_token", "token": clean_tok})
                    if do_tts:
                        _maybe_flush_tts(clean_tok)

            # else: still in pre-marker zone — DO NOTHING. Do not stream, do not
            # feed TTS. The analysis block is internal LLM reasoning only.

        # Build the final clean answer for assistant_final message.
        # Format C (no marker at all): the whole output is the answer.
        # In this case final_marker_seen is False but raw_chunks has everything.
        # We need to handle streaming for Format C too — if no marker was ever
        # seen, the tokens above were all silently dropped. Replay them now as
        # the clean answer stream.
        if not final_marker_seen:
            answer_text = sanitize_assistant_output(
                _extract_clean_answer("".join(raw_chunks))
            )
            # Stream clean answer to frontend word-by-word (Format C — no marker)
            words = answer_text.split()
            for i, word in enumerate(words):
                tok = word + (" " if i < len(words) - 1 else "")
                await manager.send_json(websocket, {"type": "assistant_token", "token": tok})
            # For TTS in Format C: push whole answer as sentence-split batches,
            # not word-by-word, so we never cut mid-word.
            if do_tts:
                for sentence in SENTENCE_END_RE.split(_sanitize_for_tts(answer_text)):
                    s = sentence.strip()
                    if s:
                        _push_batch(s)
        else:
            answer_text = sanitize_assistant_output("".join(post_marker_raw))

        if not answer_text:
            answer_text = (
                "I do not have that information at the moment. "
                "Please visit https://www.incometax.gov.in or call 1800-103-0025."
            )

        # Flush remaining TTS buffer
        if do_tts and tts_buf.strip():
            _push_batch(tts_buf)

        # Signal Track B: no more batches
        tts_queue.put_nowait(None)
        return answer_text

    # ── Track B: synthesise all batches concurrently, send in order ─────────
    async def _run_tts() -> None:
        if not do_tts:
            while await tts_queue.get() is not None:
                pass
            return

        async def _synth_one(idx: int, text: str) -> tuple:
            try:
                audio = await sarvam_tts.synthesize_sentence(text, language)
                logger.info("TTS batch=%d chars=%d bytes=%d",
                            idx, len(text), len(audio) if audio else 0)
                return idx, audio
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("TTS batch=%d failed: %s", idx, exc)
                return idx, None

        # Drain queue, launching synthesis tasks immediately as batches arrive
        pending: dict[int, asyncio.Task] = {}
        while True:
            item = await tts_queue.get()
            if item is None:
                break
            idx, text = item
            pending[idx] = asyncio.create_task(_synth_one(idx, text))

        if not pending:
            return

        # ── FIX: send each chunk as soon as it finishes (not blocked by order) ──
        # We maintain a done buffer and always send the lowest unsent index
        # once it's available. This means:
        #   - If batch 0 finishes before batch 1, send batch 0 immediately.
        #   - If batch 1 finishes before batch 0, buffer it and wait for batch 0.
        # We never wait more than MAX_WAIT_S for any single batch.
        done_buf: dict[int, bytes | None] = {}
        next_send = 0
        total = len(pending)

        for coro in asyncio.as_completed(list(pending.values())):
            result_idx, audio_bytes = await coro
            done_buf[result_idx] = audio_bytes

            # Send all consecutive completed batches in order
            while next_send in done_buf:
                audio = done_buf.pop(next_send)
                if audio:
                    await manager.send_json(
                        websocket,
                        {
                            "type":         "tts_chunk",
                            "mime":         "audio/wav",
                            "provider":     "sarvam",
                            "audio_base64": base64.b64encode(audio).decode("utf-8"),
                        },
                    )
                next_send += 1

        # Send any remaining buffered chunks in order (handles stragglers)
        while next_send < total:
            audio = done_buf.get(next_send)
            if audio:
                await manager.send_json(
                    websocket,
                    {
                        "type":         "tts_chunk",
                        "mime":         "audio/wav",
                        "provider":     "sarvam",
                        "audio_base64": base64.b64encode(audio).decode("utf-8"),
                    },
                )
            next_send += 1

    # ── Run both tracks concurrently ───────────────────────────────────────
    stream_task = asyncio.create_task(_stream_tokens())
    tts_task    = asyncio.create_task(_run_tts())

    results = await asyncio.gather(stream_task, tts_task, return_exceptions=True)
    answer_text = results[0] if isinstance(results[0], str) else ""

    if not answer_text:
        answer_text = (
            "I do not have that information at the moment. "
            "Please visit https://www.incometax.gov.in or call 1800-103-0025."
        )

    source = rag_pipeline.get_last_source(session_id)
    await manager.send_json(
        websocket,
        {
            "type":       "assistant_final",
            "session_id": session_id,
            "text":       answer_text,
            "language":   language,
            "source":     source,
        },
    )
    await manager.send_json(websocket, {"type": "tts_end"})

    if enable_voice and not sarvam_tts.enabled:
        await manager.send_json(
            websocket,
            {
                "type":    "error",
                "message": (
                    "Audio unavailable. Sarvam TTS failed or is not configured. "
                    "Please verify SARVAM_API_KEY in your .env file."
                ),
            },
        )