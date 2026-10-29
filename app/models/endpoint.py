"""Where the model server lives.

Two route families share one host, and the difference matters: the native
`/api/...` routes take everything except the model list, which is OpenAI-shaped
and sits behind the `/v1` suffix the base URL already carries. Both the
transport and the ladder build their URLs from here, so neither has to know
which family the other is on.
"""

from ..config import settings


def base_url():
    """The server root, with the OpenAI-style suffix off.

    MODEL_BASE_URL keeps a `/v1` suffix for the model list; every other route
    here — chat, the loaded-model report, the model's own metadata — is on the
    native API, and a path appended to `/v1` would be a 404.
    """
    base = settings.model_base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def chat_url():
    """The model talks on the native /api/chat path, framed as NDJSON."""
    return base_url() + "/api/chat"
