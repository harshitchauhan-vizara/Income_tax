const TypingIndicator = () => {
  return (
    <div className="message-row bot typing-row" aria-live="polite" aria-label="Assistant is typing">
      <div className="message-bubble bot typing-bubble">
        <div className="typing-dots">
          <span />
          <span />
          <span />
        </div>
      </div>
    </div>
  );
};

export default TypingIndicator;
