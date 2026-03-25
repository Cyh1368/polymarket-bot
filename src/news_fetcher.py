"""Fetch relevant news articles for market questions using Brave Search API."""

import logging
from datetime import datetime, timedelta

import httpx

from .models import NewsArticle

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SERP_API_URL = "https://serpapi.com/search"
MAX_ARTICLES_PER_MARKET = 5

# Sources to flag
STATE_MEDIA = {"rt.com", "sputniknews.com", "xinhua.net", "globaltimes.cn", "presstv.ir"}
PARTISAN_OUTLETS = {"breitbart.com", "infowars.com", "occupydemocrats.com", "dailykos.com"}
PAYWALL_SOURCES = {"wsj.com", "ft.com", "nytimes.com", "bloomberg.com", "economist.com"}


async def fetch_news(
    question: str,
    api_key: str,
    api_type: str = "brave",
    days_back: int = 7,
) -> list[NewsArticle]:
    """Search for relevant news articles about a market question.

    Args:
        question: The market question to search for.
        api_key: API key for the search service.
        api_type: "brave" or "serpapi".
        days_back: How many days of news to search.
    """
    if api_type == "brave":
        return await _fetch_brave(question, api_key, days_back)
    elif api_type == "serpapi":
        return await _fetch_serpapi(question, api_key, days_back)
    else:
        raise ValueError(f"Unknown API type: {api_type}")


async def _fetch_brave(
    question: str, api_key: str, days_back: int
) -> list[NewsArticle]:
    """Fetch news from Brave Search API."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {
        "q": question,
        "count": 10,
        "freshness": f"pd{days_back}",
        "text_decorations": False,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(BRAVE_SEARCH_URL, headers=headers, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("Brave Search failed: %s", e)
            return []

    data = resp.json()
    results = data.get("web", {}).get("results", [])

    articles = []
    seen_urls: set[str] = set()

    for r in results:
        url = r.get("url", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)

        article = NewsArticle(
            title=r.get("title", ""),
            summary=r.get("description", ""),
            source=r.get("profile", {}).get("name", _extract_domain(url)),
            published_date=_parse_date(r.get("age", "")),
            url=url,
            quality_flag=_check_source_quality(url),
        )
        articles.append(article)

        if len(articles) >= MAX_ARTICLES_PER_MARKET:
            break

    logger.info("Found %d articles for: %s", len(articles), question[:60])
    return articles


async def _fetch_serpapi(
    question: str, api_key: str, days_back: int
) -> list[NewsArticle]:
    """Fetch news from SerpAPI (Google News)."""
    params = {
        "engine": "google_news",
        "q": question,
        "api_key": api_key,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(SERP_API_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("SerpAPI failed: %s", e)
            return []

    data = resp.json()
    results = data.get("news_results", [])

    articles = []
    seen_urls: set[str] = set()
    cutoff = datetime.utcnow() - timedelta(days=days_back)

    for r in results:
        url = r.get("link", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)

        pub_date = _parse_date(r.get("date", ""))
        if pub_date < cutoff:
            continue

        article = NewsArticle(
            title=r.get("title", ""),
            summary=r.get("snippet", ""),
            source=r.get("source", {}).get("name", _extract_domain(url)),
            published_date=pub_date,
            url=url,
            quality_flag=_check_source_quality(url),
        )
        articles.append(article)

        if len(articles) >= MAX_ARTICLES_PER_MARKET:
            break

    logger.info("Found %d articles for: %s", len(articles), question[:60])
    return articles


def _extract_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return "unknown"


def _check_source_quality(url: str) -> str | None:
    """Flag potentially unreliable sources."""
    domain = _extract_domain(url)
    if domain in STATE_MEDIA:
        return "state_media"
    if domain in PARTISAN_OUTLETS:
        return "partisan"
    if domain in PAYWALL_SOURCES:
        return "paywall"
    return None


def _parse_date(date_str: str) -> datetime:
    """Best-effort date parsing."""
    if not date_str:
        return datetime.utcnow()

    # Handle relative dates like "2 hours ago", "3 days ago"
    date_str_lower = date_str.lower()
    if "hour" in date_str_lower:
        try:
            hours = int("".join(c for c in date_str_lower.split("hour")[0] if c.isdigit()))
            return datetime.utcnow() - timedelta(hours=hours)
        except ValueError:
            pass
    if "day" in date_str_lower:
        try:
            days = int("".join(c for c in date_str_lower.split("day")[0] if c.isdigit()))
            return datetime.utcnow() - timedelta(days=days)
        except ValueError:
            pass

    # Try ISO format
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    return datetime.utcnow()


def format_articles_for_prompt(articles: list[NewsArticle]) -> str:
    """Format articles into a string for Claude's prompt."""
    if not articles:
        return "No recent news articles found."

    lines = []
    for i, a in enumerate(articles, 1):
        flag = f" [⚠️ {a.quality_flag}]" if a.quality_flag else ""
        lines.append(
            f"Article {i}{flag}:\n"
            f"  Title: {a.title}\n"
            f"  Source: {a.source} ({a.published_date.strftime('%Y-%m-%d')})\n"
            f"  Summary: {a.summary}\n"
            f"  URL: {a.url}"
        )
    return "\n\n".join(lines)
