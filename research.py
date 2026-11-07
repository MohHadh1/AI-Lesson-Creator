"""Collect background material for a lesson.

This module searches the public web with DuckDuckGo, looks up books in
Open Library (and a textbook web search), and searches YouTube. It does
not write the lesson and it does not judge quality.

A failed search is reported in the bundle. Callers must not treat an
empty list as a successful search.
"""

from __future__ import annotations

import re
import warnings
from time import sleep
from typing import Callable

import requests

from errors import LessonCreatorError, public_error_message

# The installed package warns on import that it was renamed to ``ddgs``.
# The library still works; the warning is not useful in the app.
warnings.filterwarnings(
    "ignore",
    message=r"This package \(`duckduckgo_search`\) has been renamed.*",
)

_YOUTUBE_PATCHED = False


def _dedupe(items: list[dict]) -> list[dict]:
    """Drop later items that repeat a URL."""
    kept: list[dict] = []
    seen: set[str] = set()
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        kept.append(item)
    return kept


_STOP_WORDS = {
    "a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with",
    "from", "by", "into", "about", "how", "what", "why", "when", "who",
    "that", "this", "these", "those", "is", "are", "was", "were", "be",
    "as", "at", "between", "explain", "identify", "describe", "distinguish",
}


def _tokens(text: str) -> set[str]:
    """Keep meaningful words so unrelated search hits can be dropped."""
    words = re.findall(r"[A-Za-z0-9+#]+", text.lower())
    return {word for word in words if word not in _STOP_WORDS and len(word) > 1}


def _topic_score(title: str, topic_words: set[str]) -> int:
    """Score a result by how many topic words appear in its title."""
    if not topic_words:
        return 0
    overlap = topic_words & _tokens(title)
    return 3 * len(overlap)


def _quiet_ddgs():
    """Open a DuckDuckGo client without its rename warning.

    The library turns warnings on inside its constructor, so a normal
    filter does not hide the message.
    """
    from duckduckgo_search import DDGS

    original = warnings.simplefilter

    def _keep_quiet(action, *args, **kwargs):
        if action == "always":
            return None
        return original(action, *args, **kwargs)

    warnings.simplefilter = _keep_quiet
    try:
        return DDGS()
    finally:
        warnings.simplefilter = original


