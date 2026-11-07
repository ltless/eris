"""The plan a run keeps while it works.

A long run drops its older tool messages to stay inside the window, and the
first thing to go is the stretch where the model said what it was doing. The
plan is written down here instead, outside the conversation, and the engine
puts it back at the end of each round, so a run does not forget its own steps.
"""

from . import as_args

STATUSES = ("done", "doing", "todo")
MAX_STEPS = 20
MAX_CHARS = 200

TOOL = {
    "type": "function",
    "function": {
        "name": "plan",
        "description": (
            "Record the steps of the task and mark where each one stands. Call "
            "it again with the whole list whenever a step changes; the list "
            "replaces the old plan rather than adding to it. The plan is shown "
            "to the reader and repeated back to you on every round, so it does "
            "not belong in your reply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": (
                        "The whole plan, in order. Keep one step marked doing: "
                        "that is what the run is on now."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": {
                                "type": "string",
                                "description": "What to do, in a few words.",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(STATUSES),
                            },
                        },
                        "required": ["step", "status"],
                    },
                }
            },
            "required": ["steps"],
        },
    },
}


def read(args, ctx):
    """`(ok, text, reason, meta)`. The normalized list lands on the context the
    engine owns, which is what lets the next round read it back."""
    args = as_args(args)
    raw = args.get("steps")
    if not isinstance(raw, list) or not raw:
        return False, "No steps given.", "no steps were given", {}

    steps = []
    for item in raw[:MAX_STEPS]:
        if isinstance(item, dict):
            text = str(item.get("step", "") or "").strip()
            status = str(item.get("status", "") or "").strip().lower()
        else:
            text, status = str(item or "").strip(), "todo"
        if not text:
            continue
        steps.append({
            "step": text[:MAX_CHARS],
            "status": status if status in STATUSES else "todo",
        })

    # A list of empty strings is a call that said nothing, and storing it would
    # wipe a plan the run had already written down.
    if not steps:
        return False, "No step had any text.", "every step was empty", {}

    ctx.plan = tuple(steps)
    text = _render(steps)
    if len(raw) > MAX_STEPS:
        text += f"\nOnly the first {MAX_STEPS} steps were kept."
    return True, text, "", {"card": {"steps": steps}}


def _render(steps):
    done = sum(1 for s in steps if s["status"] == "done")
    lines = [f"Plan recorded ({done} of {len(steps)} done):"]
    for i, s in enumerate(steps, 1):
        lines.append(f"{i}. [{s['status']}] {s['step']}")
    return "\n".join(lines)


async def run(args, ctx):
    """Handler for the plan tool. Returns text for the model, never raises."""
    return read(args, ctx)[1]