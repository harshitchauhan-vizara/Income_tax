export class ChatWebSocket {
  constructor(url, handlers = {}) {
    this.url = url;
    this.handlers = handlers;
    this.ws = null;
    this.shouldReconnect = true;
    this.reconnectDelay = 1000;
    this.maxReconnectDelay = 10000;
  }

  connect() {
    this.shouldReconnect = true;
    this.handlers.onStatus?.("Reconnecting");
    this.ws = new WebSocket(this.url);
    this.ws.binaryType = "arraybuffer";

    this.ws.onopen = () => {
      this.reconnectDelay = 1000;
      this.handlers.onStatus?.("Connected");
    };

    this.ws.onclose = () => {
      this.handlers.onStatus?.(this.shouldReconnect ? "Reconnecting" : "Disconnected");
      if (!this.shouldReconnect) return;

      setTimeout(() => this.connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, this.maxReconnectDelay);
    };

    this.ws.onerror = () => {
      this.handlers.onStatus?.("Reconnecting");
      this.handlers.onError?.("WebSocket connection error");
    };

    this.ws.onmessage = (event) => {
      if (typeof event.data !== "string") {
        this.handlers.onAudio?.(event.data);
        return;
      }

      try {
        const payload = JSON.parse(event.data);
        this.handlers.onMessage?.(payload);
      } catch {
        this.handlers.onError?.("Invalid server payload");
      }
    };
  }

  disconnect() {
    this.shouldReconnect = false;
    this.ws?.close();
    this.handlers.onStatus?.("Disconnected");
  }

  sendText(text, language = "auto", enableVoice = false) {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send(
      JSON.stringify({
        type: "text",
        text,
        language,
        enableVoice,
      })
    );
  }

  /**
   * Send a streaming TTS request for a single sentence.
   * The backend receives this as a "tts_request" event and calls
   * sarvam_service.synthesize_sentence(text, language), then sends
   * back a "tts_chunk" event with audio_base64 — exactly like normal TTS.
   *
   * @param {string} text     - Complete sentence to synthesise.
   * @param {string} language - Language code (en / hi / ta / auto).
   */
  sendTTSRequest(text, language = "en") {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send(
      JSON.stringify({
        type: "tts_request",
        text,
        language,
      })
    );
  }

  sendControl(type, data = {}) {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type, ...data }));
  }

  isOpen() {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  sendAudioChunk(buffer) {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send(buffer);
  }
}