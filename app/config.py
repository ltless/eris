"""Application configuration from environment variables."""

import os
from pathlib import Path

# Load the repo-root .env no matter which directory uvicorn starts from.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class Settings:
    model_base_url = os.getenv("MODEL_BASE_URL", "http://localhost:8080/v1").rstrip("/")
    model_name = os.getenv("MODEL_NAME", "local-model")
    model_api_key = os.getenv("MODEL_API_KEY", "sk-no-key")

    searxng_url = os.getenv("SEARXNG_URL", "http://localhost:8082").rstrip("/")
    searxng_top_k = int(os.getenv("SEARXNG_TOP_K", "4"))
    searxng_language = os.getenv("SEARXNG_LANGUAGE", "en")
    searxng_time_range = os.getenv("SEARXNG_TIME_RANGE", "")

    fetch_pages = os.getenv("FETCH_PAGES", "1") == "1"
    fetch_top = int(os.getenv("FETCH_TOP", "3"))
    fetch_max_chars = int(os.getenv("FETCH_MAX_CHARS", "6000"))
    # What one tool call may hand the model. A search that fetched FETCH_TOP
    # full pages is the largest single result, so this has to clear that sum
    # plus the snippets, or the third page arrives cut.
    tool_output_chars = int(os.getenv("TOOL_OUTPUT_CHARS", "30000"))
    # The type the model server was started with for its KV cache. Ollama
    # accepts this only as a server-wide setting and never reports it back, so
    # this has to mirror it by hand: set the same value here and in
    # OLLAMA_KV_CACHE_TYPE, or the panel's KV figure is off by the ratio.
    kv_cache_type = os.getenv("KV_CACHE_TYPE", "f16")
    max_tool_rounds = int(os.getenv("MAX_TOOL_ROUNDS", "3"))
    # Rounds the model may earn past the allowance by finding new sources.
    extra_tool_rounds = int(os.getenv("EXTRA_TOOL_ROUNDS", "4"))
    system_prompt = os.getenv(
        "SYSTEM_PROMPT",
        "You are Eris, a helpful assistant. Answer clearly and use markdown when useful.",
    )


settings = Settings()