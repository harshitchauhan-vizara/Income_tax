import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChatWebSocket } from "../services/websocket";
import { StreamingTTSBuffer } from "../utils/streamingTTS";

const STORAGE_MESSAGES_KEY = "cp_chat_messages_v2";
const STORAGE_UI_KEY = "cp_chat_ui_v2";

const getDefaultWsUrl = () => {
  if (typeof window === "undefined") return "wss://hdlchdowcobp.online/ws";
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}/ws`;
};

const WS_URL = import.meta.env.VITE_WS_URL || getDefaultWsUrl();

const safeLoad = (key, fallback) => {
  try {
    const val = JSON.parse(localStorage.getItem(key) || "null");
    return val ?? fallback;
  } catch {
    return fallback;
  }
};

const createId = () => (crypto?.randomUUID ? crypto.randomUUID() : `msg_${Date.now()}`);
const now = () => new Date().toISOString();

const makeMessage = (overrides = {}) => ({
  id: createId(),
  role: "bot",
  text: "",
  timestamp: now(),
  source: null,
  language: "EN",
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
  const len = binary.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mime });
};

export function useChat() {
  // ── State (same order every render) ──────────────────────────────────
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
  const [liveTranscript, setLiveTranscript]       = useState("");
  const [isSpeaking, setIsSpeaking]               = useState(false);
  const [enableVoiceInChat, setEnableVoiceInChat] = useState(
    () => safeLoad(STORAGE_UI_KEY, {}).enableVoiceInChat || false
  );
  const [typedTranscript, setTypedTranscript]     = useState("");

  // ── Refs (same count every render) ───────────────────────────────────
  const wsRef                   = useRef(null);
  const activeStreamIdRef       = useRef(null);
  const pendingUserTextRef      = useRef("");
  const mediaRecorderRef        = useRef(null);
  const mediaStreamRef          = useRef(null);
  const stopRecordingPromiseRef = useRef(null);
  const audioChunkBlobsRef      = useRef([]);
  const audioQueueRef           = useRef([]);
  const isAudioPlayingRef       = useRef(false);
  const shouldPlayTtsRef        = useRef(false);
  // Single handler ref — avoids recreating WebSocket on every render
  const handleIncomingMessageRef = useRef(null);

  // ── NEW: StreamingTTSBuffer ref ────────────────────────────────────────
  // One buffer instance per chat session. Lives in a ref so it persists
  // across renders without triggering re-renders itself.
  const streamingTTSRef = useRef(null);

  const currentAudioRef = useRef(null);   // track the currently playing Audio object

  // ── stopAllAudio — immediately stops all queued and playing audio ─────
  const stopAllAudio = useCallback(() => {
    audioQueueRef.current     = [];        // clear pending queue
    isAudioPlayingRef.current = false;
    if (currentAudioRef.current) {
      currentAudioRef.current.pause();
      currentAudioRef.current.src = "";
      currentAudioRef.current = null;
    }
    setIsSpeaking(false);
    setVoiceState((prev) => (prev === "speaking" ? "idle" : prev));
  }, []);

  // ── playNextAudio — plain function, no hook ───────────────────────────
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

  // ── Persist UI settings only (not messages) ───────────────────────────
  useEffect(() => {
    localStorage.setItem(
      STORAGE_UI_KEY,
      JSON.stringify({ darkMode, selectedLanguage, sessionMode, enableVoiceInChat })
    );
  }, [darkMode, selectedLanguage, sessionMode, enableVoiceInChat]);

  // ── Typewriter effect for live transcript ─────────────────────────────
  // When liveTranscript changes (ASR result arrives), animate it character
  // by character into typedTranscript so it appears to be typed live.
  useEffect(() => {
    if (!liveTranscript) {
      setTypedTranscript("");
      return;
    }
    setTypedTranscript("");
    let i = 0;
    const interval = setInterval(() => {
      i += 1;
      setTypedTranscript(liveTranscript.slice(0, i));
      if (i >= liveTranscript.length) clearInterval(interval);
    }, 22); // ~45 chars/second — feels natural
    return () => clearInterval(interval);
  }, [liveTranscript]);

  useEffect(() => {
    if (sessionMode === "voice") {
      setEnableVoiceInChat(false);
      shouldPlayTtsRef.current = true;
    } else {
      shouldPlayTtsRef.current = enableVoiceInChat;
    }
  }, [sessionMode, enableVoiceInChat]);

  // ── appendMessage / finalizeStream ────────────────────────────────────
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

  // ── Keep handleIncomingMessageRef current without adding hook count ───
  // Assigned every render so it always closes over latest state/callbacks,
  // but the ref identity never changes → WebSocket effect stays mount-only.
  handleIncomingMessageRef.current = (payload) => {
    const type = payload.type;
    if (payload.session_id) setSessionId(payload.session_id);

    if (type === "session_updated") {
      setMessages([]);
      setLiveTranscript("");
      setTypedTranscript("");
      setVoiceState("idle");
      stopAllAudio();
      streamingTTSRef.current?.reset();
      return;
    }
    if (type === "session_cleared") {
      setMessages([]);
      setLiveTranscript("");
      setTypedTranscript("");
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

      // ── STREAMING TTS: feed each token into the buffer ────────────────
      // The buffer fires onSentenceReady as soon as a full sentence arrives.
      // Only do this when TTS playback is actually enabled.
      if (shouldPlayTtsRef.current && streamingTTSRef.current) {
        // Sync language from the token payload when available
        if (payload.language) {
          streamingTTSRef.current.setLanguage(
            normalizeLang(payload.language).toLowerCase()
          );
        }
        streamingTTSRef.current.feedToken(token);
      }
      // ── END STREAMING TTS ─────────────────────────────────────────────

      setMessages((prev) => {
        const sid = activeStreamIdRef.current;
        if (!sid) {
          const m = makeMessage({
            role: "bot",
            text: token,
            isStreaming: true,
            source: payload.source,
            language: normalizeLang(payload.language),
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
      finalizeStream(payload);
      setVoiceState((prev) => (prev === "generating" ? "idle" : prev));

      // ── STREAMING TTS: flush any remaining partial sentence ────────────
      // When the LLM stream ends there may be text in the buffer that never
      // hit a sentence boundary (e.g. a response that ends without a period).
      if (shouldPlayTtsRef.current && streamingTTSRef.current) {
        streamingTTSRef.current.flush();
      }
      // ── END STREAMING TTS ─────────────────────────────────────────────
      return;
    }
    if (type === "partial_transcript") {
      const text = payload.text || "";
      if (text) setLiveTranscript(text);
      return;
    }
    if (type === "user_transcript") {
      const text     = payload.text || "";
      setLiveTranscript(text);        // ← ADD THIS LINE to clear the partial
      const pending  = (pendingUserTextRef.current || "").trim().toLowerCase();
      const incoming = text.trim().toLowerCase();
      setLiveTranscript(text);
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
      const blob = decodeBase64ToBlob(payload.audio_base64, payload.mime || "audio/wav");
      audioQueueRef.current.push(blob);
      playNextAudio();
      return;
    }
    if (type === "tts_provider") {
      setTtsProvider(payload.provider || "unknown");
      return;
    }
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
      appendMessage({
        role: "bot",
        text: payload.message || "Something went wrong.",
        source: payload.source,
        language: normalizeLang(payload.language),
      });
      setIsTyping(false);
      setVoiceState("idle");
    }
  };

  // ── WebSocket — created ONCE on mount ─────────────────────────────────
  useEffect(() => {
    localStorage.removeItem("cp_chat_messages_v2"); // clear old data
    
    const ws = new ChatWebSocket(WS_URL, {
      onMessage: (payload) => handleIncomingMessageRef.current(payload),
      onStatus:  setConnectionStatus,
    });
    ws.connect();
    wsRef.current = ws;

    // ── NEW: Create the StreamingTTSBuffer, wired to sendTTSRequest ──────
    // onSentenceReady fires each time a complete sentence is buffered.
    // It sends a "tts_request" WS message → backend synthesises it with
    // Sarvam → sends back "tts_chunk" → existing audioQueue plays it.
    // This is the ONLY place the buffer is created — once per mount.
    streamingTTSRef.current = new StreamingTTSBuffer({
      language: "en",
      onSentenceReady: (sentence, language) => {
        // Backend now handles sentence-by-sentence TTS after collecting
        // the full clean answer. Do NOT send tts_request from the frontend
        // to avoid speaking the response twice.
        // This callback is intentionally a no-op.
        void sentence; void language;
      },
    });
    // ── END NEW ───────────────────────────────────────────────────────────

    return () => ws.disconnect();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── sendText ──────────────────────────────────────────────────────────
  const sendText = useCallback(() => {
    const text = input.trim();
    if (!text || !wsRef.current?.isOpen() || isRecording) return;
    appendMessage({ role: "user", text, language: normalizeLang(selectedLanguage) });
    pendingUserTextRef.current = text;
    setIsTyping(true);
    setVoiceState("generating");

    // Stop any audio still playing from the previous response
    stopAllAudio();
    // Reset the TTS buffer so previous partial sentences don't bleed in
    streamingTTSRef.current?.reset();

    wsRef.current.sendText(text, selectedLanguage, enableVoiceInChat);
    setInput("");
  }, [appendMessage, input, isRecording, selectedLanguage, enableVoiceInChat, stopAllAudio]);

  // ── toggleRecording ───────────────────────────────────────────────────
  const toggleRecording = useCallback(async () => {
    if (micUnavailable || !wsRef.current?.isOpen()) return;
    if (!isRecording) {
      // Stop any playing audio before recording
      stopAllAudio();
      streamingTTSRef.current?.reset();
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        setMicUnavailable(false);
        mediaStreamRef.current = stream;
        const recorder = new MediaRecorder(stream, {
          mimeType: MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
            ? "audio/webm;codecs=opus"
            : "audio/webm",
        });
        mediaRecorderRef.current   = recorder;
        audioChunkBlobsRef.current = [];
        recorder.ondataavailable   = (event) => {
          if (event.data?.size) audioChunkBlobsRef.current.push(event.data);
        };
        recorder.start();
        setIsRecording(true);
        setVoiceState("listening");
      } catch {
        setMicUnavailable(true);
      }
      return;
    }
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      stopRecordingPromiseRef.current = new Promise((resolve) =>
        recorder.addEventListener("stop", resolve, { once: true })
      );
      recorder.stop();
      await stopRecordingPromiseRef.current;
    }
    const audioBlob = new Blob(audioChunkBlobsRef.current, {
      type: recorder?.mimeType || "audio/webm",
    });
    const buffer = await audioBlob.arrayBuffer();

    // Reset TTS buffer before sending voice query too
    streamingTTSRef.current?.reset();

    wsRef.current?.sendAudioChunk(buffer);
    wsRef.current?.sendControl("audio_end", { language: selectedLanguage });
    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    mediaStreamRef.current = null;
    setIsRecording(false);
    setVoiceState("transcribing");
  }, [isRecording, micUnavailable, selectedLanguage, stopAllAudio]);

  // ── clearChat / startNewSession ───────────────────────────────────────
  const clearChat = useCallback(() => {
    setMessages([]);
    wsRef.current?.sendControl("clear_session");
  }, []);

  const startNewSession = useCallback(() => {
    setMessages([]);
    setLiveTranscript("");
    stopAllAudio();
    streamingTTSRef.current?.reset();
    wsRef.current?.sendControl("new_session");
  }, [stopAllAudio]);

  // ── Typewriter effect: animate liveTranscript → typedTranscript ──────
  // When a new transcript arrives, reveal it character by character.
  // Speed: 18ms per character (~55 chars/sec) — fast enough to feel live.
  const typewriterRef = useRef(null);

  useEffect(() => {
    // Clear any running animation
    if (typewriterRef.current) {
      clearInterval(typewriterRef.current);
      typewriterRef.current = null;
    }

    if (!liveTranscript) {
      setTypedTranscript("");
      return;
    }

    // Start from scratch each time a new transcript arrives
    let i = 0;
    setTypedTranscript("");
    typewriterRef.current = setInterval(() => {
      i += 1;
      setTypedTranscript(liveTranscript.slice(0, i));
      if (i >= liveTranscript.length) {
        clearInterval(typewriterRef.current);
        typewriterRef.current = null;
      }
    }, 18);

    return () => {
      if (typewriterRef.current) {
        clearInterval(typewriterRef.current);
        typewriterRef.current = null;
      }
    };
  }, [liveTranscript]);

  // Clear typedTranscript when voice state moves past transcribing
  useEffect(() => {
    if (voiceState === "idle" || voiceState === "generating") {
      // Small delay so user sees the completed transcript briefly
      const t = setTimeout(() => setTypedTranscript(""), 600);
      return () => clearTimeout(t);
    }
  }, [voiceState]);
  // ── END typewriter ────────────────────────────────────────────────────
  const latestResponse = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === "bot") return messages[i].text;
    }
    return "";
  }, [messages]);

  return {
    messages,
    input,
    setInput,
    sendText,
    isTyping,
    clearChat,
    startNewSession,
    connectionStatus,
    selectedLanguage,
    setSelectedLanguage,
    isRecording,
    micUnavailable,
    toggleRecording,
    darkMode,
    toggleDarkMode: () => setDarkMode((prev) => !prev),
    sessionMode,
    setSessionMode,
    sessionId,
    voiceState,
    liveTranscript,
    typedTranscript,
    latestResponse,
    isSpeaking,
    ttsProvider,
    enableVoiceInChat,
    setEnableVoiceInChat,
    stopAllAudio,
  };
}