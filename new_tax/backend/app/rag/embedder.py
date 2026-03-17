from ..config import Settings


def build_embeddings(settings: Settings):
    if settings.embedding_provider.lower() == "openai":
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            model=settings.openai_embedding_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )

    try:
        from langchain_huggingface import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(model_name=settings.sentence_transformer_model)
    except Exception:  # pylint: disable=broad-except
        from langchain_community.embeddings import SentenceTransformerEmbeddings

        return SentenceTransformerEmbeddings(model_name=settings.sentence_transformer_model)
