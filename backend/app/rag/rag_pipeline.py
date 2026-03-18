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
# ---------------------------------------------------------------------------

_SMALLTALK_GREETINGS_EN = (
    "Hello! I'm TaxBot — your Income Tax Act 2025 assistant. "
    "Ask me anything about tax slabs, ITR filing, deductions, TDS, capital gains, and more."
)
_SMALLTALK_GREETINGS_HI = (
    "नमस्ते! मैं TaxBot हूँ — आयकर अधिनियम 2025 का सहायक। "
    "कर स्लैब, ITR दाखिल करना, कटौतियाँ, TDS, पूँजीगत लाभ आदि के बारे में पूछें।"
)

_SMALLTALK_HOW_ARE_YOU_EN = (
    "I'm doing great, thank you! I'm ready to help you with any income tax questions. "
    "What would you like to know?"
)
_SMALLTALK_HOW_ARE_YOU_HI = (
    "मैं ठीक हूँ, धन्यवाद! आयकर से संबंधित किसी भी प्रश्न के लिए मैं यहाँ हूँ। "
    "आप क्या जानना चाहते हैं?"
)


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
        # session_id → deque of {"role": ..., "content": ...}
        self.memory: dict[str, deque[dict]] = defaultdict(
            lambda: deque(maxlen=settings.session_memory_size * 2)
        )
        self.response_source: dict[str, str] = {}

    # ------------------------------------------------------------------
    # SMALLTALK DETECTION
    # Handles greetings / social phrases only.
    # Returns a reply string if it is pure smalltalk, else None.
    # ------------------------------------------------------------------
    @staticmethod
    def _smalltalk_reply(query: str, language_hint: str) -> str | None:
        q_norm = re.sub(r"[^\w\s]", "", query.strip().lower())
        words = set(q_norm.split())
        lang = str(language_hint).lower()

        # If query contains real question words, let it through to the main pipeline
        question_words = {
            "who", "what", "when", "where", "why", "how", "which",
            "is", "are", "does", "did", "will", "can", "should",
        }
        if words & question_words:
            return None

        greeting_words = {"hi", "hello", "hey", "namaste", "namaskar"}
        how_are_you_phrases = (
            "how are you", "how r u", "how are u",
            "kaisa ho", "kaise ho", "kaise hain",
        )

        is_greeting = bool(words & greeting_words) and len(words) <= 6
        is_how_are_you = any(p in q_norm for p in how_are_you_phrases) and len(words) <= 8

        # Hindi Devanagari patterns
        if "नमस्ते" in query or "नमस्कार" in query:
            is_greeting = True
        if "कैसे हैं" in query or "कैसे हो" in query:
            is_how_are_you = True

        if is_how_are_you:
            return _SMALLTALK_HOW_ARE_YOU_HI if lang == "hi" else _SMALLTALK_HOW_ARE_YOU_EN

        if is_greeting:
            return _SMALLTALK_GREETINGS_HI if lang == "hi" else _SMALLTALK_GREETINGS_EN

        return None

    # ------------------------------------------------------------------
    # CONTEXT PREPARATION
    # For income tax queries: returns a minimal context marker so the LLM
    # knows to use the hardcoded knowledge base in its system prompt.
    # For non-tax queries: fetches web search results.
    # ------------------------------------------------------------------
    async def prepare_context(self, query: str) -> tuple[str, str]:
        if _is_income_tax_query(query):
            # The full knowledge base is already in the system prompt.
            # Return a lightweight marker so the LLM uses it.
            return "[Income Tax query — answer from the Knowledge Base in the system prompt.]", "knowledge_base"

        # Non-income-tax query → web search
        logger.info("Non-tax query, attempting web search: %s", query)
        web_context = await self.web_search_service.search(query)
        if web_context:
            logger.info("Web search results obtained for query: %s", query)
            return web_context, "web"

        # Web search returned nothing — instruct LLM to use its own knowledge
        logger.info("Web search returned no results for query: %s", query)
        return (
            "No web search results available. "
            "Answer this question using your own general knowledge. "
            "Be concise and accurate.",
            "general",
        )

    # ------------------------------------------------------------------
    # MAIN STREAMING ANSWER
    # ------------------------------------------------------------------
    async def answer_stream(
        self,
        query: str,
        session_id: str,
        language_hint: str = "en",
    ) -> AsyncGenerator[str, None]:
        # Auto-detect language from query text — overrides frontend hint if
        # Devanagari or Tamil script is found. This is the single source of
        # truth for language used by ALL downstream steps (smalltalk, LLM).
        detected = detect_language(query)
        lang = detected if detected != "en" else str(language_hint).lower()

        # 1. Smalltalk — greetings and social phrases
        smalltalk = self._smalltalk_reply(query, lang)
        if smalltalk:
            logger.info("Routing to smalltalk session=%s query='%s'", session_id, query)
            self.response_source[session_id] = "general"
            for token in smalltalk.split(" "):
                yield f"{token} "
            self._update_memory(session_id, query, smalltalk)
            return

        # 2. Prepare context (KB marker for tax, web search for non-tax)
        context, source = await self.prepare_context(query)
        logger.info("Routing to %s session=%s query='%s'", source, session_id, query)
        self.response_source[session_id] = source

        # 3. Stream LLM response
        history = list(self.memory[session_id])
        output_tokens: list[str] = []

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

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------
    def _update_memory(self, session_id: str, query: str, answer: str) -> None:
        self.memory[session_id].append({"role": "user", "content": query})
        self.memory[session_id].append({"role": "assistant", "content": answer})

    def get_last_source(self, session_id: str) -> str:
        return self.response_source.get(session_id, "knowledge_base")

    def reset_session(self, session_id: str) -> None:
        self.memory.pop(session_id, None)
        self.response_source.pop(session_id, None)