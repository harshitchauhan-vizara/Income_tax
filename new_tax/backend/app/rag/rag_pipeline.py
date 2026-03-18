from collections import defaultdict, deque
from collections.abc import AsyncGenerator
import logging
import re

from ..config import Settings
from ..llm.llm_service import LLMService, _is_income_tax_query, detect_language
from .retriever import RetrieverService
from ..web_search_service import WebSearchService

logger = logging.getLogger("app.rag.pipeline")


# ---------------------------------------------------------------------------
# INCOME TAX SMALLTALK REPLIES — English and Hindi only
# FY 2026-27 | AY 2027-28 | Finance Act, 2026
# ---------------------------------------------------------------------------

_SMALLTALK_GREETINGS_EN = (
    "Hello! I'm TaxBot — your Income Tax assistant for Financial Year two thousand twenty six "
    "to twenty seven (Assessment Year two thousand twenty seven to twenty eight). "
    "Ask me anything about tax slabs, ITR filing, deductions, TDS, capital gains, and more."
)
_SMALLTALK_GREETINGS_HI = (
    "नमस्ते! मैं TaxBot हूँ — वित्त वर्ष दो हजार छब्बीस से सत्ताईस का आयकर सहायक। "
    "कर स्लैब, ITR दाखिल करना, कटौतियाँ, TDS, पूँजीगत लाभ आदि के बारे में पूछें।"
)

_SMALLTALK_HOW_ARE_YOU_EN = (
    "I'm doing great, thank you! I'm ready to help you with any income tax questions for "
    "Financial Year two thousand twenty six to twenty seven. What would you like to know?"
)
_SMALLTALK_HOW_ARE_YOU_HI = (
    "मैं ठीक हूँ, धन्यवाद! वित्त वर्ष दो हजार छब्बीस से सत्ताईस से संबंधित आयकर के "
    "किसी भी प्रश्न के लिए मैं यहाँ हूँ। आप क्या जानना चाहते हैं?"
)

# ---------------------------------------------------------------------------
# EXPANDED HINGLISH DETECTION
# Catches Roman-script Hindi that detect_language() in llm_service may miss.
# ---------------------------------------------------------------------------

_HINGLISH_PATTERN = re.compile(
    r"\b("
    # question / info words
    r"kya|kitna|kitni|kaisa|kaise|kyun|kyunki|kab|kahan|kaun|kaunsa|kaunsi|konsa|konsi|"
    # action / instruction words
    r"batao|bataye|batana|samjhao|samjhaye|dikhao|bolo|karo|karein|"
    r"lagega|lagegi|hoga|hogi|hain|hai|tha|thi|ho|"
    # personal pronouns / possessives
    r"mujhe|mujhko|mera|meri|mere|tumhara|tumhari|aapka|aapki|"
    r"apna|apni|apne|"
    # common nouns / terms
    r"rupaye|paisa|paise|wala|wali|wale|"
    r"salary|income|tax|return|"
    # compound phrases (written as single tokens after space normalisation)
    r"bhai|yaar|dost|"
    # prepositions / postpositions
    r"pe|par|mein|se|ko|ka|ki|ke|"
    # regime / tax phrases
    r"naya|purana|nai|nayi|"
    r"lakh|crore|"
    # filler / connector
    r"toh|aur|lekin|ya\b|"
    r"lagta|lagti|chahiye|chahte|chahti|"
    r"mere\s+liye|mere\s+liye"
    r")\b",
    re.IGNORECASE,
)

_HINGLISH_MIN_MATCHES = 2  # require at least 2 Hinglish tokens to classify as Hindi


def _detect_language_extended(text: str) -> str:
    """
    Extended language detection used by the pipeline.
    Falls back to llm_service.detect_language first, then applies broader
    Hinglish pattern matching to catch Roman-script Hindi that the narrower
    regex in llm_service misses.
    """
    lang = detect_language(text)
    if lang == "hi":
        return "hi"
    # Count Hinglish token matches in the query
    matches = _HINGLISH_PATTERN.findall(text)
    if len(matches) >= _HINGLISH_MIN_MATCHES:
        return "hi"
    return "en"


