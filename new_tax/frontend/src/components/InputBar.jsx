import { useMemo } from "react";
import MicButton from "./MicButton";

const InputBar = ({
  value,
  onChange,
  onSend,
  disabled,
  isRecording,
  micUnavailable,
  onToggleMic,
  enableVoiceInChat,
  onToggleVoiceInChat,
  voiceState,
}) => {
  const isSendDisabled = useMemo(() => disabled || !value.trim(), [disabled, value]);

  const onKeyDown = (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!isSendDisabled) onSend();
    }
  };

  // Animated placeholder based on voice state
  const placeholder = (() => {
    if (voiceState === "listening")    return "🎙 Listening to you…";
    if (voiceState === "transcribing") return "✍️  Transcribing…";
    if (voiceState === "generating")   return "⏳ Generating response…";
    if (voiceState === "speaking")     return "🔊 Speaking…";
    if (isRecording)                   return "Recording in progress…";
    return "Type your message…";
  })();

  return (
    <footer className="input-bar-wrap">
      <div className={`input-bar ${voiceState !== "idle" ? `input-bar--${voiceState}` : ""}`}>
        <textarea
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          className="chat-input"
          placeholder={placeholder}
          rows={1}
        />

        <button
          type="button"
          className="send-btn"
          disabled={isSendDisabled}
          onClick={onSend}
        >
          Send
        </button>

        <label className="voice-toggle">
          <input
            type="checkbox"
            checked={enableVoiceInChat}
            onChange={onToggleVoiceInChat}
          />
          <span>Voice</span>
        </label>

        <MicButton
          isRecording={isRecording}
          disabled={micUnavailable}
          onToggle={onToggleMic}
          voiceState={voiceState}
        />
      </div>
    </footer>
  );
};

export default InputBar;