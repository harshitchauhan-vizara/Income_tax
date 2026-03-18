import logging
import httpx
from datetime import datetime
from .config import Settings

logger = logging.getLogger("app.web_search.web_search_service")

class WebSearchService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = getattr(settings, 'web_search_api_key', '')
        self.max_results = getattr(settings, 'web_search_max_results', 5)
        self.timeout = getattr(settings, 'web_search_timeout', 30.0)

        if not self.api_key:
            logger.warning("Gemini API key not configured. Web search will be disabled.")
            self.enabled = False
        else:
            self.enabled = True

    async def search(self, query: str) -> str:
        if not self.enabled:
            logger.info("Web search disabled - no Gemini API key configured")
            return ""
        try:
            return await self._gemini_search(query)
        except Exception as exc:
            logger.error("Gemini web search failed: %s", exc)
            return ""

    async def _gemini_search(self, query: str) -> str:
        endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                f"Provide factual, up-to-date information about the following query. "
                                f"Return only relevant facts — no extra commentary.\n\nQuery: {query}"
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 512,
            }
        }

        params = {"key": self.api_key}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(endpoint, json=payload, params=params)
            response.raise_for_status()
            data = response.json()

            # Extract text from Gemini response
            candidates = data.get("candidates", [])
            if not candidates:
                logger.info("No Gemini results for query: %s", query)
                return ""

            parts = candidates[0].get("content", {}).get("parts", [])
            text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()

            if text:
                context = (
                    f"[Web Search Results - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n\n"
                    f"{text}"
                )
                logger.info("Gemini web search returned results for query: %s", query)
                return context

            logger.info("Gemini returned empty results for query: %s", query)
            return ""