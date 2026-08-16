"""
analyzer.py
Implements SOP Section 3 (Prompt Engineering / Intelligence Layer) and
Section 4 (Logical Filtering and Semantic Search).

Uses Google's Gemini API (free tier) to classify scraped articles into the
Use Case Classification Matrix. A cheap keyword pre-filter (Section 4's
"semantic search" keyword lists) trims the candidate set before the
LLM pass, to save on API calls for obviously irrelevant articles.

Get a free API key at: https://aistudio.google.com/apikey

MODEL NOTE: Google frequently retires specific dated model IDs (e.g.
gemini-2.5-flash was cut off for new API keys well ahead of its official
shutdown date). To avoid this breaking the agent again, this file targets
the "gemini-flash-latest" alias, which Google automatically points at
its current flash-tier model — no code change needed when they release a
new version. If you ever see a 404 "model no longer available" error
again despite this, check https://ai.google.dev/gemini-api/docs/models
for the current alias name and update MODEL below.

Free tier limits (check current values at ai.google.dev/pricing before
relying on them — they change and have been cut significantly over time,
sometimes down to ~15-20 requests/day on some accounts). If you exceed
it, calls will 429; the _classify_article retry/backoff below handles
transient limit hits, but a very tight daily cap may require spacing
runs out further or upgrading to a low-cost paid tier.
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

# "gemini-flash-latest" is an alias Google keeps pointed at their current
# flash-tier model, so this doesn't break every time a dated model ID
# (like gemini-2.5-flash) gets retired. Free-tier friendly vs. "pro".
MODEL = "gemini-flash-latest"


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
            except RuntimeError as e:
                # Raised by _classify_article for config-level problems (e.g. a
                # retired model ID) that won't resolve by trying more articles.
                logger.error("Stopping run: %s", e)
                raise
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
                error_str = str(e)

                # A 404 here almost always means Google retired this model ID —
                # this is a config problem, not a per-article problem, so fail
                # loudly immediately instead of retrying/burning through articles.
                if "404" in error_str and ("NOT_FOUND" in error_str or "no longer available" in error_str):
                    raise RuntimeError(
                        f"Gemini model '{MODEL}' appears to have been retired by Google "
                        f"(404 NOT_FOUND). Check https://ai.google.dev/gemini-api/docs/models "
                        f"for the current model alias and update MODEL in analyzer.py. "
                        f"Original error: {error_str}"
                    ) from e

                # Gemini free tier commonly hits 429 (rate limit) — back off and retry.
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    wait = 15 * (attempt + 1)
                    logger.warning("Gemini rate limit hit, waiting %ds (attempt %d/%d)",
                                   wait, attempt + 1, retries)
                    time.sleep(wait)
                    continue

                # 503 UNAVAILABLE means the model is transiently overloaded on
                # Google's end — this is temporary and worth retrying, distinct
                # from a rate-limit (429) or a genuinely broken request.
                if "503" in error_str or "UNAVAILABLE" in error_str:
                    wait = 10 * (attempt + 1)
                    logger.warning("Gemini reported high demand (503), waiting %ds (attempt %d/%d)",
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
