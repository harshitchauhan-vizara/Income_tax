const Header = ({
  title,
  subtitle,
  connectionStatus,
  sessionId,
  darkMode,
  onToggleDarkMode,
  onClearChat,
  onNewSession,
  selectedLanguage,
  onLanguageChange,
  sessionMode,
  onSessionModeChange,
}) => {
  const statusKey = connectionStatus ? connectionStatus.toLowerCase() : "disconnected";

  return (
    <header className="chat-header" role="banner">
      {/* Brand block intentionally empty — branding lives in Sidebar */}
      <div className="brand-block" aria-hidden="true" />

      <div className="header-controls">
        {/* Left: mode toggle */}
        <div className="mode-toggle" role="tablist" aria-label="Session mode">
          <button
            type="button"
            role="tab"
            aria-selected={sessionMode === "chat"}
            className={sessionMode === "chat" ? "active" : ""}
            onClick={() => onSessionModeChange("chat")}
          >
            Chat Mode
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={sessionMode === "voice"}
            className={sessionMode === "voice" ? "active" : ""}
            onClick={() => onSessionModeChange("voice")}
          >
            Voice Agent Mode
          </button>
        </div>

        {/* Right: connection + lang + actions */}
        <div className="header-right">
          <span
            className={`connection-pill ${statusKey}`}
            aria-live="polite"
            aria-label={`Connection status: ${connectionStatus}`}
          >
            <span className="status-dot" aria-hidden="true" />
            {connectionStatus}
          </span>

          <select
            className="lang-select"
            value={selectedLanguage}
            onChange={(e) => onLanguageChange(e.target.value)}
            aria-label="Select language"
          >
            <option value="auto">Auto</option>
            <option value="en">English</option>
            <option value="hi">Hindi</option>
            <option value="ta">Tamil</option>
          </select>

          <button
            className="ghost-btn"
            type="button"
            onClick={onClearChat}
            aria-label="Clear session history"
          >
            Clear Session
          </button>

          <button
            className="ghost-btn"
            type="button"
            onClick={onNewSession}
            aria-label="Start a new session"
          >
            New Session
          </button>
        </div>
      </div>
    </header>
  );
};

export default Header;