def _ddg_text(keywords: str, max_results: int) -> list[dict]:
    """Run a DuckDuckGo text search across the working backends.

    The installed package's ``auto`` backend currently forces Bing and
    sometimes returns an empty list. Trying html, then lite, then bing
    keeps the search on DuckDuckGo's own clients when they respond.
    """
    if not keywords.strip():
        raise LessonCreatorError("Web search needs a search phrase.")

    try:
        from duckduckgo_search import DDGS  # noqa: F401  (import check)
    except ImportError as exc:
        raise LessonCreatorError(
            "The duckduckgo-search package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    errors: list[str] = []
    # Two passes: the first empty reply is often a temporary block.
    for attempt in range(2):
        ddgs = _quiet_ddgs()
        try:
            for backend in ("html", "lite", "bing"):
                try:
                    rows = ddgs.text(keywords, max_results=max_results, backend=backend) or []
                except Exception as exc:
                    errors.append(f"{backend}: {public_error_message(exc)}")
                    continue

                cleaned: list[dict] = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    url = str(row.get("href") or "").strip()
                    title = str(row.get("title") or "").strip()
                    if not title or not url.startswith(("http://", "https://")):
                        continue
                    cleaned.append(
                        {
                            "title": title,
                            "url": url,
                            "snippet": str(row.get("body") or "").strip(),
                            "origin": "duckduckgo",
                        }
                    )
                if cleaned:
                    return _dedupe(cleaned)[:max_results]
        finally:
            close = getattr(ddgs, "close", None)
            if callable(close):
                close()
        if attempt == 0:
            sleep(1)

    detail = "; ".join(dict.fromkeys(errors)) if errors else "every backend returned no pages"
    raise LessonCreatorError(f"Web search failed ({detail}).")


def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Search the web for pages related to ``query``.

    Args:
        query: Topic or search phrase entered by the user.
        max_results: Maximum number of pages to return.

    Returns:
        A list of dicts with keys ``title``, ``url``, and ``snippet``.

    Raises:
        LessonCreatorError: The search could not return any pages.
    """
    return _ddg_text(query, max_results)


def _open_library_books(query: str, max_results: int) -> list[dict]:
    """Look up real book records. URLs point at Open Library work pages."""
    try:
        response = requests.get(
            "https://openlibrary.org/search.json",
            params={"q": query, "limit": max_results, "language": "eng"},
            timeout=15,
            headers={"User-Agent": "AILessonCreator/1.0 (educational lesson planner)"},
        )
    except requests.RequestException as exc:
        raise LessonCreatorError(
            f"Open Library could not be reached ({public_error_message(exc)})."
        ) from exc

    if response.status_code != 200:
        raise LessonCreatorError(f"Open Library search failed (HTTP {response.status_code}).")

    try:
        payload = response.json()
    except ValueError as exc:
        raise LessonCreatorError("Open Library returned a response that was not JSON.") from exc

    books: list[dict] = []
    for doc in payload.get("docs") or []:
        if not isinstance(doc, dict):
            continue
        title = str(doc.get("title") or "").strip()
        key = str(doc.get("key") or "").strip()
        if not title or not key.startswith("/"):
            continue
        authors = [str(name).strip() for name in (doc.get("author_name") or []) if str(name).strip()]
        year = doc.get("first_publish_year")
        details = []
        if authors:
            details.append(", ".join(authors[:3]))
        if year:
            details.append(f"First published {year}")
        books.append(
            {
                "title": title,
                "url": f"https://openlibrary.org{key}",
                "snippet": ". ".join(details),
                "origin": "open_library",
            }
        )
        if len(books) >= max_results:
            break

    return books


def _book_queries(query: str) -> list[str]:
    """Build a few catalog queries from the lesson title."""
    queries = [query, f"{query} textbook"]
    for match in re.findall(r"\b[A-Z][A-Z0-9+#]{1,}\b", query):
        queries.append(f"{match} protocol")
        queries.append(match)
    ordered: list[str] = []
    for item in queries:
        cleaned = " ".join(item.split())
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return ordered[:4]


def _keep_relevant(items: list[dict], topic_words: set[str], limit: int) -> list[dict]:
    """Drop hits whose titles do not share a word with the lesson topic."""
    ranked = []
    for item in _dedupe(items):
        score = _topic_score(str(item.get("title") or ""), topic_words)
        if score <= 0:
            continue
        ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in ranked[:limit]]


def search_books(query: str, max_results: int = 4) -> list[dict]:
    """Find books and textbook pages related to ``query``.

    Open Library supplies the book records. A DuckDuckGo textbook search
    fills gaps when the catalog has little on the topic. Titles that do
    not share a word with the lesson topic are left out, so a loose
    catalog match is not presented as a source.
    """
    if not query.strip():
        raise LessonCreatorError("Book search needs a search phrase.")

    topic_words = _tokens(query)
    found: list[dict] = []
    errors: list[str] = []
    for book_query in _book_queries(query):
        try:
            found.extend(_open_library_books(book_query, max_results * 3))
        except LessonCreatorError as exc:
            errors.append(str(exc))
        if len(_keep_relevant(found, topic_words, max_results)) >= max_results:
            break

    relevant = _keep_relevant(found, topic_words, max_results)
    if len(relevant) < max_results:
        try:
            pages = _ddg_text(f"{query} textbook", max_results)
        except LessonCreatorError as exc:
            errors.append(str(exc))
        else:
            for page in pages:
                extra = dict(page)
                extra["origin"] = "web_textbook_search"
                note = "Found by a textbook web search."
                snippet = str(extra.get("snippet") or "").strip()
                extra["snippet"] = f"{note} {snippet}".strip()
                relevant.append(extra)

    relevant = _keep_relevant(relevant, topic_words, max_results)
    if relevant:
        return relevant
    detail = "; ".join(dict.fromkeys(errors)) if errors else "no titles matched the topic"
    raise LessonCreatorError(f"Book search failed ({detail}).")


def _patch_youtube_search() -> None:
    """Make youtube-search-python work with current httpx.

    httpx 0.28 renamed ``proxies`` to ``proxy``. The library still passes
    the old argument, so its video search crashes before any request.
    This replaces that one method and leaves the rest of the library as-is.
    """
    global _YOUTUBE_PATCHED
    if _YOUTUBE_PATCHED:
        return

    import httpx
    from youtubesearchpython.core import requests as yt_requests
    from youtubesearchpython.core.constants import userAgent

    def sync_post(self):
        options = {
            "headers": {"User-Agent": userAgent},
            "json": self.data,
            "timeout": self.timeout,
        }
        proxy = self.proxy or None
        try:
            return httpx.post(self.url, proxy=proxy, **options)
        except TypeError:
            return httpx.post(self.url, proxies=self.proxy, **options)

    yt_requests.RequestCore.syncPostRequest = sync_post
    _YOUTUBE_PATCHED = True


def _snippet_text(value: object) -> str:
    """Join YouTube's description pieces into one plain string."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for piece in value:
        if isinstance(piece, str):
            parts.append(piece)
        elif isinstance(piece, dict):
            parts.append(str(piece.get("text") or ""))
    return "".join(parts).strip()


def search_youtube(query: str, max_results: int = 5) -> list[dict]:
    """Search YouTube for videos related to ``query``.

    Args:
        query: Topic or search phrase entered by the user.
        max_results: Maximum number of videos to return.

    Returns:
        A list of dicts with keys ``title``, ``url``, ``channel``, and ``description``.

    Raises:
        LessonCreatorError: The search library failed or returned no videos.
    """
    if not query.strip():
        raise LessonCreatorError("YouTube search needs a search phrase.")

    try:
        _patch_youtube_search()
        from youtubesearchpython import VideosSearch
    except LessonCreatorError:
        raise
    except ImportError as exc:
        raise LessonCreatorError(
            "The youtube-search-python package is not installed. Run: pip install -r requirements.txt"
        ) from exc
    except Exception as exc:
        raise LessonCreatorError(
            f"YouTube search could not start ({public_error_message(exc)})."
        ) from exc

    try:
        payload = VideosSearch(query, limit=max(max_results, 1), timeout=15).result()
    except Exception as exc:
        raise LessonCreatorError(
            f"YouTube search failed ({public_error_message(exc)})."
        ) from exc

    videos: list[dict] = []
    for item in (payload or {}).get("result") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        video_id = str(item.get("id") or "").strip()
        url = str(item.get("link") or "").strip()
        if not url and video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"
        if not title or not url.startswith("https://"):
            continue
        channel = item.get("channel")
        channel_name = ""
        if isinstance(channel, dict):
            channel_name = str(channel.get("name") or "").strip()
        elif isinstance(channel, str):
            channel_name = channel.strip()
        videos.append(
            {
                "title": title,
                "url": url,
                "channel": channel_name,
                "description": _snippet_text(item.get("descriptionSnippet")),
                "origin": "youtube",
            }
        )
        if len(videos) >= max_results:
            break

    videos = _dedupe(videos)
    if not videos:
        raise LessonCreatorError("YouTube search returned no videos.")
    return videos


def _run_search(label: str, func: Callable[[], list[dict]]) -> tuple[list[dict], str]:
    """Run one search. Return items and an error string (empty on success)."""
    try:
        items = func()
    except LessonCreatorError as exc:
        return [], str(exc)
    except Exception as exc:
        return [], f"{label} failed ({public_error_message(exc)})."
    if not items:
        return [], f"{label} returned no results."
    return items, ""


def collect_research(
    topic: str,
    max_results: int = 5,
    learning_objective: str = "",
) -> dict:
    """Run web, book, and video searches and package them for the generator.

    Args:
        topic: Lesson title from the Streamlit form.
        max_results: How many web pages and how many videos to keep.
        learning_objective: Used to narrow the web and video queries.

    Returns:
        A research bundle. ``retrieved`` is True only when at least one
        real source came back. ``errors`` lists searches that failed.
        An empty source list always has a matching error string.
    """
    title = topic.strip()
    objective = learning_objective.strip()
    if not title:
        raise LessonCreatorError("Research needs a lesson title.")

    limit = max(1, min(int(max_results), 8))
    web_queries = []
    for candidate in (f"{title} {objective}".strip(), title, f"{title} explained"):
        if candidate and candidate not in web_queries:
            web_queries.append(candidate[:180])

    def _web_with_fallbacks() -> list[dict]:
        found: list[dict] = []
        errors: list[str] = []
        for index, web_query in enumerate(web_queries):
            try:
                found.extend(search_web(web_query, limit))
            except LessonCreatorError as exc:
                errors.append(str(exc))
                if index < len(web_queries) - 1:
                    sleep(0.5)
            found = _dedupe(found)
            if len(found) >= limit:
                return found[:limit]
        if found:
            return found[:limit]
        detail = errors[-1] if errors else "Web search returned no pages."
        raise LessonCreatorError(detail)

    video_query = web_queries[0]
    web, web_error = _run_search("Web search", _web_with_fallbacks)
    books, book_error = _run_search("Book search", lambda: search_books(title, min(limit, 4)))
    videos, video_error = _run_search(
        "YouTube search", lambda: search_youtube(video_query, min(limit, 4))
    )

    errors = [message for message in (web_error, book_error, video_error) if message]
    retrieved = bool(web or books or videos)
    return {
        "topic": title,
        "learning_objective": objective,
        "web": web,
        "books": books,
        "youtube": videos,
        "errors": errors,
        "retrieved": retrieved,
    }


def _clip(text: str, limit: int = 500) -> str:
    """Shorten a snippet so the model prompt stays readable."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def format_research_for_prompt(research: dict) -> str:
    """Describe retrieved sources for the writer and the factual reviewer.

    The teaching reviewer must not call this. It would turn that review
    into a second fact check.
    """
    lines: list[str] = []
    numbered: list[dict] = []
    for kind, key in (("website", "web"), ("book", "books"), ("video", "youtube")):
        for item in research.get(key) or []:
            numbered.append({"kind": kind, **item})

    if not numbered:
        errors = research.get("errors") or []
        error_text = " ".join(str(item) for item in errors) or "No error detail was recorded."
        return (
            "RESEARCH STATUS: Failed. No websites, books, or videos were retrieved.\n"
            "Do not claim that research was performed. Do not invent titles, authors, or URLs.\n"
            f"What failed: {error_text}"
        )

    lines.append(
        "RESEARCH STATUS: The items below were retrieved. "
        "Use only these items. Do not add sources that are not in this list."
    )
    errors = research.get("errors") or []
    if errors:
        lines.append("Some searches failed: " + " ".join(str(item) for item in errors))
        lines.append("Do not claim that a failed search succeeded.")

    for index, item in enumerate(numbered, start=1):
        title = str(item.get("title") or "Untitled").strip()
        url = str(item.get("url") or "").strip()
        kind = item.get("kind")
        origin = item.get("origin") or ""
        extra = ""
        if kind == "video" and item.get("channel"):
            extra = f" Channel: {item['channel']}."
        if kind == "book" and origin == "web_textbook_search":
            kind = "textbook page from web search"
        snippet = _clip(str(item.get("snippet") or item.get("description") or ""))
        lines.append(
            f"Source {index} ({kind}): {title}.{extra}\nURL: {url}\nExcerpt: {snippet or 'No excerpt returned.'}"
        )
    return "\n\n".join(lines)


def iter_sources(research: dict) -> list[dict]:
    """Return retrieved sources in a stable order, with no invented URLs."""
    sources: list[dict] = []
    labels = {
        "duckduckgo": "Website",
        "open_library": "Book",
        "web_textbook_search": "Textbook page",
        "youtube": "Video",
    }
    groups = (
        ("web", "Website"),
        ("books", "Book"),
        ("youtube", "Video"),
    )
    for key, default_kind in groups:
        for item in research.get(key) or []:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url.startswith(("http://", "https://")) or not title:
                continue
            kind = labels.get(str(item.get("origin") or ""), default_kind)
            sources.append(
                {
                    "title": title,
                    "url": url,
                    "kind": kind,
                    "detail": str(item.get("snippet") or item.get("description") or item.get("channel") or "").strip(),
                }
            )
    return _dedupe(sources)
