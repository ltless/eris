"""On-demand document search: let the model re-query the user's own docs.

The turn's one-shot RAG pass uses the raw user message. This reuses the same
per-user retrieve but is called by the model, with its own query, in a round —
same multi-round contract as web_search. Silent: results ride the existing
sources chips, so the UI invents no extra row.
"""

from .. import rag
from . import as_args

TOOL = {
    "type": "function",
    "function": {
        "name": "doc_search",
        "description": (
            "Search the user's uploaded documents. Use it to find more or "
            "different passages than the context already given, e.g. when "
            "comparing two files or the first hits miss."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to find in the documents."},
            },
            "required": ["query"],
        },
    },
}


def render(results, start=0):
    if not results:
        return (
            "No relevant document passages for this query. Say you could not "
            "find it in the uploaded documents; do not invent it."
        )
    head = "Document passages:\n\n"
    return head + "\n\n".join(
        f"[{start + i + 1}] ({r['source']})\n{r['text']}" for i, r in enumerate(results)
    )


async def run(user_id, args):
    args = as_args(args)
    query = str(args.get("query", "") or "").strip()
    if not query:
        return []
    try:
        return await rag.retrieve(user_id, query)
    except Exception:
        return []
