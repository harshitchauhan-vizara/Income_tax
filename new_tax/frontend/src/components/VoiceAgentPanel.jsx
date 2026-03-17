import { useEffect, useRef, useCallback, useState } from "react";

const stateLabel = {
  idle:         "READY",
  listening:    "LISTENING...",
  transcribing: "PROCESSING...",
  generating:   "THINKING...",
  speaking:     "SPEAKING...",
};

function detectScriptLang(text = "") {
  if (!text) return "en";
  if (/[\u0900-\u097F]/.test(text)) return "hi";
  if (/[\u0B80-\u0BFF]/.test(text)) return "ta";
  if (/[\u0600-\u06FF]/.test(text)) return "ar";
  return "en";
}

/* ══════════════════════════════════════════════════
   NeuralWaveform  — UNCHANGED
   ══════════════════════════════════════════════════ */
function NeuralWaveform({ voiceState }) {
  const canvasRef    = useRef(null);
  const rafRef       = useRef(null);
  const frameRef     = useRef(0);
  const analyserRef  = useRef(null);
  const streamRef    = useRef(null);
  const actxRef      = useRef(null);
  const oscRef       = useRef(null);
  const oscTimerRef  = useRef(null);
  const ampSmoothRef = useRef(0);
  const isActive     = voiceState !== "idle";

  const SCHEME = {
    idle:        { r:108, g:155, b:210, accent:[140, 185, 230] },
    listening:   { r:220, g:50,  b:50,  accent:[240, 100, 100] },
    transcribing:{ r:234, g:179, b:8,   accent:[255, 210, 60]  },
    generating:  { r:234, g:179, b:8,   accent:[255, 210, 60]  },
    speaking:    { r:58,  g:107, b:163, accent:[90,  145, 205] },
  };

  const teardown = useCallback(() => {
    if (oscTimerRef.current) { clearInterval(oscTimerRef.current); oscTimerRef.current = null; }
    if (oscRef.current)      { try { oscRef.current.stop(); } catch(_){} oscRef.current = null; }
    if (streamRef.current)   { streamRef.current.getTracks().forEach(t=>t.stop()); streamRef.current = null; }
    if (actxRef.current)     { actxRef.current.close().catch(()=>{}); actxRef.current = null; }
    analyserRef.current = null;
  }, []);

  useEffect(() => {
    if (voiceState !== "listening") return;
    let live = true;
    (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio:true, video:false });
        if (!live) { stream.getTracks().forEach(t=>t.stop()); return; }
        streamRef.current = stream;
        const actx = new (window.AudioContext || window.webkitAudioContext)();
        actxRef.current = actx;
        const an = actx.createAnalyser();
        an.fftSize = 512; an.smoothingTimeConstant = 0.88;
        analyserRef.current = an;
        actx.createMediaStreamSource(stream).connect(an);
      } catch(_) {}
    })();
    return () => { live = false; teardown(); };
  }, [voiceState, teardown]);

  useEffect(() => {
    if (voiceState !== "speaking" && voiceState !== "transcribing") return;
    const actx = new (window.AudioContext || window.webkitAudioContext)();
    actxRef.current = actx;
    const an = actx.createAnalyser();
    an.fftSize = 512; an.smoothingTimeConstant = 0.94;
    analyserRef.current = an;
    const osc = actx.createOscillator(), gain = actx.createGain();
    oscRef.current = osc;
    osc.type = "sine"; osc.frequency.value = 160; gain.gain.value = 0.14;
    osc.connect(gain); gain.connect(an); osc.start();
    oscTimerRef.current = setInterval(() => {
      if (!actxRef.current) return;
      const now = actxRef.current.currentTime;
      osc.frequency.linearRampToValueAtTime(110 + Math.random()*90, now+0.65);
      gain.gain.linearRampToValueAtTime(0.05 + Math.random()*0.26, now+0.65);
    }, 750);
    return () => teardown();
  }, [voiceState, teardown]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) { rafRef.current = requestAnimationFrame(draw); return; }
    frameRef.current = (frameRef.current + 1) % 2;
    if (frameRef.current !== 0) { rafRef.current = requestAnimationFrame(draw); return; }

    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    let rawAmp = 0;
    if (analyserRef.current && isActive) {
      const buf = new Uint8Array(analyserRef.current.frequencyBinCount);
      analyserRef.current.getByteFrequencyData(buf);
      rawAmp = Math.min(1, Math.sqrt(buf.reduce((s,v)=>s+v*v,0)/buf.length)/100);
    }
    ampSmoothRef.current += (rawAmp - ampSmoothRef.current) * 0.18;
    const amp    = ampSmoothRef.current;
    const scheme = SCHEME[voiceState] || SCHEME.idle;
    const t      = Date.now() * 0.00045;
    const LANES  = [
      { speed:1.0,  ampMul:1.00, freq:1.80, alpha:isActive ? 0.80+amp*0.20 : 0.28, lw:2.2 },
      { speed:1.35, ampMul:0.65, freq:2.40, alpha:isActive ? 0.50+amp*0.35 : 0.15, lw:1.5 },
      { speed:0.70, ampMul:1.30, freq:1.20, alpha:isActive ? 0.30+amp*0.50 : 0.08, lw:1.0 },
      { speed:1.90, ampMul:0.40, freq:3.50, alpha:isActive ? 0.22+amp*0.38 : 0.05, lw:0.7 },
    ];
    const baseAmpPx = isActive ? 12 + amp*24 : 4;

    LANES.forEach((lane, li) => {
      const cr = li<2 ? scheme.r : scheme.accent[0];
      const cg = li<2 ? scheme.g : scheme.accent[1];
      const cb = li<2 ? scheme.b : scheme.accent[2];
      if (isActive && amp > 0.04) {
        ctx.save();
        ctx.shadowColor = `rgba(${cr},${cg},${cb},${lane.alpha*0.55})`;
        ctx.shadowBlur  = 16 + amp*12;
        ctx.strokeStyle = `rgba(${cr},${cg},${cb},${lane.alpha*0.35})`;
        ctx.lineWidth   = lane.lw * 3.2;
        ctx.lineCap     = "round";
        ctx.beginPath();
        for (let x=0; x<=W; x+=3) {
          const nx=x/W, env=Math.sin(nx*Math.PI);
          const y=H/2+Math.sin(nx*lane.freq*Math.PI*2+t*lane.speed*2.2+li*0.9)*baseAmpPx*lane.ampMul*env;
          x===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
        }
        ctx.stroke(); ctx.restore();
      }
      ctx.save();
      ctx.strokeStyle = `rgba(${cr},${cg},${cb},${lane.alpha})`;
      ctx.lineWidth = lane.lw; ctx.lineCap="round"; ctx.lineJoin="round";
      ctx.beginPath();
      for (let x=0; x<=W; x+=2) {
        const nx=x/W, env=Math.sin(nx*Math.PI);
        const y=H/2+Math.sin(nx*lane.freq*Math.PI*2+t*lane.speed*2.2+li*0.9)*baseAmpPx*lane.ampMul*env;
        x===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
      }
      ctx.stroke(); ctx.restore();
    });

    rafRef.current = requestAnimationFrame(draw);
  }, [isActive, voiceState]);

  useEffect(() => {
    rafRef.current = requestAnimationFrame(draw);
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
  }, [draw]);

  return (
    <canvas
      ref={canvasRef}
      className="vap-wave-canvas"
      width={640}
      height={56}
      aria-hidden="true"
    />
  );
}

