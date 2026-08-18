"""Splitting document text into retrieval units.

Roughly 512 tokens per chunk with ~15% overlap, preferring paragraph breaks and
falling back to sentence then word boundaries only when a single paragraph is
too long to fit. Overlap exists so that a passage straddling a boundary is
still wholly present in one chunk.

Token counts are estimated rather than measured: importing the embedding
model's tokenizer here would make chunking depend on the model, and the whole
point of keying vectors by ``model_id`` is that the two stay independent.
"""

from __future__ import annotations

import re

TARGET_TOKENS = 512
OVERLAP_RATIO = 0.15

_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.!?;:])\s+")


def estimate_tokens(text: str) -> int:
    """Approximate a WordPiece count without loading a tokenizer.

    English prose runs about 1.3 subword tokens per whitespace word. The
    character term catches text with no spaces at all — base64 payloads, CJK —
    where the word count would badly understate the true length.
    """
    words = len(text.split())
    return max((words * 13) // 10, len(text) // 4, 1 if text else 0)


def _split_oversized(unit: str, target: int) -> list[str]:
    """Break a single unit that exceeds the budget, gently then bluntly."""
    if estimate_tokens(unit) <= target:
        return [unit]

    pieces: list[str] = []
    for sentence in _SENTENCE_RE.split(unit):
        if not sentence:
            continue
        if estimate_tokens(sentence) <= target:
            pieces.append(sentence)
            continue
        # A single sentence over budget: split on words.
        current: list[str] = []
        for word in sentence.split():
            # A "word" can itself be longer than a chunk — a base64 blob, a
            # minified script, CJK with no spaces. Cut it by characters, or
            # nothing below terminates.
            for fragment in _split_long_word(word, target):
                current.append(fragment)
                if estimate_tokens(" ".join(current)) >= target:
                    pieces.append(" ".join(current))
                    current = []
        if current:
            pieces.append(" ".join(current))
    return [p for p in pieces if p.strip()]


def _split_long_word(word: str, target: int) -> list[str]:
    if estimate_tokens(word) <= target:
        return [word]
    width = max(target * 4, 1)  # estimate_tokens counts len//4 for spaceless text
    return [word[i : i + width] for i in range(0, len(word), width)]


def chunk_text(
    text: str,
    *,
    target_tokens: int = TARGET_TOKENS,
    overlap_ratio: float = OVERLAP_RATIO,
) -> list[str]:
    """Split text into overlapping chunks. Empty input yields no chunks."""
    text = (text or "").strip()
    if not text:
        return []

    units: list[str] = []
    for paragraph in _PARAGRAPH_RE.split(text):
        paragraph = paragraph.strip()
        if paragraph:
            units.extend(_split_oversized(paragraph, target_tokens))
    if not units:
        return []

    overlap_budget = int(target_tokens * overlap_ratio)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for unit in units:
        unit_tokens = estimate_tokens(unit)
        if current and current_tokens + unit_tokens > target_tokens:
            chunks.append("\n\n".join(current))
            current, current_tokens = _carry_over(current, overlap_budget)
        current.append(unit)
        current_tokens += unit_tokens

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _carry_over(units: list[str], budget: int) -> tuple[list[str], int]:
    """Trailing units of the finished chunk that seed the next one."""
    if budget <= 0:
        return [], 0
    carried: list[str] = []
    total = 0
    for unit in reversed(units):
        unit_tokens = estimate_tokens(unit)
        if total + unit_tokens > budget and carried:
            break
        carried.insert(0, unit)
        total += unit_tokens
        if total >= budget:
            break
    # Never carry the whole chunk forward: that would not terminate.
    if len(carried) >= len(units):
        carried = carried[1:]
        total = sum(estimate_tokens(u) for u in carried)
    return carried, total
