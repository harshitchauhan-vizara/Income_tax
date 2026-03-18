import { useState, useEffect } from "react";
import ChatContainer from "./components/ChatContainer";
import Header from "./components/Header";
import InputBar from "./components/InputBar";
import VoiceAgentPanel from "./components/VoiceAgentPanel";
import { useChat } from "./hooks/useChat";

/* ── Sidebar icon components (inline SVG, no extra deps) ── */
const Icon = ({ d, d2, viewBox = "0 0 24 24" }) => (
  <svg
    width="18" height="18"
    viewBox={viewBox}
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    className="sb-nav-icon"
  >
    <path d={d} />
    {d2 && <path d={d2} />}
  </svg>
);

const SunIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" className="sb-theme-icon">
    <circle cx="12" cy="12" r="5" />
    <line x1="12" y1="1"  x2="12" y2="3" />
    <line x1="12" y1="21" x2="12" y2="23" />
    <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
    <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
    <line x1="1" y1="12" x2="3" y2="12" />
    <line x1="21" y1="12" x2="23" y2="12" />
    <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
    <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
  </svg>
);

const MoonIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" className="sb-theme-icon">
    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
  </svg>
);

const NAV_ITEMS = [
  {
    id: "dashboard",
    label: "Dashboard",
    icon: <Icon d="M3 3h7v7H3zm11 0h7v7h-7zM3 14h7v7H3zm11 3a4 4 0 1 0 8 0 4 4 0 0 0-8 0z" />,
  },
  {
    id: "conversations",
    label: "Conversations",
    icon: <Icon d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />,
    badge: "3",
  },
  {
    id: "ai-agents",
    label: "AI Agents",
    icon: <Icon d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 5a3 3 0 1 1 0 6 3 3 0 0 1 0-6zm0 13a7.97 7.97 0 0 1-6-2.7C6.01 15.36 9.86 14 12 14s5.99 1.36 6 3.3A7.97 7.97 0 0 1 12 20z" />,
  },
  {
    id: "knowledge",
    label: "Knowledge Base",
    icon: <Icon d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" d2="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />,
  },
  {
    id: "analytics",
    label: "Analytics",
    icon: <Icon d="M18 20V10M12 20V4M6 20v-6" />,
  },
  {
    id: "settings",
    label: "Settings",
    icon: <Icon d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" d2="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />,
  },
];

function App() {
  const [activeNav, setActiveNav] = useState("dashboard");

  const {
    messages,
    connectionStatus,
    isTyping,
    input,
    setInput,
    sendText,
    clearChat,
    startNewSession,
    selectedLanguage,
    setSelectedLanguage,
    isRecording,
    micUnavailable,
    toggleRecording,
    darkMode,
    toggleDarkMode,
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
  } = useChat();

  const [lastTranscript, setLastTranscript] = useState("");
  useEffect(() => {
    if (liveTranscript) setLastTranscript(liveTranscript);
  }, [liveTranscript]);

  const handleClearChat = () => {
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    clearChat();
  };

  const handleNewSession = () => {
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    startNewSession();
  };

  const shellClass = `chatbot-shell${darkMode ? "" : " app-light"}`;

  return (
    <div className={shellClass}>

      {/* ── Left Sidebar ── */}
      <aside className="sidebar">
        <div className="sb-brand">
          <div className="sb-logo">
            <img className="logo-image" src="/income_tax_logo.png" alt="Income Tax Chatbot" />
          </div>
          <div className="sb-brand-text">
            <h2>Income Tax 2026</h2>
            <span>AI TAX ASSISTANT</span>
          </div>
        </div>

        <div className="sb-status">
          <span className="sb-status-dot" />
          <span className="sb-status-text">
            {connectionStatus === "Connected" ? "AI Online" : connectionStatus}
          </span>
        </div>

        <div className="sb-session">
          <div className="sb-session-label">Active Session</div>
          <div className="sb-session-id">{sessionId ? sessionId.slice(0, 14) : "—"}</div>
        </div>

        <div className="sb-nav-label">Navigation</div>
        <nav className="sb-nav">
          {NAV_ITEMS.map((item) => (
            <div
              key={item.id}
              className={`sb-nav-item${activeNav === item.id ? " active" : ""}`}
              onClick={() => setActiveNav(item.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && setActiveNav(item.id)}
            >
              {item.icon}
              {item.label}
              {item.badge && <span className="sb-badge">{item.badge}</span>}
            </div>
          ))}
        </nav>

        <div className="sb-divider" />

        <div className="sb-bottom">
          <div className="sb-context">
            <div className="sb-context-label">
              Context Window
              <span className="sb-context-value">12k / 32k</span>
            </div>
            <div className="sb-context-bar-wrap">
              <div className="sb-context-bar" />
            </div>
          </div>
          <button className="sb-theme-btn" onClick={toggleDarkMode} type="button" aria-label="Toggle theme">
            {darkMode ? <SunIcon /> : <MoonIcon />}
            {darkMode ? "Light Mode" : "Dark Mode"}
          </button>
        </div>
      </aside>

      {/* ── Main Area ── */}
      <div className="chatbot-card">
        <Header
          title="Income Tax Act 2025 Assistant"
          subtitle="AI Powered Tax Guidance"
          connectionStatus={connectionStatus}
          sessionId={sessionId}
          darkMode={darkMode}
          onToggleDarkMode={toggleDarkMode}
          onClearChat={handleClearChat}
          onNewSession={handleNewSession}
          selectedLanguage={selectedLanguage}
          onLanguageChange={setSelectedLanguage}
          sessionMode={sessionMode}
          onSessionModeChange={setSessionMode}
        />

        {sessionMode === "chat" ? (
          <ChatContainer
            messages={messages}
            isTyping={isTyping}
            voiceState={voiceState}
            typedTranscript={typedTranscript}
          />
        ) : (
          <VoiceAgentPanel
            voiceState={voiceState}
            transcript={lastTranscript}
            typedTranscript={typedTranscript}
            latestResponse={latestResponse}
            isRecording={isRecording}
            micUnavailable={micUnavailable}
            onToggleMic={toggleRecording}
            isSpeaking={isSpeaking}
            ttsProvider={ttsProvider}
          />
        )}

        {sessionMode === "chat" && (
          <InputBar
            value={input}
            onChange={setInput}
            onSend={sendText}
            disabled={isRecording}
            isRecording={isRecording}
            micUnavailable={micUnavailable}
            onToggleMic={toggleRecording}
            enableVoiceInChat={enableVoiceInChat}
            onToggleVoiceInChat={() => setEnableVoiceInChat(!enableVoiceInChat)}
            voiceState={voiceState}
          />
        )}
      </div>
    </div>
  );
}

export default App;