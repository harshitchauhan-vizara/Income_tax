# Multilingual Voice + Chat RAG System

Production-ready modular implementation using **FastAPI + React (Vite) + WebSocket + Whisper + LangChain/Chroma + gTTS**.

## Features

- Text chat and voice chat (audio chunk streaming)
- Multilingual support (English/Hindi/Tamil)
- RAG contextual answering with strict grounded system prompt
- Session memory per websocket connection
- Token streaming over WebSocket
- TTS audio streaming back to client
- Reconnect-capable WebSocket frontend client
- Rate limiting and structured logging

## Project Structure

```bash
backend/
  app/
    main.py
    websocket_manager.py
    config.py
    asr/whisper_service.py
    rag/{embedder,vectorstore,retriever,rag_pipeline}.py
    llm/llm_service.py
    tts/gtts_service.py
    utils/language_detector.py
  requirements.txt
  Dockerfile

frontend/
  src/
    App.jsx
    components/{ChatWindow,MicRecorder,AudioPlayer}.jsx
    services/websocket.js
    hooks/useAudioStream.js
```

## Configuration

1. Keep root `config.yaml` (already supported).
2. Create env from template:

```bash
cp backend/.env.example .env
```

No secrets are hardcoded in runtime if env overrides are set.

## Run Backend Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8111
```

## Run Frontend Locally

```bash
cd frontend
npm install
npm run dev
```

Set custom websocket URL if needed:

```bash
VITE_WS_URL=ws://localhost:8111/ws
```

## Docker (Backend)

```bash
cd backend
docker build -t voice-chat-rag-backend .
docker run --rm -p 8111:8111 --env-file ../.env voice-chat-rag-backend
```

## Notes

- Add domain docs in `backend/data/knowledge/*.md|*.txt`.
- gTTS can later be swapped with ElevenLabs by replacing `app/tts` service only.
- LLM backend is swappable via config (`base_url`, model, key).

## Troubleshooting (ASR not working)

If voice input is not transcribed and you see fallback answers like "I do not know based on the provided context", verify ASR health:

```bash
curl http://localhost:8111/health/asr
```

You should see `"available": true` for ASR.

If `available` is `false`, install backend dependencies in the same environment used to run uvicorn:

```bash
pip install -r backend/requirements.txt
```

Key packages needed for voice path:
- `faster-whisper`
- `ffmpeg` (system package)

When ASR is unavailable or transcription fails, backend now emits an `asr_error` WebSocket event and frontend displays the exact error reason.

## Troubleshooting (RAG not working / fallback retrieval)

If startup logs show errors like `No module named 'langchain_community'`, install missing RAG dependencies in the same active environment:

```bash
pip install -r backend/requirements.txt
```

Or install only the missing packages quickly:

```bash
pip install langchain langchain-community chromadb sentence-transformers
```

When these are missing, backend runs in fallback mode (no vector retrieval), so answers may default to "I do not know based on the provided context.".