class RAGPipeline:
    def __init__(
        self,
        settings: Settings,
        retriever: RetrieverService,
        llm_service: LLMService,
    ) -> None:
        self.settings = settings
        self.retriever = retriever
        self.llm_service = llm_service
        self.web_search_service = WebSearchService(settings)
        self.memory: dict[str, deque[dict]] = defaultdict(
            lambda: deque(maxlen=settings.session_memory_size * 2)
        )
        self.response_source: dict[str, str] = {}

    @staticmethod
    def _smalltalk_reply(query: str, language_hint: str) -> str | None:
        q_norm = re.sub(r"[^\w\s]", "", query.strip().lower())
        words = set(q_norm.split())
        lang = str(language_hint).lower()

        question_words = {
            "who", "what", "when", "where", "why", "how", "which",
            "is", "are", "does", "did", "will", "can", "should",
        }
        if words & question_words:
            return None

        greeting_words = {"hi", "hello", "hey", "namaste", "namaskar"}
        how_are_you_phrases = (
            "how are you", "how r u", "how are u",
            "kaisa ho", "kaisi ho", "kaise ho", "kaise hain", "kaisa hoon",
        )

        is_greeting = bool(words & greeting_words) and len(words) <= 6
        is_how_are_you = any(p in q_norm for p in how_are_you_phrases) and len(words) <= 8

        if "नमस्ते" in query or "नमस्कार" in query:
            is_greeting = True
        if "कैसे हैं" in query or "कैसे हो" in query:
            is_how_are_you = True

        if is_how_are_you:
            return _SMALLTALK_HOW_ARE_YOU_HI if lang == "hi" else _SMALLTALK_HOW_ARE_YOU_EN
        if is_greeting:
            return _SMALLTALK_GREETINGS_HI if lang == "hi" else _SMALLTALK_GREETINGS_EN
        return None

    async def prepare_context(self, query: str) -> tuple[str, str]:
        if _is_income_tax_query(query):
            return (
                "[Income Tax query — answer from the Knowledge Base in the system prompt. "
                "FY 2026-27 / AY 2027-28 / Income Tax Act, 2025 / Finance Act, 2026.]",
                "knowledge_base",
            )

        logger.info("Non-tax query, attempting web search: %s", query)
        web_context = await self.web_search_service.search(query)
        if web_context:
            return web_context, "web"

        return (
            "No web search results available. "
            "Answer this question using your own general knowledge. "
            "Be concise and accurate.",
            "general",
        )

    async def answer_stream(
        self,
        query: str,
        session_id: str,
        language_hint: str = "en",
    ) -> AsyncGenerator[str, None]:
        # Use the extended detector so Hinglish (Roman-script Hindi) is caught
        # even when it isn't matched by the narrower regex in llm_service.
        detected = _detect_language_extended(query)
        lang = detected if detected != "en" else str(language_hint).lower()

        smalltalk = self._smalltalk_reply(query, lang)
        if smalltalk:
            logger.info("Routing to smalltalk session=%s query='%s'", session_id, query)
            self.response_source[session_id] = "general"
            for token in smalltalk.split(" "):
                yield f"{token} "
            self._update_memory(session_id, query, smalltalk)
            return

        context, source = await self.prepare_context(query)
        logger.info("Routing to %s session=%s query='%s'", source, session_id, query)
        self.response_source[session_id] = source

        history = list(self.memory[session_id])
        output_tokens: list[str] = []

        # Pass the resolved `lang` so llm_service doesn't re-detect and override it.
        async for token in self.llm_service.stream_chat_completion(
            context=context,
            query=query,
            history=history,
            language_hint=lang,
        ):
            output_tokens.append(token)
            yield token

        answer = "".join(output_tokens).strip() or "I do not have that information right now."
        self._update_memory(session_id, query, answer)

    def _update_memory(self, session_id: str, query: str, answer: str) -> None:
        self.memory[session_id].append({"role": "user", "content": query})
        self.memory[session_id].append({"role": "assistant", "content": answer})

    def get_last_source(self, session_id: str) -> str:
        return self.response_source.get(session_id, "knowledge_base")

    def reset_session(self, session_id: str) -> None:
        self.memory.pop(session_id, None)
        self.response_source.pop(session_id, None)