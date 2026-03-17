import asyncio

from ..config import Settings
from .vectorstore import VectorStoreService


class RetrieverService:
    def __init__(self, settings: Settings, vectorstore: VectorStoreService) -> None:
        self.settings = settings
        self.vectorstore = vectorstore

    async def retrieve(self, query: str) -> list:
        return await asyncio.to_thread(
            self.vectorstore.vectordb.similarity_search,
            query,
            self.settings.retriever_top_k,
        )
