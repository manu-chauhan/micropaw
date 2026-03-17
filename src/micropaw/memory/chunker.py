"""Text chunker for RAG document ingestion."""

from __future__ import annotations

_DEFAULT_MAX_CHARS = 2000
_DEFAULT_OVERLAP = 200


def chunk_text(
    text: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
    overlap: int = _DEFAULT_OVERLAP,
) -> list[str]:
    """Split text into overlapping chunks.

    Strategy: split on paragraph breaks first, then line breaks, then hard-cut.
    Each chunk overlaps the previous by ``overlap`` characters for context continuity.
    """
    if not text or not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0

    while start < len(text):
        end = start + max_chars

        if end >= len(text):
            chunks.append(text[start:])
            break

        # Try to break at paragraph boundary
        segment = text[start:end]
        min_break = max_chars // 8  # at least 12.5% of chunk to avoid tiny fragments
        break_pos = segment.rfind("\n\n")
        if break_pos == -1 or break_pos < min_break:
            # Try line boundary
            break_pos = segment.rfind("\n")
        if break_pos == -1 or break_pos < min_break:
            # Try space
            break_pos = segment.rfind(" ")
        if break_pos == -1 or break_pos < min_break:
            # Hard cut
            break_pos = max_chars

        actual_end = start + break_pos
        chunks.append(text[start:actual_end].rstrip())

        # Advance with overlap
        start = max(actual_end - overlap, start + 1)

    return [c for c in chunks if c.strip()]
