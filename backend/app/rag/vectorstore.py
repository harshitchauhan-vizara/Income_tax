import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from ..config import Settings
from .embedder import build_embeddings

logger = logging.getLogger("app.rag.vectorstore")


class _FallbackVectorStore:
    def similarity_search(self, _query: str, _k: int = 4) -> list:
        return []

    def add_documents(self, _docs: list) -> None:
        return None

    def persist(self) -> None:
        return None


class VectorStoreService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.persist_dir = Path(settings.chroma_persist_dir)
        self.knowledge_dir = Path(settings.knowledge_dir)

        self.vectordb: Any = _FallbackVectorStore()
        self._chroma_available = False

        os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        try:
            try:
                from langchain_chroma import Chroma
            except Exception:  # pylint: disable=broad-except
                from langchain_community.vectorstores import Chroma

            self.embeddings = build_embeddings(settings)
            self.vectordb = Chroma(
                persist_directory=str(self.persist_dir),
                embedding_function=self.embeddings,
                collection_name="knowledge_base",
            )
            self._chroma_available = True
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Chroma/LangChain not available, running with fallback retriever: %s", exc)

    async def ensure_index(self) -> None:
        await asyncio.to_thread(self._ensure_index_sync)

    def _ensure_index_sync(self) -> None:
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        if not self._chroma_available:
            return

        docs = self._load_knowledge_docs()
        if not docs:
            return

        existing_sources: set[str] = set()
        try:
            existing = self.vectordb._collection.get(include=["metadatas"])  # pylint: disable=protected-access
            for metadata in existing.get("metadatas", []) or []:
                if isinstance(metadata, dict) and metadata.get("source"):
                    existing_sources.add(str(metadata["source"]))
        except Exception:  # pylint: disable=broad-except
            existing_sources = set()

        docs_to_add = [doc for doc in docs if str(doc.metadata.get("source", "")) not in existing_sources]
        if docs_to_add:
            self.vectordb.add_documents(docs_to_add)
            self.vectordb.persist()

    def _load_knowledge_docs(self) -> list:
        docs: list = []
        try:
            from langchain_core.documents import Document
        except Exception:  # pylint: disable=broad-except
            return docs

        text_files = list(self.knowledge_dir.rglob("*.txt")) + list(self.knowledge_dir.rglob("*.md"))
        for text_file in text_files:
            text = text_file.read_text(encoding="utf-8", errors="ignore").strip()
            if not text:
                continue
            docs.append(Document(page_content=text, metadata={"source": str(text_file)}))

        if not docs:
            docs.append(
                Document(
                    page_content=(
                        "No knowledge files are loaded yet. "
                        "Upload data to backend/data/knowledge as .txt or .md files for RAG responses."
                    ),
                    metadata={"source": "system_seed"},
                )
            )
        return docs
