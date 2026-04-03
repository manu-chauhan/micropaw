"""PDF reading tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from micropaw.tools.base import Tool, ToolContext
from micropaw.tools.path_security import validate_file_path

MAX_OUTPUT = 50_000


def _parse_pages(spec: str, total: int) -> list[int]:
    """Parse a page specification like '1-5', '3', '1,3,5' into page indices (0-based)."""
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, end = part.split("-", 1)
                start_i = max(0, int(start) - 1)
                end_i = min(total, int(end))
                pages.update(range(start_i, end_i))
            else:
                idx = int(part) - 1
                if 0 <= idx < total:
                    pages.add(idx)
        except ValueError:
            continue
    return sorted(pages)


class PdfReadTool(Tool):
    name = "pdf_read"
    description = "Read text content from a PDF file. Supports page ranges."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the PDF file"},
            "pages": {"type": "string", "description": "Page range (e.g. '1-5', '3', '1,3,5'). Default: all pages"},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        path_str = args["path"]
        pages_spec = args.get("pages")

        if error := validate_file_path(path_str):
            return f"Error: {error}"

        path = Path(path_str).resolve()
        if not path.exists():
            return f"Error: File not found: {path}"

        try:
            from pypdf import PdfReader
        except ImportError:
            return "Error: pypdf not installed. Install with: pip install 'micropaw[pdf]'"

        try:
            reader = PdfReader(str(path))
            total = len(reader.pages)

            if pages_spec:
                page_indices = _parse_pages(pages_spec, total)
                if not page_indices:
                    return f"Error: No valid pages in range '{pages_spec}'. PDF has {total} page(s)."
            else:
                page_indices = list(range(total))

            parts = [f"PDF: {path.name} ({total} pages)"]
            output_len = 0

            for idx in page_indices:
                text = reader.pages[idx].extract_text() or "(no text)"
                header = f"\n--- Page {idx + 1} ---\n"
                if output_len + len(header) + len(text) > MAX_OUTPUT:
                    parts.append(f"\n... (truncated at page {idx + 1})")
                    break
                parts.append(header + text)
                output_len += len(header) + len(text)

            return "\n".join(parts)

        except Exception as e:
            return f"Error reading PDF: {e}"
