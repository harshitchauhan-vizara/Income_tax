import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChatWebSocket } from "../services/websocket";
import { StreamingTTSBuffer } from "../utils/streamingTTS";

const STORAGE_UI_KEY = "cp_chat_ui_v2";

const getDefaultWsUrl = () => {
  if (typeof window === "undefined") return "ws://localhost:8111/ws";
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const host = window.location.hostname || "localhost";
  return `${protocol}://${host}:8111/ws`;
};

const WS_URL = import.meta.env.VITE_WS_URL || getDefaultWsUrl();

const safeLoad = (key, fallback) => {
  try {
    const val = JSON.parse(localStorage.getItem(key) || "null");
    return val ?? fallback;
  } catch { return fallback; }
};

const createId = () => (crypto?.randomUUID ? crypto.randomUUID() : `msg_${Date.now()}`);
const now = () => new Date().toISOString();

const makeMessage = (overrides = {}) => ({
  id: createId(), role: "bot", text: "", timestamp: now(), source: null, language: "EN",
  ...overrides,
});

const normalizeLang = (raw) => {
  const v = String(raw || "").toLowerCase().trim();
  if (v === "hi" || v === "hindi" || v.startsWith("hi-")) return "HI";
  if (v === "ta" || v === "tamil" || v.startsWith("ta-")) return "TA";
  return "EN";
};

const sanitizeText = (text = "") =>
  String(text)
    .replace(/analysis<\|message\|>[\s\S]*?final<\|message\|>/gi, "")
    .replace(/^analysis<\|message\|>/gi, "")
    .replace(/^final<\|message\|>/gi, "")
    .replace(/<\|channel\|>analysis[\s\S]*?<\|end\|>/gi, "")
    .replace(/<\|start\|>assistant/gi, "")
    .replace(/<\|end\|>/gi, "")
    .replace(/<\|channel\|>/gi, "")
    .trim();

const decodeBase64ToBlob = (base64, mime = "audio/wav") => {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mime });
};

// ── Web Speech API helpers ────────────────────────────────────────────────────
// Detects if the browser supports real-time speech recognition.
// Chrome/Edge on desktop: full support. Firefox/Safari: limited or none.
const hasSpeechRecognition = () =>
  typeof window !== "undefined" &&
  ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);

const createSpeechRecognition = (lang = "en-IN") => {
  if (!hasSpeechRecognition()) return null;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const rec = new SR();
  rec.continuous      = true;   // keep listening until we stop it
  rec.interimResults  = true;   // fire events for in-progress words
  rec.maxAlternatives = 1;
  rec.lang            = lang;   // match the app's language
  return rec;
};
// ─────────────────────────────────────────────────────────────────────────────

