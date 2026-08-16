"""
delivery.py
Implements SOP Section 5: Output and Notification Integration.
"""

import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

from analyzer import Finding

logger = logging.getLogger("mws_agent.delivery")


def build_markdown_report(findings: list[Finding], run_date: str, source_failures: list[dict]) -> str:
    lines = [f"# MWS Weekly Industrial Intelligence Summary — {run_date}", ""]

    if not findings:
        lines.append("_No actionable findings this week._")
    else:
        by_category: dict[str, list[Finding]] = {}
        for f in findings:
            by_category.setdefault(f.category, []).append(f)

        lines.append(f"**Total findings:** {len(findings)}  ")
        critical_count = sum(1 for f in findings if f.severity == "Critical")
        lines.append(f"**Critical items:** {critical_count}")
        lines.append("")

        for category, items in sorted(by_category.items()):
            lines.append(f"## {category} ({len(items)})")
            lines.append("")
            lines.append("| Company | Sector | Location | Pain Point / Opportunity | Severity | Source |")
            lines.append("|---|---|---|---|---|---|")
            for f in items:
                pain = f.pain_point_or_opportunity.replace("|", "/").replace("\n", " ")
                lines.append(
                    f"| {f.company_name} | {f.sector} | {f.location} | {pain} | "
                    f"{f.severity} | [{f.source}]({f.article_url}) |"
                )
            lines.append("")

    if source_failures:
        lines.append("## Source Health Issues (Section 6 review)")
        lines.append("")
        for fail in source_failures:
            lines.append(f"- **{fail['source']}** — `{fail['url']}` — {fail['detail']}")
        lines.append("")

    return "\n".join(lines)


def save_markdown(report_text: str, config: dict) -> str:
    out_dir = Path(config["output"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = config["output"]["filename_pattern"].format(date=datetime.now().strftime("%Y-%m-%d"))
    path = out_dir / filename
    path.write_text(report_text, encoding="utf-8")
    logger.info("Report saved to %s", path)
    return str(path)


def send_email(report_text: str, config: dict) -> None:
    email_cfg = config["delivery"]["email"]
    if not email_cfg.get("enabled"):
        logger.info("Email delivery disabled in config — skipping.")
        return

    password = os.environ.get(email_cfg["smtp_password_env_var"])
    if not password:
        logger.warning("Email enabled but %s env var not set — skipping send.",
                        email_cfg["smtp_password_env_var"])
        return

    msg = MIMEMultipart()
    msg["From"] = email_cfg["smtp_user"]
    msg["To"] = ", ".join(email_cfg["recipients"])
    msg["Subject"] = email_cfg["subject_pattern"].format(date=datetime.now().strftime("%Y-%m-%d"))
    msg.attach(MIMEText(report_text, "plain"))

    try:
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
            server.starttls()
            server.login(email_cfg["smtp_user"], password)
            server.sendmail(email_cfg["smtp_user"], email_cfg["recipients"], msg.as_string())
        logger.info("Email sent to %s", email_cfg["recipients"])
    except Exception as e:
        logger.error("Email delivery failed: %s", e)


def post_slack_alert(findings: list[Finding], config: dict) -> None:
    slack_cfg = config["delivery"]["slack"]
    if not slack_cfg.get("enabled"):
        logger.info("Slack delivery disabled in config — skipping.")
        return

    webhook_url = os.environ.get(slack_cfg["webhook_url_env_var"])
    if not webhook_url:
        logger.warning("Slack enabled but %s env var not set — skipping.",
                        slack_cfg["webhook_url_env_var"])
        return

    critical = [
        f for f in findings
        if f.severity == "Critical" and f.category in slack_cfg["alert_on_categories"]
    ]
    if not critical:
        logger.info("No critical findings to alert on Slack this run.")
        return

    text_lines = [f"*:rotating_light: {len(critical)} Critical Environmental Compliance finding(s) this week*"]
    for f in critical[:10]:
        text_lines.append(f"- *{f.company_name}* ({f.sector}, {f.location}): {f.pain_point_or_opportunity} — <{f.article_url}|source>")

    try:
        resp = requests.post(webhook_url, json={"text": "\n".join(text_lines)}, timeout=10)
        resp.raise_for_status()
        logger.info("Slack alert posted for %d critical findings.", len(critical))
    except Exception as e:
        logger.error("Slack delivery failed: %s", e)


def save_to_drive(report_path: str, config: dict) -> None:
    drive_cfg = config["delivery"]["drive"]
    if not drive_cfg.get("enabled"):
        logger.info("Google Drive delivery disabled in config — skipping.")
        return
    logger.warning(
        "Google Drive upload requires google-api-python-client + OAuth credentials, "
        "which are not configured in this environment. Skipping upload for %s. "
        "See README for setup instructions.", report_path
    )
