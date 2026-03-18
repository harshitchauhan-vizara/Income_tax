/**
 * streamingTTS.js
 *
 * Sentence-boundary streaming TTS module.
 *
 * Flow:
 *   1. LLM tokens → feedToken() → sentence buffer
 *   2. Complete sentences → pending accumulator
 *   3. Pending sent to TTS only when >= MIN_CHUNK_LENGTH chars
 *      (prevents choppy audio from Sarvam's per-request latency)
 *   4. flush() at stream end sends any remaining text regardless of length
 *
 * Zero dependencies. No side-effects.
 */

const SENTENCE_END_RE  = /[.!?।]\s+|[.!?।]$/;
const MIN_SENTENCE_LEN = 25;   // ignore very short sentences like "OK." or "Rs."
const MIN_CHUNK_LENGTH = 120;  // batch until this many chars before calling Sarvam
const MAX_BUFFER_LEN   = 300;  // hard-flush buffer if no sentence end found

// Meta-text patterns that sometimes leak from the LLM into the stream
const META_STRIP_RE = [
  /analysis<\|message\|>/gi,
  /final<\|message\|>/gi,
  /<\|start\|>assistant/gi,
  /<\|end\|>/gi,
  /<\|channel\|>/gi,
  /<\|message\|>/gi,
  /<\|[^>]*\|?>/g,          // any remaining <|...|> tag
  /\banalysis\b/gi,         // bare "analysis" word that leaked
];

export class StreamingTTSBuffer {
  constructor({ onSentenceReady, language = "en" }) {
    this._onSentenceReady = onSentenceReady;
    this._language        = language;
    this._buffer          = "";
    this._pending         = "";
    this._flushed         = false;
  }

  setLanguage(lang) { this._language = lang; }

  feedToken(token) {
    if (!token) return;
    this._buffer += token;
    this._tryFlushSentences();
  }

  flush() {
    const remaining = this._buffer.trim();
    if (remaining) this._pending += (this._pending ? " " : "") + remaining;
    this._buffer = "";
    if (this._pending.trim().length > 4) this._emit(this._pending);
    this._pending = "";
    this._flushed = true;
  }

  reset() {
    this._buffer  = "";
    this._pending = "";
    this._flushed = false;
  }

  _tryFlushSentences() {
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const match = SENTENCE_END_RE.exec(this._buffer);
      if (!match) break;
      const endIdx   = match.index + match[0].length;
      const sentence = this._buffer.slice(0, endIdx).trim();
      this._buffer   = this._buffer.slice(endIdx);
      if (sentence.length >= MIN_SENTENCE_LEN) {
        this._pending += (this._pending ? " " : "") + sentence;
      } else {
        this._buffer = sentence + " " + this._buffer;
        break;
      }
    }

    // Hard-flush if buffer is very long with no sentence end
    if (this._buffer.length > MAX_BUFFER_LEN) {
      const cutoff  = this._buffer.lastIndexOf(" ", MAX_BUFFER_LEN);
      const splitAt = cutoff > 0 ? cutoff : MAX_BUFFER_LEN;
      const chunk   = this._buffer.slice(0, splitAt).trim();
      this._buffer  = this._buffer.slice(splitAt).trim();
      if (chunk) this._pending += (this._pending ? " " : "") + chunk;
    }

    // Only call TTS when batch is long enough for smooth speech
    if (this._pending.length >= MIN_CHUNK_LENGTH) {
      this._emit(this._pending);
      this._pending = "";
    }
  }

  _emit(text) {
    let cleaned = text;

    // Strip any leaked LLM meta-text (safety net)
    for (const re of META_STRIP_RE) cleaned = cleaned.replace(re, "");

    cleaned = cleaned
      .replace(/\s+/g, " ")
      .replace(/[|*_`#~]/g, "")
      .replace(/https?:\/\/\S+/g, "")
      .replace(/www\.\S+/g, "")
      .replace(/₹/g, " rupees ")
      .replace(/(\d+),(\d{2}),(\d{3})/g, (_, a, b, c) => `${a}${b}${c}`)
      .replace(/Section\s+\d+[A-Za-z]*/gi, "")
      .replace(/\d+%/g, (m) => m.replace("%", " percent"))
      .replace(/\bFY\s*\d{4}[-–]\d{2,4}\b/gi, "")
      .replace(/\(.*?\)/g, "")
      .replace(/\s{2,}/g, " ")
      .trim();

    if (cleaned.length > 4) {
      this._onSentenceReady(cleaned, this._language);
    }
  }
}