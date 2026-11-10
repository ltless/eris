"""The plan a run keeps while it works.

A long run drops its older tool messages to stay inside the window, and the
first thing to go is the stretch where the model said what it was doing. The
plan is written down here instead, outside the conversation, and the engine
puts it back at the end of each round, so a run does not forget its own steps.
"""

from ..errors import TOOL_ERROR
from ..security import READ_ONLY
from .registry import TAGS, Tool, ToolResult, missing_arg, registry

STATUSES = ("done", "doing", "todo")
MAX_STEPS = 20
MAX_CHARS = 200
NAME = "plan"
TIMEOUT_S = 10.0

DESCRIPTION = (
    "Record the steps of the task and mark where each one stands. Call "
    "it again with the whole list whenever a step changes; the list "
    "replaces the old plan rather than adding to it. The plan is shown "
    "to the reader and repeated back to you on every round, so it does "
    "not belong in your reply."
)
PARAMETERS = {
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
}


def read(args, ctx):
    """Answer the plan tool, or say which failure stopped it. The normalized
    list lands on the context the engine owns, which is what lets the next
    round read it back."""
    raw = args.get("steps")
    if not isinstance(raw, list) or not raw:
        return missing_arg("steps")

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
        return ToolResult.failed(TOOL_ERROR, "No step had any text.", reason="every step was empty")

    ctx.plan = tuple(steps)
    text = _render(steps)
    if len(raw) > MAX_STEPS:
        text += f"\nOnly the first {MAX_STEPS} steps were kept."
    return ToolResult.ok(text, card={"steps": steps})


def step_line(i, step):
    """One step, numbered and marked, as both the tool's answer and the plan
    the engine puts back into the conversation read it."""
    return f"{i}. [{step['status']}] {step['step']}"


def _render(steps):
    done = sum(1 for s in steps if s["status"] == "done")
    lines = [f"Plan recorded ({done} of {len(steps)} done):"]
    for i, s in enumerate(steps, 1):
        lines.append(step_line(i, s))
    return "\n".join(lines)


def register():
    return registry.add(Tool(
        name=NAME,
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=read,
        permission=READ_ONLY,
        timeout=TIMEOUT_S,
        tags=TAGS,
        carded=True,
        last_pass=True,
    ))
