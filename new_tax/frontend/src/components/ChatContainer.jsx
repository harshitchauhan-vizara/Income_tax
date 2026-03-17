import { useEffect, useRef } from "react";
import MessageBubble from "./MessageBubble";
import TypingIndicator from "./TypingIndicator";

/**
 * voiceState values from useChat:
 *   "idle"         — nothing happening
 *   "listening"    — mic open, user speaking
 *   "transcribing" — audio sent to Whisper, waiting for text
 *   "generating"   — LLM generating response
 *   "speaking"     — TTS playing
 *
 * typedTranscript — animated character-by-character version of liveTranscript
 */
const ChatContainer = ({ messages, isTyping, voiceState, typedTranscript }) => {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, isTyping, voiceState, typedTranscript]);

  const isListening    = voiceState === "listening";
  const isTranscribing = voiceState === "transcribing";

  return (
    <section className="chat-container" aria-live="polite">
      {messages.length === 0 && !isListening && !isTranscribing ? (
        <div className="empty-state">
          <h2>Welcome to Income Tax Act 2025 Assistant</h2>
          <p>Ask your question in English or Hindi.</p>
        </div>
      ) : null}

      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} />
      ))}

      {/* Live voice bubble — shown while mic is open or transcription in progress */}
      {(isListening || isTranscribing) ? (
        <div className="message-bubble user live-voice-bubble" aria-live="polite">
          <div className="bubble-content">
            {isListening && !typedTranscript ? (
              /* Pulsing dots while user is still speaking */
              <span className="voice-listening-dots">
                <span /><span /><span />
              </span>
            ) : (
              /* Transcribed text appears character-by-character */
              <span className="live-transcript-text">
                {typedTranscript}
                <span className="transcript-cursor" aria-hidden="true">|</span>
              </span>
            )}
          </div>
          <div className="bubble-meta">
            <span className="voice-state-label">
              {isListening ? "🎙 Listening…" : "✍️ Transcribing…"}
            </span>
          </div>
        </div>
      ) : null}

      {isTyping ? <TypingIndicator /> : null}
      <div ref={bottomRef} />
    </section>
  );
};

export default ChatContainer;