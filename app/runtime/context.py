"""Assembling what the model gets to see.

Three kinds of material compete for the same context window: session
(conversation and current instruction), knowledge (retrieved passages), and
evidence (what search tools fetched). The rules: keep the current instruction,
stop when the budget is full, and drop the least relevant passage first.
"""

from dataclasses import dataclass

DEFAULT_BUDGET = 24_000


@dataclass
class ContextBudget:
    total: int = DEFAULT_BUDGET
    per_source: int = 6_000
    session: int = 8_000

    def slice(self, text, limit=None):
        limit = limit or self.per_source
        if text is None:
            return ""
        return text if len(text) <= limit else text[:limit].rstrip() + "\n[... truncated]"


def assemble(budget, *, base_prompt, today, session="",
             knowledge=(), evidence=(), instruction=""):
    """The system prompt and the context blocks, within budget.

    Order matters more than it looks: the base prompt and the current
    instruction go first because a truncation that drops them is a broken
    request, and the optional material is appended longest-first so the least
    relevant passage is the one that falls off the end.
    """
    parts = [budget.slice(base_prompt, 8_000)]
    if today:
        parts.append(f"Today is {today}.")
    used = sum(len(p) for p in parts)
    room = max(budget.total - used, 0)

    blocks = []
    for text in _blocks(knowledge, "Context", budget, room):
        blocks.append(text)
        room -= len(text)
        if room <= 0:
            break
    for text in _blocks(evidence, "Evidence", budget, room):
        blocks.append(text)
        room -= len(text)
        if room <= 0:
            break

    if blocks:
        parts.extend(blocks)
    if session:
        parts.append("Recent conversation:\n" + budget.slice(session, budget.session))
    if instruction:
        parts.append(instruction)
    return "\n\n".join(p for p in parts if p and p.strip())


def _blocks(items, label, budget, room):
    """One block per passage, with its origin, until `room` runs out."""
    if not items:
        return []
    kept = []
    spent = 0
    for s in items:
        text = budget.slice(s.text)
        cost = len(text) + 60
        if spent + cost > room:
            break
        spent += cost
        head = f"[{len(kept) + 1}]" + (f" ({s.origin})" if s.origin else "")
        kept.append((head, text))
    if not kept:
        return []
    body = "\n\n".join(f"{head}\n{text}" for head, text in kept)
    cite = (
        f"\nCite these as [n]. They are sources gathered for this question; do not "
        f"present a passage you were not given."
    )
    return [f"{label}:\n{body}{cite}"]