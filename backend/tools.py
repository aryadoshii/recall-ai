"""Web search tool for the agentic loop."""

from __future__ import annotations

# System prompt fragment appended when agent mode is active.
AGENT_SYSTEM_ADDENDUM = """
## Web Search Tool
You have access to real-time web search via DuckDuckGo. You MUST use it when:
- The user asks about anything that happened after your training cutoff
- The user asks for current news, prices, scores, weather, or live data
- The user mentions a specific recent event, product release, or person in the news
- You are not 100% certain your knowledge is up to date on the topic

To trigger a search output EXACTLY this and NOTHING else (no extra text before or after):
[SEARCH: your specific search query]

Wait for the search results before writing your answer. You may search multiple times if needed."""


def web_search(query: str, max_results: int = 5) -> str:
    """Run a DuckDuckGo search and return formatted results."""
    try:
        from ddgs import DDGS
    except ImportError:
        return "Web search unavailable: `ddgs` is not installed."

    try:
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        return f"Search failed: {exc}"

    if not hits:
        return "No results found."

    lines: list[str] = []
    for hit in hits:
        title = hit.get("title", "").strip()
        url = hit.get("href", "").strip()
        body = hit.get("body", "").strip()
        lines.append(f"**{title}**\n{url}\n{body}")

    return "\n\n".join(lines)
