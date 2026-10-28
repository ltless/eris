"""Assembling what the model gets to see.

Three kinds of material compete for the same context window: session
(conversation and current instruction), knowledge (retrieved passages), and
evidence (what search tools fetched). The rules: rank before including, drop
duplicates, keep the current instruction, stop when the budget is full.
"""

import re
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


@dataclass
class Source:
    """One candidate passage, with enough metadata to rank and dedupe it."""

    text: str
    origin: str = ""
    kind: str = "web"          # web | evidence | code
    score: float = 0.0
    ref: str = ""


def dedupe(items, key=None):
    """Drop repeats, keeping the first of each, which is the highest ranked."""
    seen = set()
    out = []
    for it in items:
        k = key(it) if key else _fingerprint(it.text if hasattr(it, "text") else str(it))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


_WORD = re.compile(r"\w+", re.UNICODE)


def _fingerprint(text, length=120):
    """A cheap identity for a passage: its words, lowercased and sorted.

    Two chunks of the same page usually overlap by the chunk stride, so a
    prefix comparison would miss them; the word set catches it.
    """
    words = sorted({w.lower() for w in _WORD.findall(text or "")})
    return " ".join(words)[:length]


def rank(items, query, *, freshness_weight=0.0, trust=None):
    """Score passages by term overlap with the query, then order them.

    Term overlap is crude and it is meant to be: it is explainable, it cannot
    be fooled into treating a fluent passage as relevant, and the alternative —
    a second model call to grade passages — costs more than it returns at this
    size. Freshness and a domain trust list adjust the score where they apply.
    """
    terms = {t.lower() for t in _WORD.findall(query or "") if len(t) > 2}
    trust = trust or {}

    def score(s):
        words = {w.lower() for w in _WORD.findall(s.text or "")}
        overlap = len(terms & words)
        base = overlap / (len(terms) or 1)
        if trust.get(s.origin, 0):
            base += 0.15
        return base + s.score

    scored = [Source(text=s.text, origin=s.origin, kind=s.kind, ref=s.ref,
                     score=score(s)) for s in items]
    scored.sort(key=lambda s: -s.score)
    return scored


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
    for text, label in _blocks(knowledge, "Context", budget, room):
        blocks.append(text)
        room -= len(text)
        if room <= 0:
            break
    for text, label in _blocks(evidence, "Evidence", budget, room):
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
    out = []
    if not items:
        return out
    kept = []
    spent = 0
    for s in items:
        text = budget.slice(s.text)
        cost = len(text) + 60
        if spent + cost > room:
            break
        spent += cost
        kept.append(s)
        head = f"[{len(kept)}]" + (f" ({s.origin})" if s.origin else "")
        kept[-1] = (s, head, text)
    if not kept:
        return out
    body = "\n\n".join(f"{head}\n{text}" for _, head, text in kept)
    cite = (
        f"\nCite these as [n]. They are sources gathered for this question; do not "
        f"present a passage you were not given."
    )
    out.append((f"{label}:\n{body}{cite}", label))
    return out