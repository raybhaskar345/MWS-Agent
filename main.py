"""
main.py
Entry point for the MWS Industrial Intelligence Agent.

Implements the full SOP pipeline end to end:
  1. Load config (sources + classification matrix)
  2. Scrape sources (Section 2)
  3. Classify via LLM (Section 3 & 4)
  4. Build + save report, send email/Slack/Drive (Section 5)
  5. Log source health issues for review (Section 6)

Usage:
    python main.py                # run once, now
    python main.py --dry-run      # scrape + classify but do not send email/Slack
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml

from scraper import SourceScraper
from analyzer import Analyzer
from delivery import build_markdown_report, save_markdown, send_email, post_slack_alert, save_to_drive

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def setup_logging():
    log_file = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )
    return log_file


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="MWS Industrial Intelligence Agent")
    parser.add_argument("--dry-run", action="store_true", help="Skip email/Slack/Drive delivery")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    args = parser.parse_args()

    log_file = setup_logging()
    logger = logging.getLogger("mws_agent.main")
    logger.info("=== MWS Industrial Intelligence Agent run started ===")

    config = load_config(args.config)

    # --- Section 2: Scrape ---
    scraper = SourceScraper(config)
    articles = scraper.run()
    logger.info("Scraping complete: %d total articles fetched", len(articles))

    # --- Section 3 & 4: Classify ---
    analyzer = Analyzer(config)
    findings = analyzer.analyze(articles)
    logger.info("Classification complete: %d findings extracted", len(findings))

    # --- Section 5: Output ---
    run_date = datetime.now().strftime("%Y-%m-%d")
    report_text = build_markdown_report(findings, run_date, scraper.failures)
    report_path = save_markdown(report_text, config)

    if not args.dry_run:
        send_email(report_text, config)
        post_slack_alert(findings, config)
        save_to_drive(report_path, config)
    else:
        logger.info("Dry run — skipping email/Slack/Drive delivery.")

    # --- Section 6: Review logging ---
    if scraper.failures:
        logger.warning("Source health issues this run: %d (see report + log for detail)", len(scraper.failures))

    logger.info("=== Run complete. Report: %s | Log: %s ===", report_path, log_file)
    print(f"\nDone. Report written to: {report_path}")
    print(f"Full log: {log_file}")


if __name__ == "__main__":
    main()
