const sourceLabel = {
  rag: "Knowledge Base",
  qna: "FAQ",
  general: "General Assistance",
};

const formatTime = (value) => {
  try {
    return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
};

const MessageBubble = ({ message }) => {
  const isUser = message.role === "user";
  const lang = message.language || "EN";
  const source = message.source ? sourceLabel[message.source] || message.source : null;

  return (
    <article className={`message-row ${isUser ? "user" : "bot"}`}>
      <div className={`message-bubble ${isUser ? "user" : "bot"} fade-in`}>
        <p className="message-text" lang={lang === "HI" ? "hi" : lang === "TA" ? "ta" : "en"}>
          {message.text}
        </p>        
          <div className="message-meta">
          <span className="lang-badge">{lang}</span>
          <span className="timestamp">{formatTime(message.timestamp)}</span>
        </div>
        {source ? <span className="source-badge">{source}</span> : null}
      </div>
    </article>
  );
};

export default MessageBubble;
