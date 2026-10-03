import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    model_base_url = os.getenv("MODEL_BASE_URL", "http://localhost:8080/v1").rstrip("/")
    model_name = os.getenv("MODEL_NAME", "local-model")
    model_api_key = os.getenv("MODEL_API_KEY", "sk-no-key")
    embed_model = os.getenv("EMBED_MODEL", "local-model")

    chroma_host = os.getenv("CHROMA_HOST", "localhost")
    chroma_port = int(os.getenv("CHROMA_PORT", "8000"))
    chroma_collection = os.getenv("CHROMA_COLLECTION", "eris")

    searxng_url = os.getenv("SEARXNG_URL", "http://localhost:8082").rstrip("/")
    searxng_top_k = int(os.getenv("SEARXNG_TOP_K", "4"))
    searxng_language = os.getenv("SEARXNG_LANGUAGE", "en")
    searxng_time_range = os.getenv("SEARXNG_TIME_RANGE", "")

    rag_top_k = int(os.getenv("RAG_TOP_K", "4"))
    system_prompt = os.getenv(
        "SYSTEM_PROMPT",
        "You are Eris, a helpful assistant. Answer clearly and use markdown when useful.",
    )


settings = Settings()