/* ══════════════════════════════════════════════════
   FloatingParticles  — UNCHANGED
   ══════════════════════════════════════════════════ */
function FloatingParticles({ voiceState }) {
  const canvasRef = useRef(null);
  const rafRef    = useRef(null);
  const frameRef  = useRef(0);
  const ptsRef    = useRef([]);
  const isActive  = voiceState !== "idle";

  useEffect(() => {
    ptsRef.current = Array.from({ length:38 }, () => ({
      x:Math.random(), y:Math.random(),
      vx:(Math.random()-0.5)*0.00018,
      vy:(Math.random()-0.5)*0.00018-0.00009,
      r:0.8+Math.random()*2.2,
      alpha:0.05+Math.random()*0.18,
      t:Math.random()*Math.PI*2,
    }));
  }, []);

  const SCHEME = {
    idle:        [108, 155, 210],
    listening:   [220, 50,  50 ],
    transcribing:[234, 179, 8  ],
    generating:  [234, 179, 8  ],
    speaking:    [58,  107, 163],
  };

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) { rafRef.current = requestAnimationFrame(draw); return; }
    frameRef.current = (frameRef.current+1)%3;
    if (frameRef.current !== 0) { rafRef.current = requestAnimationFrame(draw); return; }
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0,0,W,H);
    const [r,g,b] = SCHEME[voiceState] || SCHEME.idle;
    const speedMul = isActive ? 2.2 : 1;
    const now = Date.now()*0.001;
    ptsRef.current.forEach(p => {
      p.t  += 0.008*speedMul;
      p.x  += p.vx*speedMul + Math.sin(p.t*0.7)*0.00006;
      p.y  += p.vy*speedMul;
      if (p.x<-0.05) p.x=1.05; if (p.x>1.05) p.x=-0.05;
      if (p.y<-0.05) p.y=1.05; if (p.y>1.05) p.y=-0.05;
      const pulse = 0.5+0.5*Math.sin(now*1.4+p.t);
      const alpha = p.alpha*(isActive ? 0.55+pulse*0.55 : 0.3);
      ctx.beginPath();
      ctx.arc(p.x*W, p.y*H, p.r*(isActive ? 1+pulse*0.6 : 1), 0, Math.PI*2);
      ctx.fillStyle = `rgba(${r},${g},${b},${alpha})`;
      ctx.fill();
    });
    rafRef.current = requestAnimationFrame(draw);
  }, [isActive, voiceState]);

  useEffect(() => {
    rafRef.current = requestAnimationFrame(draw);
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
  }, [draw]);

  return (
    <canvas
      ref={canvasRef}
      className="vap-particles-canvas"
      width={960} height={600}
      aria-hidden="true"
    />
  );
}

