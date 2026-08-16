"""
scraper.py
Implements SOP Section 2: Configuring the Agentic Scraping Workflow.

Fetches articles from RSS feeds and/or section URLs for each configured
source, respecting parsing_depth (home -> article) and lookback_days.
"""

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin

import requests
import feedparser
from bs4 import BeautifulSoup

logger = logging.getLogger("mws_agent.scraper")


@dataclass
class Article:
    source: str
    vertical: str
    title: str
    url: str
    published: Optional[datetime]
    full_text: str = ""
    fetch_status: str = "ok"      # ok | blocked | error | truncated
    error_detail: str = ""


class SourceScraper:
    def __init__(self, config: dict):
        self.cfg = config
        self.crawl_cfg = config["crawl"]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.crawl_cfg["user_agent"]})
        self.timeout = self.crawl_cfg["request_timeout_seconds"]
        self.delay = self.crawl_cfg["request_delay_seconds"]
        self.lookback = timedelta(days=self.crawl_cfg["lookback_days"])
        self.max_per_source = self.crawl_cfg["max_articles_per_source"]
        self.failures: list[dict] = []

    # ---------------------------------------------------------------
    # Public entry point
    # ---------------------------------------------------------------
    def run(self) -> list[Article]:
        all_articles: list[Article] = []
        cutoff = datetime.now(timezone.utc) - self.lookback

        for source in self.cfg["sources"]:
            if source.get("optional") and self._has_placeholder(source):
                logger.info("Skipping optional/unconfigured source: %s", source["name"])
                continue

            found = []
            for feed_url in source.get("rss_feeds", []):
                found.extend(self._pull_rss(source, feed_url, cutoff))

            for section_url in source.get("section_urls", []):
                if section_url.strip().upper().startswith("TODO"):
                    self._record_failure(source["name"], section_url, "unconfigured TODO URL")
                    continue
                found.extend(self._pull_section(source, section_url, cutoff))

            found = found[: self.max_per_source]
            logger.info("Source '%s': %d articles within lookback window", source["name"], len(found))
            all_articles.extend(found)

        return all_articles

    # ---------------------------------------------------------------
    # RSS path
    # ---------------------------------------------------------------
    def _pull_rss(self, source: dict, feed_url: str, cutoff: datetime) -> list[Article]:
        results = []
        try:
            resp = self.session.get(feed_url, timeout=self.timeout)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"Feed parse error: {parsed.bozo_exception}")

            for entry in parsed.entries:
                published = self._parse_entry_date(entry)
                if published and published < cutoff:
                    continue  # outside lookback window

                article = Article(
                    source=source["name"],
                    vertical=", ".join(source.get("verticals", [])),
                    title=entry.get("title", "Untitled"),
                    url=entry.get("link", ""),
                    published=published,
                )
                # Parsing depth 2: fetch full article text, not just the feed summary
                self._enrich_with_full_text(article)
                results.append(article)
                time.sleep(self.delay)

        except Exception as e:
            self._record_failure(source["name"], feed_url, str(e))

        return results

    # ---------------------------------------------------------------
    # Section-page crawl path (fallback when no RSS exists)
    # ---------------------------------------------------------------
    def _pull_section(self, source: dict, section_url: str, cutoff: datetime) -> list[Article]:
        results = []
        try:
            resp = self.session.get(section_url, timeout=self.timeout)
            if resp.status_code in (403, 429):
                self._record_failure(source["name"], section_url,
                                      f"HTTP {resp.status_code} — likely bot-detection/paywall")
                return results
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            links = self._extract_article_links(soup, section_url)

            for link in links[: self.max_per_source]:
                article = Article(
                    source=source["name"],
                    vertical=", ".join(source.get("verticals", [])),
                    title=link["title"],
                    url=link["url"],
                    published=None,  # unknown until article page is fetched
                )
                self._enrich_with_full_text(article)
                # If the article page exposes a date and it's outside lookback, drop it
                if article.published and article.published < cutoff:
                    continue
                results.append(article)
                time.sleep(self.delay)

        except Exception as e:
            self._record_failure(source["name"], section_url, str(e))

        return results

    def _extract_article_links(self, soup: BeautifulSoup, base_url: str) -> list[dict]:
        """Heuristic link extraction for depth-2 crawling of a section/listing page.

        Filters out category/tag/archive/author pages, which are easy to
        mistake for articles since their link text is often long enough to
        pass a naive length check (e.g. "25 Years of Indian Infrastructure",
        "Ports & Shipping News" are category names, not article headlines).
        """
        # URL path segments that indicate a listing/index page, not an article.
        non_article_path_markers = (
            "/category/", "/categories/", "/tag/", "/tags/", "/author/",
            "/page/", "/archive", "/section/", "/topic/", "/topics/",
        )

        candidates = []
        seen = set()
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            href = a["href"]
            if not title or len(title) < 15:
                continue  # skip nav/menu links
            full_url = urljoin(base_url, href)

            # Skip listing/index pages masquerading as articles.
            lower_url = full_url.lower()
            if any(marker in lower_url for marker in non_article_path_markers):
                continue
            # Skip bare year/month archive paths like /2026/08/
            path = full_url.split(base_url.split("//", 1)[-1].split("/", 1)[0], 1)[-1]
            path_parts = [p for p in path.strip("/").split("/") if p]
            if path_parts and all(p.isdigit() for p in path_parts):
                continue

            if full_url in seen:
                continue
            seen.add(full_url)
            candidates.append({"title": title, "url": full_url})
        return candidates

    # ---------------------------------------------------------------
    # Article body fetch (depth 2)
    # ---------------------------------------------------------------
    def _enrich_with_full_text(self, article: Article) -> None:
        if not article.url:
            article.fetch_status = "error"
            article.error_detail = "missing URL"
            return
        try:
            resp = self.session.get(article.url, timeout=self.timeout)
            if resp.status_code in (403, 429):
                article.fetch_status = "blocked"
                article.error_detail = f"HTTP {resp.status_code}"
                return
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Try to recover a publish date from common meta tags if not already set
            if article.published is None:
                article.published = self._extract_meta_date(soup)

            paragraphs = soup.find_all("p")
            text = "\n".join(p.get_text(strip=True) for p in paragraphs)
            if len(text) < 200:
                article.fetch_status = "truncated"
            article.full_text = text[:8000]  # cap payload size for the LLM pass

        except Exception as e:
            article.fetch_status = "error"
            article.error_detail = str(e)

    @staticmethod
    def _extract_meta_date(soup: BeautifulSoup) -> Optional[datetime]:
        for prop in ["article:published_time", "og:published_time", "publish-date"]:
            tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
            if tag and tag.get("content"):
                try:
                    return datetime.fromisoformat(tag["content"].replace("Z", "+00:00"))
                except ValueError:
                    continue
        return None

    @staticmethod
    def _parse_entry_date(entry) -> Optional[datetime]:
        for key in ("published_parsed", "updated_parsed"):
            val = entry.get(key)
            if val:
                return datetime(*val[:6], tzinfo=timezone.utc)
        return None

    @staticmethod
    def _has_placeholder(source: dict) -> bool:
        urls = source.get("section_urls", []) + source.get("rss_feeds", [])
        return any(u.strip().upper().startswith("TODO") for u in urls) or not urls

    def _record_failure(self, source_name: str, url: str, detail: str) -> None:
        is_ssl_issue = "CERTIFICATE_VERIFY_FAILED" in detail or "SSLError" in detail

        if is_ssl_issue:
            # This is a known condition on the target site (expired/misconfigured
            # cert on their end), not something wrong with our scraper. We
            # deliberately do NOT bypass SSL verification to fetch anyway —
            # that would accept unverified connections and is not worth the
            # risk for an optional/secondary source. Log calmly, not as a
            # scraper bug, and note it distinctly in the report.
            logger.info(
                "Source '%s' has an SSL certificate problem on their end (skipping, not retrying): %s",
                source_name, url,
            )
            detail = f"SSL certificate issue on target site (not a scraper bug): {detail}"
        else:
            logger.warning("Source health issue [%s] %s -> %s", source_name, url, detail)

        self.failures.append({
            "source": source_name,
            "url": url,
            "detail": detail,
            "category": "ssl_certificate" if is_ssl_issue else "other",
        })