export function useChat() {
  // ── State ──────────────────────────────────────────────────────────────────
  const [messages, setMessages]                   = useState([]);
  const [input, setInput]                         = useState("");
  const [isTyping, setIsTyping]                   = useState(false);
  const [connectionStatus, setConnectionStatus]   = useState("Reconnecting");
  const [selectedLanguage, setSelectedLanguage]   = useState(() => safeLoad(STORAGE_UI_KEY, {}).selectedLanguage || "auto");
  const [darkMode, setDarkMode]                   = useState(() => Boolean(safeLoad(STORAGE_UI_KEY, {}).darkMode));
  const [sessionMode, setSessionMode]             = useState(() => safeLoad(STORAGE_UI_KEY, {}).sessionMode || "chat");
  const [sessionId, setSessionId]                 = useState("");
  const [isRecording, setIsRecording]             = useState(false);
  const [micUnavailable, setMicUnavailable]       = useState(false);
  const [ttsProvider, setTtsProvider]             = useState("unknown");
  const [voiceState, setVoiceState]               = useState("idle");
  // liveTranscript = what shows in YOU SAID (live while speaking, final after stop)
  const [liveTranscript, setLiveTranscript]       = useState("");
  // clearResponse: true while user is speaking/transcribing — hides old response
  const [clearResponse, setClearResponse]         = useState(false);
  const [isSpeaking, setIsSpeaking]               = useState(false);
  const [enableVoiceInChat, setEnableVoiceInChat] = useState(
    () => safeLoad(STORAGE_UI_KEY, {}).enableVoiceInChat || false
  );
  // typedTranscript is the value rendered in the YOU SAID card — set directly, no animation
  const [typedTranscript, setTypedTranscript]     = useState("");

  // ── Refs ───────────────────────────────────────────────────────────────────
  const wsRef                    = useRef(null);
  const activeStreamIdRef        = useRef(null);
  const pendingUserTextRef       = useRef("");
  const mediaRecorderRef         = useRef(null);
  const mediaStreamRef           = useRef(null);
  const stopRecordingPromiseRef  = useRef(null);
  const audioChunkBlobsRef       = useRef([]);
  const audioQueueRef            = useRef([]);
  const isAudioPlayingRef        = useRef(false);
  const shouldPlayTtsRef         = useRef(false);
  const handleIncomingMessageRef = useRef(null);
  const streamingTTSRef          = useRef(null);
  const currentAudioRef          = useRef(null);
  // Web Speech API recognition instance
  const recognitionRef           = useRef(null);
  // Tracks the interim text so we can update YOU SAID in real-time
  const interimTextRef           = useRef("");

  // ── stopAllAudio ───────────────────────────────────────────────────────────
  const stopAllAudio = useCallback(() => {
    audioQueueRef.current     = [];
    isAudioPlayingRef.current = false;
    if (currentAudioRef.current) {
      currentAudioRef.current.pause();
      currentAudioRef.current.src = "";
      currentAudioRef.current = null;
    }
    setIsSpeaking(false);
    setVoiceState((prev) => (prev === "speaking" ? "idle" : prev));
  }, []);

  // ── playNextAudio ──────────────────────────────────────────────────────────
  const playNextAudio = useCallback(() => {
    if (isAudioPlayingRef.current) return;
    const next = audioQueueRef.current.shift();
    if (!next) {
      setIsSpeaking(false);
      setVoiceState((prev) => (prev === "speaking" ? "idle" : prev));
      return;
    }
    isAudioPlayingRef.current = true;
    setIsSpeaking(true);
    setVoiceState("speaking");
    const url = URL.createObjectURL(next);
    const audio = new Audio(url);
    currentAudioRef.current = audio;
    const cleanup = () => {
      URL.revokeObjectURL(url);
      isAudioPlayingRef.current = false;
      currentAudioRef.current = null;
      playNextAudio();
    };
    audio.onended = cleanup;
    audio.onerror = cleanup;
    audio.play().catch(cleanup);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Persist UI settings ────────────────────────────────────────────────────
  useEffect(() => {
    localStorage.setItem(
      STORAGE_UI_KEY,
      JSON.stringify({ darkMode, selectedLanguage, sessionMode, enableVoiceInChat })
    );
  }, [darkMode, selectedLanguage, sessionMode, enableVoiceInChat]);

  // ── sessionMode / TTS sync ─────────────────────────────────────────────────
  useEffect(() => {
    if (sessionMode === "voice") {
      setEnableVoiceInChat(false);
      shouldPlayTtsRef.current = true;
    } else {
      shouldPlayTtsRef.current = enableVoiceInChat;
    }
  }, [sessionMode, enableVoiceInChat]);

  // ── Clear YOU SAID after response is done ──────────────────────────────────
  useEffect(() => {
    if (voiceState === "idle" || voiceState === "generating") {
      const t = setTimeout(() => {
        setTypedTranscript("");
        setLiveTranscript("");
        interimTextRef.current = "";
      }, 800);
      return () => clearTimeout(t);
    }
  }, [voiceState]);

  // ── appendMessage / finalizeStream ────────────────────────────────────────
  const appendMessage = useCallback((msg) => {
    setMessages((prev) => [...prev, makeMessage(msg)]);
  }, []);

  const finalizeStream = useCallback((payload) => {
    const cleanedText = sanitizeText(payload.text || payload.message || "");
    const language    = normalizeLang(payload.language);
    setMessages((prev) => {
      const streamId = activeStreamIdRef.current;
      if (!streamId)
        return [...prev, makeMessage({ role: "bot", text: cleanedText, source: payload.source, language })];
      const updated = prev.map((m) =>
        m.id === streamId
          ? { ...m, text: cleanedText || m.text, isStreaming: false, source: payload.source || m.source, language }
          : m
      );
      activeStreamIdRef.current = null;
      return updated;
    });
  }, []);

  // ── handleIncomingMessageRef ───────────────────────────────────────────────
  handleIncomingMessageRef.current = (payload) => {
    const type = payload.type;
    if (payload.session_id) setSessionId(payload.session_id);

    if (type === "session_updated" || type === "session_cleared") {
      setMessages([]);
      setLiveTranscript("");
      setTypedTranscript("");
      setClearResponse(false);
      setVoiceState("idle");
      stopAllAudio();
      streamingTTSRef.current?.reset();
      return;
    }
    if (type === "assistant_token") {
      const token = sanitizeText(payload.token || "");
      if (!token) return;
      setIsTyping(true);
      setVoiceState("generating");
      if (shouldPlayTtsRef.current && streamingTTSRef.current) {
        if (payload.language) streamingTTSRef.current.setLanguage(normalizeLang(payload.language).toLowerCase());
        streamingTTSRef.current.feedToken(token);
      }
      setMessages((prev) => {
        const sid = activeStreamIdRef.current;
        if (!sid) {
          const m = makeMessage({
            role: "bot", text: token, isStreaming: true,
            source: payload.source, language: normalizeLang(payload.language),
          });
          activeStreamIdRef.current = m.id;
          return [...prev, m];
        }
        return prev.map((msg) => (msg.id === sid ? { ...msg, text: `${msg.text}${token}` } : msg));
      });
      return;
    }
    if (type === "assistant_final") {
      setIsTyping(false);
      setClearResponse(false);   // show the new response
      finalizeStream(payload);
      setVoiceState((prev) => (prev === "generating" ? "idle" : prev));
      if (shouldPlayTtsRef.current && streamingTTSRef.current) streamingTTSRef.current.flush();
      return;
    }
    if (type === "partial_transcript") {
      // Whisper partials — only use if Web Speech API is NOT available
      // (Web Speech API already handles live display in the browser)
      if (!hasSpeechRecognition()) {
        const text = (payload.text || "").trim();
        if (text && !text.startsWith("\uD83C\uDFA4")) {
          setTypedTranscript(text);
          setLiveTranscript(text);
        }
      }
      return;
    }
    if (type === "user_transcript") {
      // Final Whisper transcript — replaces Web Speech API interim text
      const text = payload.text || "";
      setTypedTranscript(text);
      setLiveTranscript(text);
      interimTextRef.current = "";
      const pending  = (pendingUserTextRef.current || "").trim().toLowerCase();
      const incoming = text.trim().toLowerCase();
      if (pending && pending === incoming) {
        pendingUserTextRef.current = "";
        setVoiceState("transcribing");
        return;
      }
      appendMessage({ role: "user", text, source: payload.source, language: normalizeLang(payload.language) });
      setVoiceState("transcribing");
      return;
    }
    if (type === "tts_chunk" && payload.audio_base64) {
      if (!shouldPlayTtsRef.current) return;
      if (payload.provider) setTtsProvider(payload.provider);
      audioQueueRef.current.push(decodeBase64ToBlob(payload.audio_base64, payload.mime || "audio/wav"));
      playNextAudio();
      return;
    }
    if (type === "tts_provider") { setTtsProvider(payload.provider || "unknown"); return; }
    if (type === "tts_end") {
      if (!audioQueueRef.current.length && !isAudioPlayingRef.current) {
        setIsSpeaking(false);
        setVoiceState((prev) => (prev === "speaking" ? "idle" : prev));
      }
      return;
    }
    if (type === "error" || type === "asr_error") {
      stopAllAudio();
      streamingTTSRef.current?.reset();
      appendMessage({ role: "bot", text: payload.message || "Something went wrong.", source: payload.source, language: normalizeLang(payload.language) });
      setIsTyping(false);
      setVoiceState("idle");
    }
  };

  // ── WebSocket mount ────────────────────────────────────────────────────────
  useEffect(() => {
    localStorage.removeItem("cp_chat_messages_v2");
    const ws = new ChatWebSocket(WS_URL, {
      onMessage: (payload) => handleIncomingMessageRef.current(payload),
      onStatus:  setConnectionStatus,
    });
    ws.connect();
    wsRef.current = ws;
    streamingTTSRef.current = new StreamingTTSBuffer({
      language: "en",
      onSentenceReady: (s, l) => { void s; void l; },
    });
    return () => ws.disconnect();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── sendText ───────────────────────────────────────────────────────────────
  const sendText = useCallback(() => {
    const text = input.trim();
    if (!text || !wsRef.current?.isOpen() || isRecording) return;
    appendMessage({ role: "user", text, language: normalizeLang(selectedLanguage) });
    pendingUserTextRef.current = text;
    setIsTyping(true);
    setVoiceState("generating");
    stopAllAudio();
    streamingTTSRef.current?.reset();
    wsRef.current.sendText(text, selectedLanguage, enableVoiceInChat);
    setInput("");
  }, [appendMessage, input, isRecording, selectedLanguage, enableVoiceInChat, stopAllAudio]);

  // ── toggleRecording ────────────────────────────────────────────────────────
  // Two parallel tracks run simultaneously when recording:
  //
  // Track A — Web Speech API (browser built-in, free, instant):
  //   Fires onresult events with interim words in near real-time (<100ms).
  //   Words appear in YOU SAID AS the user speaks — Google-style.
  //   Falls back gracefully if browser doesn't support it (Firefox, older Safari).
  //
  // Track B — MediaRecorder + Whisper (backend, accurate):
  //   Collects full audio, sends at stop → Whisper gives accurate final text.
  //   Replaces the Web Speech API interim text once it arrives.
  //
  // This gives the FEEL of instant recognition (Track A) with the ACCURACY
  // of Whisper (Track B) — best of both worlds.
  const toggleRecording = useCallback(async () => {
    if (micUnavailable || !wsRef.current?.isOpen()) return;

    if (!isRecording) {
      // ── START ──────────────────────────────────────────────────────────────
      stopAllAudio();
      streamingTTSRef.current?.reset();
      setTypedTranscript("");
      setLiveTranscript("");
      interimTextRef.current = "";
      setClearResponse(true);

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        setMicUnavailable(false);
        mediaStreamRef.current = stream;

        // ── Track A: Web Speech API for instant live display ───────────────
        if (hasSpeechRecognition()) {
          // Map app language to BCP-47 tag for Web Speech API
          const speechLang =
            selectedLanguage === "hi" ? "hi-IN" :
            selectedLanguage === "ta" ? "ta-IN" : "en-IN";

          const rec = createSpeechRecognition(speechLang);
          recognitionRef.current = rec;

          rec.onresult = (event) => {
            let interim = "";
            let finalSoFar = "";
            for (let i = event.resultIndex; i < event.results.length; i++) {
              const t = event.results[i][0].transcript;
              if (event.results[i].isFinal) finalSoFar += t + " ";
              else interim += t;
            }
            // Show the most complete text we have right now
            const display = (finalSoFar + interim).trim();
            if (display) {
              interimTextRef.current = display;
              setTypedTranscript(display);
              setLiveTranscript(display);
            }
          };

          rec.onerror = (e) => {
            // "no-speech" and "aborted" are normal — not real errors
            if (e.error !== "no-speech" && e.error !== "aborted") {
              console.warn("SpeechRecognition error:", e.error);
            }
          };

          // onend fires when recognition stops — restart it while still recording
          // so continuous mode stays active across browser-imposed time limits
          rec.onend = () => {
            if (isRecording && recognitionRef.current === rec) {
              try { rec.start(); } catch (_) {}
            }
          };

          try { rec.start(); } catch (_) {}
        }
        // ── End Track A ────────────────────────────────────────────────────

        // ── Track B: MediaRecorder + Whisper for accurate final text ────────
        const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
          ? "audio/webm;codecs=opus" : "audio/webm";
        const recorder = new MediaRecorder(stream, { mimeType });
        mediaRecorderRef.current   = recorder;
        audioChunkBlobsRef.current = [];

        recorder.ondataavailable = (event) => {
          if (!event.data?.size) return;
          // Collect chunks — will be assembled into one complete WebM blob at stop.
          // We do NOT stream individual chunks to the backend because WebM chunks
          // after the first are missing the EBML container header and cause
          // "InvalidDataError: Invalid data found" in PyAV/Whisper.
          // Web Speech API handles live display; Whisper gets the full clean blob.
          audioChunkBlobsRef.current.push(event.data);
        };

        recorder.start(250); // 250ms chunks → continuous backend streaming
        // ── End Track B ────────────────────────────────────────────────────

        setIsRecording(true);
        setVoiceState("listening");
      } catch {
        setMicUnavailable(true);
      }
      return;
    }

    // ── STOP ───────────────────────────────────────────────────────────────
    // Stop Web Speech API first so it doesn't fire more results
    if (recognitionRef.current) {
      recognitionRef.current.onend = null; // prevent auto-restart
      try { recognitionRef.current.stop(); } catch (_) {}
      recognitionRef.current = null;
    }

    // Stop MediaRecorder
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      stopRecordingPromiseRef.current = new Promise((resolve) =>
        recorder.addEventListener("stop", resolve, { once: true })
      );
      recorder.stop();
      await stopRecordingPromiseRef.current;
    }

    // Send the complete audio blob as one binary message then signal audio_end.
    // Sending as one complete WebM ensures PyAV can decode it correctly.
    if (audioChunkBlobsRef.current.length > 0) {
      const fullBlob = new Blob(audioChunkBlobsRef.current, {
        type: recorder?.mimeType || "audio/webm",
      });
      const fullBuffer = await fullBlob.arrayBuffer();
      wsRef.current?.sendAudioChunk(fullBuffer);
    }
    wsRef.current?.sendControl("audio_end", { language: selectedLanguage });

    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    mediaStreamRef.current = null;
    setIsRecording(false);
    setVoiceState("transcribing");
    // Keep showing whatever Web Speech showed until Whisper result arrives
  }, [isRecording, micUnavailable, selectedLanguage, stopAllAudio]);

  // ── clearChat / startNewSession ────────────────────────────────────────────
  const clearChat = useCallback(() => {
    setMessages([]);
    wsRef.current?.sendControl("clear_session");
  }, []);

  const startNewSession = useCallback(() => {
    setMessages([]);
    setLiveTranscript("");
    setTypedTranscript("");
    stopAllAudio();
    streamingTTSRef.current?.reset();
    wsRef.current?.sendControl("new_session");
  }, [stopAllAudio]);

  // ── latestResponse ─────────────────────────────────────────────────────────
  const latestResponse = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "bot") return messages[i].text;
    }
    return "";
  }, [messages]);

  return {
    messages, input, setInput, sendText, isTyping, clearChat, startNewSession,
    clearResponse,
    connectionStatus, selectedLanguage, setSelectedLanguage,
    isRecording, micUnavailable, toggleRecording,
    darkMode, toggleDarkMode: () => setDarkMode((prev) => !prev),
    sessionMode, setSessionMode, sessionId,
    voiceState, liveTranscript, typedTranscript, latestResponse,
    isSpeaking, ttsProvider, enableVoiceInChat, setEnableVoiceInChat, stopAllAudio,
  };
}