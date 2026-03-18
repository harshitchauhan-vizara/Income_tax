import re


HINDI_RE = re.compile(r"[\u0900-\u097F]")
TAMIL_RE = re.compile(r"[\u0B80-\u0BFF]")


LANGUAGE_CODE_MAP = {
    "en": "en",
    "hi": "hi",
    "ta": "ta",
}

# Common Hindi romanised words that indicate Hindi intent even without Devanagari script
HINGLISH_KEYWORDS = {
    "kya", "hai", "hain", "mera", "meri", "mujhe", "aap", "tum", "kaise",
    "kyun", "kab", "kahan", "kaisa", "theek", "nahi", "nahin", "hoga",
    "chahiye", "batao", "bataiye", "samjhao", "matlab", "paisa", "paise",
    "kitna", "kitne", "lagega", "milega", "bharana", "bharna", "tax",
    "income", "kamai", "salary", "naukri", "vyapar", "dhandha",
}


def detect_supported_language(text: str, fallback: str = "en") -> str:
    """
    Detect language from text.
    Priority: Tamil script > Hindi script > Hinglish keywords > fallback > English
    """
    if not text or not text.strip():
        return fallback if fallback in LANGUAGE_CODE_MAP else "en"

    # Script-based detection (most reliable)
    if TAMIL_RE.search(text):
        return "ta"
    if HINDI_RE.search(text):
        return "hi"

    # Hinglish detection — romanised Hindi words
    words = set(re.findall(r"\b\w+\b", text.lower()))
    if words & HINGLISH_KEYWORDS:
        return "hi"

    # Trust the caller's fallback if it's a valid language
    if fallback in LANGUAGE_CODE_MAP:
        return fallback

    return "en"


def map_tts_lang(language: str) -> str:
    return LANGUAGE_CODE_MAP.get(language, "en")