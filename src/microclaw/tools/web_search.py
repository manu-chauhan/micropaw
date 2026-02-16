"""Web search tool using DuckDuckGo."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from microclaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web and return results with titles, URLs, and snippets."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
            "max_results": {"type": "integer", "description": "Maximum results to return (default: 5)"},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        query = args["query"]
        max_results = args.get("max_results", 5)

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                )
                resp.raise_for_status()
                data = resp.json()

            results = []
            for topic in data.get("RelatedTopics", [])[:max_results]:
                if "Text" in topic and "FirstURL" in topic:
                    results.append(f"- {topic['Text']}\n  URL: {topic['FirstURL']}")

            if data.get("AbstractText"):
                results.insert(0, f"Summary: {data['AbstractText']}\nSource: {data.get('AbstractURL', '')}")

            if not results:
                return f"No results found for: {query}"

            return f"Search results for '{query}':\n\n" + "\n\n".join(results)

        except Exception as e:
            return f"Search error: {e}"
