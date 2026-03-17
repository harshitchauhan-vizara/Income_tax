import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(
            str(PROJECT_DIR / ".env"),
            str(PROJECT_DIR / ".env.example"),
            str(WORKSPACE_DIR / "backend" / ".env"),
            str(WORKSPACE_DIR / "backend" / ".env.example"),
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Voice Chat RAG"
    debug: bool = Field(default=False)
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8111)

    cors_origins: str = Field(default="*")
    session_memory_size: int = Field(default=8)
    rate_limit_per_minute: int = Field(default=60)

    whisper_model_size: str = Field(default="large-v3")
    whisper_compute_type: str = Field(default="int8")
    whisper_confidence_threshold: float = Field(default=0.35)

    embedding_provider: str = Field(default="sentence_transformers")
    openai_embedding_model: str = Field(default="text-embedding-3-small")
    sentence_transformer_model: str = Field(default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

    chroma_persist_dir: str = Field(default=str(WORKSPACE_DIR / "backend" / "data" / "chroma"))
    knowledge_dir: str = Field(default=str(WORKSPACE_DIR / "backend" / "data" / "knowledge"))
    retriever_top_k: int = Field(default=4)

    llm_provider: str = Field(default="openai_compatible")
    llm_base_url: str = ""
    llm_model_name: str = ""
    llm_api_key: str = ""
    llm_temperature: float = 0.3
    llm_max_tokens: int = 1024
    llm_verify_ssl: bool = True

    # ── Sarvam TTS ──────────────────────────────────────────────────────────
    sarvam_api_key: str = ""

    # Use bulbul:v2 — it supports pitch + loudness for natural human-like voice.
    # bulbul:v3 ignores pitch and loudness entirely.
    sarvam_model: str = Field(default="bulbul:v2")

    # Default speaker (used when no language-specific speaker is resolved)
    # Valid speakers (from Sarvam API):
    # Female: anushka, manisha, vidya, arya, ritu, priya, neha, pooja, simran, kavya,
    #         ishita, shreya, roopa, amelia, sophia, tanya, shruti, suhani, kavitha, rupali
    # Male:   abhilash, karun, hitesh, aditya, rahul, rohan, amit, dev, ratan, varun,
    #         manan, sumit, kabir, aayan, shubh, ashutosh, advait, anand, tarun, sunny,
    #         mani, gokul, vijay, mohit, rehan, soham
    sarvam_speaker: str = Field(default="shubh")      # warm, clear female — good default

    # Per-language speaker overrides
    # ishita / priya → warm female, natural for Hindi guidance
    # ritu → clear female, good for English
    # Set to "" to fall back to sarvam_speaker default
    sarvam_speaker_en: str = Field(default="shubh")     # English — clear female
    sarvam_speaker_hi: str = Field(default="shubh")   # Hindi   — warm female
    sarvam_speaker_ta: str = Field(default="shubh")   # Tamil

    sarvam_language_code: str = Field(default="hi-IN")
    sarvam_target_sample_rate: int = Field(default=22050)   # 22050 = highest quality WAV

    # ── Human-like voice parameters (bulbul:v2 only) ──────────────────────
    # pitch: -0.1 to +0.1 — slight positive lift makes voice warmer and less robotic
    sarvam_pitch: float = Field(default=0.05)

    # pace: 0.5–2.0 — 0.85 feels natural for explanatory speech (not hurried, not slow)
    sarvam_speech_rate: float = Field(default=0.85)

    # loudness: 1.0–3.0 — 1.2 is clear without sounding loud or broadcast-y
    sarvam_loudness: float = Field(default=1.2)
    # ────────────────────────────────────────────────────────────────────────

    # ── Web Search ───────────────────────────────────────────────────────────
    web_search_api_key: str = Field(default="")
    web_search_provider: str = Field(default="gemini")
    web_search_max_results: int = Field(default=5)
    web_search_timeout: float = Field(default=30.0)
    google_search_engine_id: str = Field(default="")
    # ────────────────────────────────────────────────────────────────────────


def _load_yaml_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _resolve_config_path() -> Path | None:
    env_path = os.getenv("CONFIG_PATH")
    if env_path:
        candidate = Path(env_path).expanduser().resolve()
        if candidate.exists():
            return candidate

    candidates = [
        PROJECT_DIR / "config.yaml",
        WORKSPACE_DIR / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    config_path = _resolve_config_path()
    yaml_config = _load_yaml_config(config_path) if config_path else {}
    llm_cfg = yaml_config.get("llm", {})

    settings.llm_base_url    = os.getenv("LLM_BASE_URL",    llm_cfg.get("base_url",   settings.llm_base_url))
    settings.llm_model_name  = os.getenv("LLM_MODEL_NAME",  llm_cfg.get("model_name", settings.llm_model_name))
    settings.llm_api_key     = os.getenv("LLM_API_KEY",     llm_cfg.get("api_key",    settings.llm_api_key))
    settings.llm_temperature = float(os.getenv("LLM_TEMPERATURE", llm_cfg.get("temperature", settings.llm_temperature)))
    settings.llm_max_tokens  = int(os.getenv("LLM_MAX_TOKENS",    llm_cfg.get("max_tokens",  settings.llm_max_tokens)))
    settings.llm_verify_ssl  = str(os.getenv("LLM_VERIFY_SSL",    llm_cfg.get("verify_ssl",  settings.llm_verify_ssl))).lower() in {
        "1", "true", "yes",
    }

    return settings