/* ══════════════════════════════════════════════════════════
   VoiceAgentPanel
   ══════════════════════════════════════════════════════════ */
const VoiceAgentPanel = ({
  voiceState, transcript, latestResponse,
  isRecording, micUnavailable, onToggleMic,
  isSpeaking, ttsProvider,
}) => {
  const isActive    = voiceState !== "idle";
  const isListening = voiceState === "listening";
  const isThinking  = voiceState === "generating" || voiceState === "transcribing";
  const isTalking   = voiceState === "speaking";

  /* typewriter */
  const [displayedResponse, setDisplayedResponse] = useState("");
  const typeTimerRef = useRef(null);
  useEffect(() => {
    if (!latestResponse) { setDisplayedResponse(""); return; }
    setDisplayedResponse("");
    let i = 0;
    clearInterval(typeTimerRef.current);
    typeTimerRef.current = setInterval(() => {
      i++;
      setDisplayedResponse(latestResponse.slice(0, i));
      if (i >= latestResponse.length) clearInterval(typeTimerRef.current);
    }, 14);
    return () => clearInterval(typeTimerRef.current);
  }, [latestResponse]);

  const transcriptLang = detectScriptLang(transcript);
  const responseLang   = detectScriptLang(latestResponse);

  const wrapCls = [
    "vap-avatar-system",
    isActive    ? "vap-active"    : "",
    isListening ? "vap-listening" : "",
    isTalking   ? "vap-speaking"  : "",
    isThinking  ? "vap-thinking"  : "",
  ].filter(Boolean).join(" ");

  const statusCls = [
    "vap-status-label",
    isListening ? "vap-status-listen" : "",
    isTalking   ? "vap-status-speak"  : "",
    isThinking  ? "vap-status-think"  : "",
  ].filter(Boolean).join(" ");

  return (
    <section className="vap-root" aria-live="polite">

      {/* ── Ambient layers ── */}
      <div className="vap-scanlines"      aria-hidden="true" />
      <div className="vap-orb vap-orb-1"  aria-hidden="true" />
      <div className="vap-orb vap-orb-2"  aria-hidden="true" />
      <div className="vap-orb vap-orb-3"  aria-hidden="true" />
      <div className="vap-orb vap-orb-4"  aria-hidden="true" />
      <div className="vap-particles-wrap" aria-hidden="true">
        <FloatingParticles voiceState={voiceState} />
      </div>
      <div className="vap-grid" aria-hidden="true" />

      {/* ════════════════════════════════════════════
          VERTICAL STACK — everything centred
          ════════════════════════════════════════════ */}
      <div className="vap-stack">

        {/* 1. AVATAR */}
        <div className={wrapCls}>
          <div className="vap-halo vap-halo-1" aria-hidden="true" />
          <div className="vap-halo vap-halo-2" aria-hidden="true" />
          <div className="vap-halo vap-halo-3" aria-hidden="true" />
          <div className="vap-pulse-ring" aria-hidden="true" />
          <div className="vap-pulse-ring" aria-hidden="true" />
          <div className="vap-pulse-ring" aria-hidden="true" />
          <div className="vap-pulse-ring" aria-hidden="true" />
          <div className="vap-avatar-frame" aria-label="AI Agent">
            <div className="vap-arc-ring vap-arc-ring-1" aria-hidden="true" />
            <div className="vap-arc-ring vap-arc-ring-2" aria-hidden="true" />
            <div className="vap-avatar-img-wrap">
              <div className="vap-avatar-placeholder">
                <svg viewBox="0 0 100 100" fill="none">
                  <circle cx="50" cy="36" r="20" fill="currentColor" opacity=".65" />
                  <ellipse cx="50" cy="82" rx="32" ry="20" fill="currentColor" opacity=".4" />
                </svg>
              </div>
            </div>
            <div className="vap-bracket vap-bracket-tl" aria-hidden="true" />
            <div className="vap-bracket vap-bracket-tr" aria-hidden="true" />
            <div className="vap-bracket vap-bracket-bl" aria-hidden="true" />
            <div className="vap-bracket vap-bracket-br" aria-hidden="true" />
            <div className="vap-avatar-shimmer"         aria-hidden="true" />
          </div>
        </div>

        {/* 2. WAVEFORM */}
        <div className="vap-waveform-wrap">
          <NeuralWaveform voiceState={voiceState} isSpeaking={isSpeaking} />
        </div>

        {/* 3. STATUS */}
        <p className={statusCls}>
          {isActive && <span className="vap-status-dot"   aria-hidden="true" />}
          <span className="vap-status-text">{stateLabel[voiceState] || stateLabel.idle}</span>
          {isActive && <span className="vap-status-dot vap-status-dot-r" aria-hidden="true" />}
        </p>

        {/* 4. CARDS */}
        <div className={`vap-cards-row${latestResponse ? "" : " single"}`}>

          <div className={`vap-card${transcript ? "" : " vap-card-muted"}`}>
            <div className="vap-card-label">
              <span className="vap-card-label-dot" />
              YOU SAID
            </div>
            <p className="vap-card-text" lang={transcriptLang}>
              {transcript || "Tap the microphone and speak your query..."}
            </p>
          </div>

          {latestResponse && (
            <div className="vap-card response">
              <div className="vap-card-label">
                <span className="vap-card-label-dot" />
                RESPONSE
              </div>
              <p className="vap-card-text" lang={responseLang}>
                {displayedResponse}
                {displayedResponse.length < latestResponse.length && (
                  <span className="vap-cursor" aria-hidden="true">▌</span>
                )}
              </p>
            </div>
          )}

        </div>

        {/* 5. CONTROLS */}
        <div className="vap-controls">

          {/* Provider pill */}
          <div className="vap-provider-chip">
            <span className="vap-provider-dot" />
            {ttsProvider === "elevenlabs"  ? "ElevenLabs Voice Engine" :
             ttsProvider === "sarvam"      ? "Sarvam Voice Engine"     :
             ttsProvider === "unavailable" ? "Voice Unavailable"       :
             "Detecting Engine..."}
          </div>

          {/* Mic button — neumorphic raised rounded-square, icon colour via CSS */}
          <button
            type="button"
            className={`vap-mic-btn${isRecording ? " vap-mic-active" : ""}`}
            onClick={onToggleMic}
            disabled={micUnavailable}
            aria-pressed={isRecording}
            aria-label={isRecording ? "Stop recording" : "Start recording"}
          >
            {/*
              vap-mic-wrap is exactly 68×68 — the same size as the core.
              Ripple rings and orbit are absolutely positioned inside it,
              so they radiate from the true centre of the square button.
            */}
            <div className="vap-mic-wrap">
              {/* Ripple rings */}
              <div className="vap-ripple"              aria-hidden="true" />
              <div className="vap-ripple vap-ripple-2" aria-hidden="true" />
              <div className="vap-ripple vap-ripple-3" aria-hidden="true" />
              {/* Orbit dashed ring */}
              <div className="vap-mic-orbit"           aria-hidden="true" />

              {/* Neumorphic core */}
              <div className="vap-mic-core">
                {isRecording ? (
                  <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
                    <rect
                      x="6" y="6" width="12" height="12" rx="2.5"
                      className="vap-mic-icon-fill"
                    />
                  </svg>
                ) : (
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <rect
                      x="9" y="2" width="6" height="11" rx="3"
                      className="vap-mic-icon-fill"
                    />
                    <path
                      d="M5 11a7 7 0 0 0 14 0"
                      strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"
                      className="vap-mic-icon-stroke"
                    />
                    <line
                      x1="12" y1="18" x2="12" y2="21"
                      strokeWidth="1.9" strokeLinecap="round"
                      className="vap-mic-icon-stroke"
                    />
                    <line
                      x1="9" y1="21" x2="15" y2="21"
                      strokeWidth="1.9" strokeLinecap="round"
                      className="vap-mic-icon-stroke"
                    />
                  </svg>
                )}
              </div>
            </div>{/* end vap-mic-wrap */}

            <span className="vap-mic-label">
              {isRecording ? "TAP TO STOP" : "TAP TO SPEAK"}
            </span>
          </button>

        </div>

      </div>{/* end vap-stack */}
    </section>
  );
};

export default VoiceAgentPanel;