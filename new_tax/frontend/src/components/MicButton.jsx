const MicButton = ({ isRecording, disabled, onToggle, voiceState }) => {
  const isListening    = voiceState === "listening";
  const isTranscribing = voiceState === "transcribing";
  const isActive       = isRecording || isListening || isTranscribing;

  // Icon and label change by state
  const icon = isListening ? "🎙" : isTranscribing ? "✍️" : "🎙";
  const label = (() => {
    if (isListening)    return "Listening…";
    if (isTranscribing) return "Transcribing…";
    if (isRecording)    return "Stop";
    return "Mic";
  })();

  const ariaLabel = (() => {
    if (isListening)    return "Listening to your voice";
    if (isTranscribing) return "Transcribing your speech";
    if (isRecording)    return "Stop voice recording";
    return "Start voice recording";
  })();

  return (
    <button
      type="button"
      className={[
        "mic-btn",
        isRecording    ? "recording"    : "",
        isListening    ? "listening"    : "",
        isTranscribing ? "transcribing" : "",
      ].filter(Boolean).join(" ")}
      onClick={onToggle}
      disabled={disabled}
      aria-pressed={isActive}
      aria-label={ariaLabel}
      title={disabled ? "Microphone unavailable" : ariaLabel}
    >
      {/* Pulse ring — visible while listening */}
      {isListening ? <span className="mic-pulse-ring" aria-hidden="true" /> : null}

      <span className="mic-icon" aria-hidden="true">{icon}</span>
      <span>{label}</span>
    </button>
  );
};

export default MicButton;