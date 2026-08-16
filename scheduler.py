"""
scheduler.py
Implements SOP Section 2 scheduling requirement:
  "set the frequency to Weekly... Monday mornings at 08:00 IST"

This runs main.py's pipeline in-process on a recurring schedule, so the
agent can run continuously (e.g. as a systemd service or in a long-lived
container) instead of relying on an external cron daemon.

If you'd rather use system cron, skip this file and add a line like:
    0 8 * * 1 cd /path/to/mws_agent && /usr/bin/python3 main.py >> logs/cron.log 2>&1
(crontab times are in the server's local timezone — adjust for IST if needed)
"""

import logging
import time

import schedule  # pip install schedule
import pytz
from datetime import datetime

from main import main as run_pipeline

logger = logging.getLogger("mws_agent.scheduler")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

IST = pytz.timezone("Asia/Kolkata")


def job():
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    logger.info("Triggering scheduled run at %s", now_ist)
    try:
        run_pipeline()
    except Exception as e:
        logger.exception("Scheduled run failed: %s", e)


def run_scheduler():
    # 'schedule' library uses the machine's local time. If this process runs
    # on a server NOT in IST, convert 08:00 IST to that server's local time
    # before setting this, or run the process itself in Asia/Kolkata (e.g.
    # TZ=Asia/Kolkata python scheduler.py).
    schedule.every().monday.at("08:00").do(job)
    logger.info("Scheduler started. Waiting for next Monday 08:00 (server local time / IST if TZ set).")

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    run_scheduler()
