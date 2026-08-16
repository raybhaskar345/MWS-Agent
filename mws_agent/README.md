# MWS Industrial Intelligence Agent

An automated agent that implements the SOP **"Deploying Industrial Intelligence
Agents"**: it monitors business publications, extracts industrial use-case
signals (environmental compliance, water scarcity, capacity expansion, etc.),
and delivers a weekly Markdown/table summary.

This is a self-contained Python implementation of the SOP — it doesn't depend
on VSGPT/ChatGPT's Agent Builder UI, but follows the exact same steps and
logic, so it will also work if you later decide to port it into VSGPT/ChatGPT
Agents or another platform instead.

## What it does (mapped to the SOP)

| SOP Section | Implementation |
|---|---|
| 1. Source Definition | `config.yaml` → `sources:` |
| 2. Scraping Workflow | `scraper.py` (RSS + depth-2 HTML crawl, weekly schedule) |
| 3. Prompt / Classification Matrix | `analyzer.py` (Gemini API call using the SOP's system prompt + matrix) |
| 4. Semantic keyword pre-filter | `analyzer.py::_keyword_prefilter` |
| 5. Output & Notification | `delivery.py` (Markdown report, email, Slack, Drive stub) |
| 6. Review & Refinement | Source-health failures logged in report + `logs/` |

## Setup

```bash
cd mws_agent
pip install -r requirements.txt
```

Get a **free** Gemini API key at https://aistudio.google.com/apikey (sign in
with a Google account, click "Create API key" — no billing required for the
free tier), then set it as an environment variable:
```bash
export GEMINI_API_KEY="your-key-here"
```

**Free tier note:** Gemini 2.5 Flash's free tier has per-minute and per-day
request caps (check current numbers at https://ai.google.dev/pricing since
they change). The agent already adds a delay between calls and retries with
backoff on rate-limit errors, but if you have a large number of articles in
a given week, a run may take longer than an equivalent paid-tier run, or
you may need to split it across a day. If you outgrow the free tier,
`analyzer.py` isolates all the model-calling logic in one file, so swapping
back to Claude or another provider later only requires editing that file.

### 1. Fill in real source URLs
Open `config.yaml` and replace every `TODO: ...` URL under `sources:` with
the actual RSS feed or section URL for that publication. Business Standard's
RSS URLs are provided as a starting guess — verify they still resolve.
Industry Today, Indian Infrastructure Research, CPCB, and NGT need URLs
confirmed manually (their RSS availability and site structure change).

### 2. (Optional) Enable delivery channels
In `config.yaml` under `delivery:`:
- **Email**: set `enabled: true`, fill in SMTP host/user/recipients, and set
  the password via environment variable (`export MWS_SMTP_PASSWORD=...`) —
  never hardcode it in the file.
- **Slack**: set `enabled: true` and `export MWS_SLACK_WEBHOOK_URL=...`
  (create an Incoming Webhook in your Slack workspace).
- **Google Drive**: stubbed out — `delivery.py::save_to_drive` logs a
  reminder. Wire in `google-api-python-client` + OAuth if you want this live.

## Running

**One-off run (recommended first, to sanity-check output):**
```bash
python main.py --dry-run
```
This scrapes, classifies, and writes the report to `output/`, but skips
email/Slack/Drive.

**Full run with delivery:**
```bash
python main.py
```

**Continuous weekly scheduling** (Monday 08:00 IST, per SOP Section 2):
```bash
TZ=Asia/Kolkata python scheduler.py
```
Keep this running in the background (e.g. `tmux`, a systemd service, or a
Docker container). Alternatively, use system cron instead of `scheduler.py`
— see the comment at the top of that file for the crontab line.

## Output

Each run writes `output/weekly_intel_YYYY-MM-DD.md`: findings grouped by
category (Environmental Compliance, Water Scarcity, Rapid Expansion,
Monetary Loss, Ultrapure Quality, Deferment of Capital Costs), each row with
Company / Sector / Location / Pain Point / Severity / Source link. Any
source-health issues (blocked scrapers, broken feeds) are listed at the
bottom for the weekly review step.

## Section 6 — Review & Refinement checklist

After each run, check:
- **False positives** — generic economic news with no company-level signal
  slipping through. Tighten `classification_matrix` keywords/logic in
  `config.yaml` if so.
- **Source health** — see the "Source Health Issues" section of the
  generated report. If Business Standard (or others) starts returning
  403/429s consistently, its RSS feed URL may have changed, or it needs a
  different fetch strategy (e.g. a licensed API instead of scraping).
- **Prompt tuning** — edit `system_prompt` / matrix `logic` fields in
  `config.yaml` if the model is missing subtle signals like "operational
  delays." No code changes needed — the prompt is fully config-driven.

## Notes / limitations

- Sites with hard paywalls or aggressive bot detection (flagged in the SOP
  for Business Standard) may still block scraping even with a browser-like
  User-Agent. If that happens consistently, consider a licensed news API or
  RSS-only mode for that source.
- CPCB/NGT portals often publish notices as PDFs or in non-standard HTML;
  `section_urls` crawling may need source-specific parsing once real URLs
  are confirmed — flag it and I can add a custom parser for that page
  structure.
- The Google Drive upload is a stub — tell me if you want it wired up with
  real OAuth (I'll need your Drive folder ID and to walk you through the
  one-time auth flow).
