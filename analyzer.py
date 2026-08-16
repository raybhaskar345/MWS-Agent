"""
analyzer.py
Implements SOP Section 3 (Prompt Engineering / Intelligence Layer) and
Section 4 (Logical Filtering and Semantic Search).

Uses Google's Gemini API (free tier) to classify scraped articles into the
Use Case Classification Matrix. A cheap keyword pre-filter (Section 4's
"semantic search" keyword lists) trims the candidate set before the
LLM pass, to save on API calls for obviously irrelevant articles.

Get a free API key at: https://aistudio.google.com/apikey
Free tier limits (check current values at ai.google.dev/pricing before
relying on them — they change): as of setup time, Gemini 2.5 Flash has a
free per-minute and per-day request cap on API-key access. If you exceed
it, calls will 429; the _classify_article retry/backoff below handles
transient limit hits.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types

from scraper import Article

logger = logging.getLogger("mws_agent.analyzer")

# "flash" is the free-tier-friendly model — fast and cheap/free vs "pro".
MODEL = "gemini-2.5-flash"


@dataclass
class Finding:
    company_name: str
    sector: str
    location: str
    category: str
    pain_point_or_opportunity: str
    severity: str            # "Critical" | "Standard"
    source: str
    article_title: str
    article_url: str


class Analyzer:
    def __init__(self, config: dict):
        self.cfg = config
        self.system_prompt = self._build_system_prompt()

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "No Gemini API key found. Get a free key at "
                "https://aistudio.google.com/apikey and set it as an "
                "environment variable: export GEMINI_API_KEY=..."
            )
        self.client = genai.Client(api_key=api_key)

    # ---------------------------------------------------------------
    def _build_system_prompt(self) -> str:
        base = self.cfg["system_prompt"].strip()
        matrix_lines = ["\n\nUSE CASE CLASSIFICATION MATRIX:"]
        for row in self.cfg["classification_matrix"]:
            matrix_lines.append(
                f"- {row['category']}: keywords={row['keywords']}; logic: {row['logic'].strip()}"
            )
        matrix_lines.append(
            "\nRespond ONLY with a JSON array. Each element must have exactly these keys: "
            '"company_name", "sector", "location", "category", "pain_point_or_opportunity", '
            '"severity" (one of "Critical" or "Standard"; use "Critical" for confirmed penalties, '
            'fines, or violations under Environmental Compliance, else "Standard"). '
            "If the article has no actionable company-level finding, return an empty array."
        )
        return base + "\n".join(matrix_lines)

    # ---------------------------------------------------------------
    def _keyword_prefilter(self, article: Article) -> bool:
        """Section 4: cheap pre-filter before the LLM pass."""
        text = f"{article.title} {article.full_text}".lower()
        all_keywords = []
        for lst in self.cfg["semantic_keyword_lists"].values():
            all_keywords.extend(lst)
        for row in self.cfg["classification_matrix"]:
            all_keywords.extend(row["keywords"])
        return any(kw.lower() in text for kw in all_keywords)

    # ---------------------------------------------------------------
    def analyze(self, articles: list[Article]) -> list[Finding]:
        findings: list[Finding] = []
        candidates = [a for a in articles if a.fetch_status == "ok" and self._keyword_prefilter(a)]
        skipped = len(articles) - len(candidates)
        logger.info("Pre-filter: %d candidates sent to LLM, %d skipped (no keyword match / fetch issue)",
                    len(candidates), skipped)

        for article in candidates:
            try:
                results = self._classify_article(article)
                findings.extend(results)
            except Exception as e:
                logger.error("LLM classification failed for %s: %s", article.url, e)
            # Free-tier rate limits are the main constraint with Gemini —
            # a small delay between calls avoids tripping per-minute caps.
            time.sleep(2.0)

        return findings

    def _classify_article(self, article: Article, retries: int = 3) -> list[Finding]:
        user_content = (
            f"Article Title: {article.title}\n"
            f"Source: {article.source}\n"
            f"URL: {article.url}\n\n"
            f"Article Text:\n{article.full_text}"
        )

        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                response = self.client.models.generate_content(
                    model=MODEL,
                    contents=user_content,
                    config=types.GenerateContentConfig(
                        system_instruction=self.system_prompt,
                        response_mime_type="application/json",
                        max_output_tokens=1500,
                    ),
                )
                raw_text = (response.text or "").strip()
                return self._parse_findings(raw_text, article)

            except Exception as e:
                last_error = e
                # Gemini free tier commonly hits 429 (rate limit) — back off and retry.
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    wait = 15 * (attempt + 1)
                    logger.warning("Gemini rate limit hit, waiting %ds (attempt %d/%d)",
                                   wait, attempt + 1, retries)
                    time.sleep(wait)
                    continue
                raise

        logger.error("Gave up on %s after %d retries: %s", article.url, retries, last_error)
        return []

    def _parse_findings(self, raw_text: str, article: Article) -> list[Finding]:
        raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            parsed = json.loads(raw_text) if raw_text else []
        except json.JSONDecodeError:
            logger.warning("Non-JSON LLM response for %s; skipping. Raw: %.200s", article.url, raw_text)
            return []

        findings = []
        for item in parsed:
            try:
                findings.append(Finding(
                    company_name=item.get("company_name", "Unknown"),
                    sector=item.get("sector", "Unknown"),
                    location=item.get("location", "Not mentioned"),
                    category=item.get("category", "Unclassified"),
                    pain_point_or_opportunity=item.get("pain_point_or_opportunity", ""),
                    severity=item.get("severity", "Standard"),
                    source=article.source,
                    article_title=article.title,
                    article_url=article.url,
                ))
            except Exception as e:
                logger.warning("Malformed finding item skipped: %s (%s)", item, e)

        return findings
