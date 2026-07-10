#!/usr/bin/env python3
"""
+==================================================================+
|         THE DAILY SCOOP -- CRON DELIVERY                         |
|                                                                  |
|  Orchestrates the full newspaper pipeline and delivers the       |
|  compiled edition to your inbox every morning at 7:30 AM IST.    |
|                                                                  |
|  Pipeline:                                                       |
|    news_fetcher.py -> content_generator.py -> SMTP delivery      |
|                                                                  |
|  Usage:                                                          |
|    python cron_delivery.py                   (run once now)       |
|    python cron_delivery.py --schedule        (daemon at 7:30 AM) |
|    python cron_delivery.py --install         (Windows Task Sched) |
|    python cron_delivery.py --test-email      (send test email)   |
|                                                                  |
+==================================================================+
"""

import os
import re
import sys
import json
import time
import logging
import smtplib
import argparse
import subprocess
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

# -- Fix Windows console encoding ------------------------------------------
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# -- Third-party imports ---------------------------------------------------
try:
    from dotenv import load_dotenv
except ImportError:
    print("\nMissing python-dotenv. Run: pip install python-dotenv\n")
    sys.exit(1)

# -- Load environment ------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

# -- Logging ---------------------------------------------------------------
LOG_DIR = SCRIPT_DIR / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            LOG_DIR / "cron_delivery.log",
            encoding="utf-8",
            mode="a",
        ),
    ],
)
log = logging.getLogger("cron")

# -- Timezone --------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))

# ==========================================================================
#                          CONFIGURATION
# ==========================================================================

# -- SMTP (Gmail) ----------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("GMAIL_ADDRESS", "")
SMTP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
DELIVERY_TO = os.getenv("DELIVERY_TO", SMTP_USER)  # defaults to self

# -- Pipeline scripts ------------------------------------------------------
NEWS_FETCHER = SCRIPT_DIR / "news_fetcher.py"
CONTENT_GENERATOR = SCRIPT_DIR / "content_generator.py"

# -- Template (dynamically pulled each run) --------------------------------
TEMPLATE_PATH = SCRIPT_DIR / "newspaper_template.html"

# -- Output paths ----------------------------------------------------------
EDITIONS_DIR = SCRIPT_DIR / "data" / "editions"
FEEDS_DIR = Path(os.getenv("OUTPUT_DIR", str(SCRIPT_DIR / "data" / "datasets")))

# -- Schedule (24h format, IST) -------------------------------------------
SCHEDULE_HOUR = int(os.getenv("SCHEDULE_HOUR", "7"))
SCHEDULE_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "30"))

# -- Retry configuration ---------------------------------------------------
SMTP_MAX_RETRIES = 3
SMTP_RETRY_DELAY = 5  # seconds (doubles each retry)

# -- Freshness threshold (max age of feed data before considered stale) ----
FEED_STALE_HOURS = 18  # if feed is older than 18h, re-fetch

# -- Python executable -----------------------------------------------------
PYTHON = sys.executable


# ==========================================================================
#                     PIPELINE EXECUTION
# ==========================================================================

def run_step(script_path: Path, step_name: str, extra_args: list = None) -> bool:
    """
    Run a pipeline script as a subprocess.
    Returns True on success, False on failure.
    """
    if not script_path.exists():
        log.error(f"  [{step_name}] Script not found: {script_path}")
        return False

    cmd = [PYTHON, str(script_path)]
    if extra_args:
        cmd.extend(extra_args)

    log.info(f"  [{step_name}] Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,  # 5 minute timeout per step
        )

        # Log stdout (last 30 lines to avoid flooding)
        if result.stdout:
            lines = result.stdout.strip().split("\n")
            for line in lines[-30:]:
                log.info(f"  [{step_name}] {line}")

        if result.returncode != 0:
            log.error(f"  [{step_name}] FAILED (exit code {result.returncode})")
            if result.stderr:
                for line in result.stderr.strip().split("\n")[-15:]:
                    log.error(f"  [{step_name}] stderr: {line}")
            return False

        log.info(f"  [{step_name}] OK")
        return True

    except subprocess.TimeoutExpired:
        log.error(f"  [{step_name}] TIMEOUT (>300s)")
        return False
    except Exception as e:
        log.error(f"  [{step_name}] Exception: {e}")
        return False


# ==========================================================================
#                   DYNAMIC COMPONENT HOOKUP CHECKS
# ==========================================================================

def verify_feed_freshness() -> dict:
    """
    Check whether today's feed data exists and is fresh.
    Returns a dict with 'exists', 'path', 'is_stale', 'age_hours'.
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    today_feed = FEEDS_DIR / f"newspaper_feed_{today}.json"

    result = {"exists": False, "path": None, "is_stale": True, "age_hours": None}

    # Check for today's exact feed first
    if today_feed.exists():
        result["exists"] = True
        result["path"] = today_feed
    else:
        # Fall back to the most recent feed
        feeds = sorted(FEEDS_DIR.glob("newspaper_feed_*.json"), reverse=True)
        if feeds:
            result["exists"] = True
            result["path"] = feeds[0]

    if result["path"] and result["path"].exists():
        mod_time = datetime.fromtimestamp(
            result["path"].stat().st_mtime, tz=IST
        )
        age = datetime.now(IST) - mod_time
        result["age_hours"] = age.total_seconds() / 3600
        result["is_stale"] = result["age_hours"] > FEED_STALE_HOURS
    else:
        result["is_stale"] = True

    return result


def verify_template_available() -> bool:
    """Verify that newspaper_template.html exists and is non-empty."""
    if not TEMPLATE_PATH.exists():
        log.error(f"  Template missing: {TEMPLATE_PATH}")
        return False
    size = TEMPLATE_PATH.stat().st_size
    if size < 100:
        log.error(f"  Template suspiciously small ({size} bytes): {TEMPLATE_PATH}")
        return False
    log.info(f"  Template OK: {TEMPLATE_PATH.name} ({size / 1024:.1f} KB)")
    return True


def verify_edition_generated() -> Path | None:
    """
    Find the freshly compiled edition HTML.
    Search order:
      1. Today's dated edition in data/editions/
      2. Most recent edition in data/editions/
      3. newspaper_preview.html (if recently updated)
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    EDITIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Priority 1: Today's exact edition
    today_edition = EDITIONS_DIR / f"the_daily_scoop_{today}.html"
    if today_edition.exists():
        log.info(f"  Found today's edition: {today_edition.name}")
        return today_edition

    # Priority 2: Most recent edition (in case date formatting differs)
    editions = sorted(EDITIONS_DIR.glob("the_daily_scoop_*.html"), reverse=True)
    if editions:
        latest = editions[0]
        mod_time = datetime.fromtimestamp(latest.stat().st_mtime, tz=IST)
        age_mins = (datetime.now(IST) - mod_time).total_seconds() / 60
        # Only use if generated within the last 30 minutes (i.e., this pipeline run)
        if age_mins < 30:
            log.info(f"  Found recent edition: {latest.name} ({age_mins:.0f}m ago)")
            return latest

    # Priority 3: Preview file (used during dev/testing)
    preview = SCRIPT_DIR / "newspaper_preview.html"
    if preview.exists():
        mod_time = datetime.fromtimestamp(preview.stat().st_mtime, tz=IST)
        age_mins = (datetime.now(IST) - mod_time).total_seconds() / 60
        if age_mins < 30:
            log.info(f"  Found preview: {preview.name} ({age_mins:.0f}m ago)")
            return preview

    # Nothing found
    return None


def find_latest_edition() -> Path | None:
    """Find the most recently generated HTML edition."""
    EDITIONS_DIR.mkdir(parents=True, exist_ok=True)
    editions = sorted(EDITIONS_DIR.glob("the_daily_scoop_*.html"), reverse=True)
    if editions:
        return editions[0]
    return None


def find_todays_edition() -> Path | None:
    """Find today's specific edition."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    path = EDITIONS_DIR / f"the_daily_scoop_{today}.html"
    if path.exists():
        return path
    return find_latest_edition()


# ==========================================================================
#                       EMAIL DELIVERY
# ==========================================================================

def _smtp_send(msg: MIMEMultipart, recipient: str) -> bool:
    """
    Low-level SMTP send with retry and exponential backoff.
    Handles transient network failures that are common at 7:30 AM.
    """
    delay = SMTP_RETRY_DELAY

    for attempt in range(1, SMTP_MAX_RETRIES + 1):
        try:
            log.info(f"  SMTP attempt {attempt}/{SMTP_MAX_RETRIES} → {SMTP_HOST}:{SMTP_PORT}")

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                log.info("  TLS established, authenticating...")
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, [recipient], msg.as_string())

            log.info(f"  Email delivered to {recipient}")
            return True

        except smtplib.SMTPAuthenticationError as e:
            log.error(f"  SMTP auth failed: {e}")
            log.error("  Check your Gmail App Password in .env")
            log.error("  Generate one at: https://myaccount.google.com/apppasswords")
            return False  # Don't retry auth errors

        except (smtplib.SMTPException, ConnectionError, OSError) as e:
            log.warning(f"  SMTP attempt {attempt} failed: {e}")
            if attempt < SMTP_MAX_RETRIES:
                log.info(f"  Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2  # Exponential backoff
            else:
                log.error(f"  All {SMTP_MAX_RETRIES} SMTP attempts failed.")
                return False

        except Exception as e:
            log.error(f"  Unexpected email error: {e}")
            log.error(traceback.format_exc())
            return False

    return False


def send_email(html_path: Path) -> bool:
    """
    Send the compiled newspaper edition via Gmail SMTP.
    Uses TLS (port 587) with App Password authentication.
    Includes retry logic for transient network failures.
    """
    if not SMTP_USER or not SMTP_PASSWORD:
        log.error("Email delivery failed: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set in .env")
        return False

    recipient = DELIVERY_TO
    today = datetime.now(IST)
    subject = f"The Daily Scoop — {today.strftime('%A, %B %d, %Y')}"

    log.info(f"  Preparing email to: {recipient}")
    log.info(f"  Subject: {subject}")
    log.info(f"  Edition file: {html_path.name} ({html_path.stat().st_size / 1024:.1f} KB)")

    try:
        # Read the compiled HTML
        html_content = html_path.read_text(encoding="utf-8")

        # Build the email
        msg = MIMEMultipart("alternative")
        msg["From"] = f"The Daily Scoop <{SMTP_USER}>"
        msg["To"] = recipient
        msg["Subject"] = subject
        msg["X-Mailer"] = "The Daily Scoop Pipeline v2.0"
        msg["X-Priority"] = "3"  # Normal priority

        # Plain text fallback
        plain_text = (
            f"The Daily Scoop — {today.strftime('%A, %B %d, %Y')}\n\n"
            f"Your daily newspaper is ready! Open this email in an HTML-capable "
            f"client to see the full styled edition.\n\n"
            f"--\n"
            f"Brewed with love and a little caffeine."
        )
        msg.attach(MIMEText(plain_text, "plain", "utf-8"))

        # HTML version (the actual newspaper)
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        # Send with retry
        return _smtp_send(msg, recipient)

    except Exception as e:
        log.error(f"  Email preparation failed: {e}")
        log.error(traceback.format_exc())
        return False


def send_failure_notification(step_name: str, error_details: str) -> None:
    """
    Send a brief failure alert email when the pipeline breaks.
    This ensures you always know if your morning paper didn't generate,
    even when the pipeline runs unattended via Task Scheduler.
    """
    if not SMTP_USER or not SMTP_PASSWORD:
        return  # Can't send alerts without SMTP creds

    recipient = DELIVERY_TO
    today = datetime.now(IST)

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"The Daily Scoop <{SMTP_USER}>"
        msg["To"] = recipient
        msg["Subject"] = f"⚠ Daily Scoop FAILED — {today.strftime('%b %d')} — {step_name}"
        msg["X-Priority"] = "1"  # High priority

        # Truncate error details for email
        error_preview = error_details[:500] if error_details else "No details available"

        html = f"""
        <div style="font-family: Georgia, serif; max-width: 480px; margin: 40px auto;
                    padding: 32px; background: #FFF5F5; border-radius: 16px;
                    border: 1px solid #FED7D7; text-align: center;">
            <h2 style="color: #C53030; letter-spacing: 2px; font-weight: 400; font-size: 16px;">
                PIPELINE FAILURE ALERT
            </h2>
            <div style="margin: 16px auto; width: 60px;">
                <span style="display:inline-block; width:60px; height:2px; background:#FC8181; border-radius:2px;"></span>
            </div>
            <p style="color: #2D2D2D; font-size: 15px; font-weight: 600;">
                Step Failed: {step_name}
            </p>
            <p style="color: #718096; font-size: 13px; line-height: 1.6;">
                {today.strftime('%A, %B %d, %Y at %H:%M:%S IST')}
            </p>
            <div style="background: #FFF; border: 1px solid #FED7D7; border-radius: 8px;
                        padding: 16px; margin: 16px 0; text-align: left;">
                <pre style="color: #C53030; font-size: 11px; white-space: pre-wrap;
                            font-family: monospace; margin: 0;">{error_preview}</pre>
            </div>
            <p style="color: #A0AEC0; font-size: 11px; margin-top: 24px;">
                Check the full log: data/logs/cron_delivery.log
            </p>
        </div>
        """
        msg.attach(MIMEText(f"Pipeline failed at: {step_name}\n\n{error_preview}", "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [recipient], msg.as_string())

        log.info(f"  Failure alert sent to {recipient}")

    except Exception as e:
        log.warning(f"  Could not send failure alert: {e}")


def send_test_email() -> bool:
    """Send a minimal test email to verify SMTP configuration."""
    if not SMTP_USER or not SMTP_PASSWORD:
        log.error("Cannot send test: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set in .env")
        return False

    recipient = DELIVERY_TO
    log.info(f"Sending test email to {recipient}...")

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"The Daily Scoop <{SMTP_USER}>"
        msg["To"] = recipient
        msg["Subject"] = "The Daily Scoop — SMTP Test"

        html = """
        <div style="font-family: Georgia, serif; max-width: 480px; margin: 40px auto;
                    padding: 32px; background: #FDFBF7; border-radius: 16px;
                    border: 1px solid #F0EBE5; text-align: center;">
            <h2 style="color: #2D2D2D; letter-spacing: 4px; font-weight: 400;">
                THE DAILY SCOOP
            </h2>
            <div style="margin: 16px auto; width: 100px;">
                <span style="display:inline-block; width:28px; height:2px; background:#F2C4C0; border-radius:2px;"></span>
                <span style="display:inline-block; width:28px; height:2px; background:#B8D4B0; border-radius:2px; margin:0 4px;"></span>
                <span style="display:inline-block; width:28px; height:2px; background:#C5B8D4; border-radius:2px;"></span>
            </div>
            <p style="color: #4A7C4A; font-size: 18px; font-weight: 600;">
                SMTP Configuration Verified
            </p>
            <p style="color: #8A7D76; font-size: 14px; line-height: 1.6;">
                Your email delivery pipeline is working perfectly.<br>
                You'll receive The Daily Scoop every morning at 7:30 AM IST.
            </p>
            <p style="color: #B5A9A2; font-size: 11px; margin-top: 24px;">
                Brewed with love and a little caffeine.
            </p>
        </div>
        """
        msg.attach(MIMEText("SMTP test successful. The Daily Scoop is ready.", "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        return _smtp_send(msg, recipient)

    except Exception as e:
        log.error(f"Test email failed: {e}")
        return False


# ==========================================================================
#                    FULL PIPELINE ORCHESTRATION
# ==========================================================================

def run_full_pipeline() -> bool:
    """
    Execute the complete Daily Scoop pipeline:
      1. Pre-flight: Validate all components hook together
      2. Fetch raw news data (news_fetcher.py)
      3. Generate LLM content (content_generator.py → newspaper_template.html)
      4. Deliver compiled HTML via secure SMTP

    Each step validates the previous step's output before proceeding,
    ensuring dynamic inter-component hookup integrity.
    """
    now = datetime.now(IST)
    log.info("=" * 56)
    log.info(f"  PIPELINE START: {now.strftime('%A, %B %d, %Y at %H:%M:%S IST')}")
    log.info("=" * 56)

    results = {
        "preflight": False,
        "fetch": False,
        "generate": False,
        "deliver": False,
    }

    # -- Step 0: Pre-flight component hookup check -------------------------
    log.info("STEP 0/4: Verifying component hookup...")

    if not verify_template_available():
        log.error("Pipeline aborted: newspaper_template.html missing.")
        send_failure_notification("Pre-flight", "newspaper_template.html not found")
        _log_summary(results, now)
        return False

    if not NEWS_FETCHER.exists():
        log.error(f"Pipeline aborted: {NEWS_FETCHER} not found.")
        send_failure_notification("Pre-flight", f"Script missing: {NEWS_FETCHER}")
        _log_summary(results, now)
        return False

    if not CONTENT_GENERATOR.exists():
        log.error(f"Pipeline aborted: {CONTENT_GENERATOR} not found.")
        send_failure_notification("Pre-flight", f"Script missing: {CONTENT_GENERATOR}")
        _log_summary(results, now)
        return False

    results["preflight"] = True
    log.info("  All components verified and hooked up.")

    # -- Step 1: Fetch news ------------------------------------------------
    log.info("STEP 1/4: Fetching news data...")

    # Check if today's feed already exists and is fresh
    feed_status = verify_feed_freshness()
    if feed_status["exists"] and not feed_status["is_stale"]:
        log.info(f"  Fresh feed found ({feed_status['age_hours']:.1f}h old). Skipping re-fetch.")
        results["fetch"] = True
    else:
        if feed_status["exists"] and feed_status["is_stale"]:
            log.info(f"  Feed exists but is stale ({feed_status['age_hours']:.1f}h old). Re-fetching...")
        results["fetch"] = run_step(NEWS_FETCHER, "Fetch")

    if not results["fetch"]:
        log.error("Pipeline aborted: news fetch failed.")
        send_failure_notification("News Fetch", "news_fetcher.py returned a non-zero exit code")
        _log_summary(results, now)
        return False

    # Validate that feed output actually exists after fetch
    feed_status = verify_feed_freshness()
    if not feed_status["exists"]:
        log.error("Pipeline aborted: no feed JSON found after fetch step!")
        send_failure_notification("News Fetch", "news_fetcher.py ran but produced no output JSON")
        _log_summary(results, now)
        return False
    log.info(f"  Feed validated: {feed_status['path'].name} ({feed_status['age_hours']:.1f}h old)")

    # -- Step 2: Generate content ------------------------------------------
    log.info("STEP 2/4: Generating content via LLM...")
    log.info(f"  Template source: {TEMPLATE_PATH.name}")
    log.info(f"  Feed source: {feed_status['path'].name}")

    results["generate"] = run_step(CONTENT_GENERATOR, "Generate")

    if not results["generate"]:
        log.error("Pipeline aborted: content generation failed.")
        send_failure_notification(
            "Content Generation",
            "content_generator.py failed. Check LLM API key and feed data."
        )
        _log_summary(results, now)
        return False

    # Validate that an edition was actually produced
    edition = verify_edition_generated()
    if not edition:
        log.error("Pipeline aborted: content_generator ran but no edition HTML found!")
        send_failure_notification(
            "Content Generation",
            "content_generator.py exited OK but no edition HTML was produced"
        )
        _log_summary(results, now)
        return False

    # -- Step 3: Email delivery --------------------------------------------
    log.info("STEP 3/4: Delivering via secure SMTP...")
    log.info(f"  Edition: {edition.name} ({edition.stat().st_size / 1024:.1f} KB)")
    log.info(f"  Recipient: {DELIVERY_TO}")
    log.info(f"  SMTP: {SMTP_HOST}:{SMTP_PORT} (TLS)")

    results["deliver"] = send_email(edition)

    if not results["deliver"]:
        send_failure_notification("Email Delivery", f"Failed to deliver {edition.name} via SMTP")

    # -- Summary -----------------------------------------------------------
    _log_summary(results, now)
    return all(results.values())


def _log_summary(results: dict, start_time: datetime):
    """Print a pipeline execution summary."""
    elapsed = (datetime.now(IST) - start_time).total_seconds()

    def status(ok):
        return "OK" if ok else "FAIL"

    # Handle both 3-step and 4-step result dicts gracefully
    has_preflight = "preflight" in results

    print()
    print("  +---------------------------------------------+")
    print("  |          PIPELINE EXECUTION SUMMARY         |")
    print("  +---------------------------------------------+")
    if has_preflight:
        print(f"  |  0. Pre-flight:       [{status(results['preflight']):>4}]              |")
    print(f"  |  1. News Fetch:        [{status(results['fetch']):>4}]              |")
    print(f"  |  2. Content Generate:  [{status(results['generate']):>4}]              |")
    print(f"  |  3. Email Delivery:    [{status(results['deliver']):>4}]              |")
    print(f"  |  Elapsed:              {elapsed:>5.0f}s              |")
    print("  +---------------------------------------------+")

    if all(results.values()):
        print("  |  Result: ALL STEPS PASSED                  |")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"  |  Result: FAILED at: {', '.join(failed):<22} |")
    print("  +---------------------------------------------+")
    print()


# ==========================================================================
#                    SCHEDULE / DAEMON MODE
# ==========================================================================

def run_scheduler():
    """
    Run as a persistent daemon that triggers the pipeline at 7:30 AM IST daily.
    Uses a simple sleep loop -- no external scheduler dependency needed.
    """
    log.info("+" + "-" * 54 + "+")
    log.info("|  DAILY SCOOP SCHEDULER -- DAEMON MODE                |")
    log.info(f"|  Scheduled: {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} IST daily" + " " * 26 + "|")
    log.info(f"|  Delivering to: {DELIVERY_TO:<37}|")
    log.info(f"|  SMTP: {SMTP_HOST}:{SMTP_PORT} (TLS + retry)" + " " * 13 + "|")
    log.info("+" + "-" * 54 + "+")
    log.info("Daemon started. Press Ctrl+C to stop.\n")

    last_run_date = None

    try:
        while True:
            now = datetime.now(IST)
            today_date = now.date()

            # Check if it's time to run (within the target minute, and not already run today)
            if (
                now.hour == SCHEDULE_HOUR
                and now.minute == SCHEDULE_MINUTE
                and last_run_date != today_date
            ):
                log.info(f"Trigger! It's {now.strftime('%H:%M')} IST. Starting pipeline...")
                last_run_date = today_date

                try:
                    success = run_full_pipeline()
                    if success:
                        log.info("Pipeline completed. Next run tomorrow.")
                    else:
                        log.error("Pipeline had failures. Check logs above.")
                except Exception as e:
                    log.error(f"Pipeline crashed: {e}")
                    log.error(traceback.format_exc())
                    send_failure_notification("Pipeline Crash", traceback.format_exc())

            # Sleep 30 seconds between checks (responsive but light)
            time.sleep(30)

    except KeyboardInterrupt:
        log.info("\nDaemon stopped by user.")
        sys.exit(0)


# ==========================================================================
#              WINDOWS TASK SCHEDULER INSTALLATION
# ==========================================================================

def install_windows_task():
    """
    Register the pipeline as a Windows Task Scheduler task that runs
    daily at 7:30 AM IST. Does NOT require the Python daemon to be running.

    Uses XML-based task definition to enable:
      - StartWhenAvailable: If the PC was asleep/off at the scheduled time,
        the task runs automatically as soon as the machine wakes up.
      - ExecutionTimeLimit: 30 minutes max runtime to avoid zombie processes.
    """
    task_name = "TheDailyScoopPipeline"
    script_path = str(SCRIPT_DIR / "cron_delivery.py")
    python_path = PYTHON

    log.info("Installing Windows Scheduled Task...")
    log.info(f"  Task Name:  {task_name}")
    log.info(f"  Schedule:   Daily at {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}")
    log.info(f"  StartWhenAvailable: YES (catches missed runs)")
    log.info(f"  Command:    {python_path} {script_path}")

    # Build XML task definition -- this lets us set StartWhenAvailable,
    # which the basic schtasks CLI doesn't support.
    import getpass
    username = getpass.getuser()

    task_xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>The Daily Scoop - automated newspaper pipeline. Fetches news, generates content via LLM, and delivers via email.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python_path}</Command>
      <Arguments>"{script_path}"</Arguments>
      <WorkingDirectory>{str(SCRIPT_DIR)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""

    # Write XML to a temp file, then import it
    xml_path = SCRIPT_DIR / "data" / "_task_definition.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        xml_path.write_text(task_xml, encoding="utf-16")

        cmd = [
            "schtasks", "/Create",
            "/TN", task_name,
            "/XML", str(xml_path),
            "/F",  # Force overwrite if exists
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        # Clean up temp XML
        try:
            xml_path.unlink()
        except Exception:
            pass

        if result.returncode == 0:
            log.info(f"  Task '{task_name}' installed successfully!")
            log.info(f"  The pipeline will run every day at {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}.")
            log.info(f"  If the PC is asleep, it will run as soon as it wakes up.")
            log.info("  To verify: schtasks /Query /TN TheDailyScoopPipeline")
            log.info("  To remove: schtasks /Delete /TN TheDailyScoopPipeline /F")
            return True
        else:
            log.error(f"  Task installation failed (exit {result.returncode})")
            if result.stderr:
                log.error(f"  {result.stderr.strip()}")
            log.info("  TIP: Try running as Administrator if you get access errors.")
            return False

    except Exception as e:
        log.error(f"  Installation failed: {e}")
        return False


def uninstall_windows_task():
    """Remove the Windows Task Scheduler task."""
    task_name = "TheDailyScoopPipeline"
    try:
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            log.info(f"Task '{task_name}' removed.")
        else:
            log.error(f"Could not remove task: {result.stderr.strip()}")
    except Exception as e:
        log.error(f"Uninstall failed: {e}")


# ==========================================================================
#                          PRE-FLIGHT CHECK
# ==========================================================================

def preflight_check():
    """Verify all pipeline components exist and are configured."""
    issues = []

    # Check scripts
    if not NEWS_FETCHER.exists():
        issues.append(f"Missing: {NEWS_FETCHER}")
    if not CONTENT_GENERATOR.exists():
        issues.append(f"Missing: {CONTENT_GENERATOR}")

    # Check rules.md
    rules_path = SCRIPT_DIR / "rules.md"
    if not rules_path.exists():
        issues.append(f"Missing: {rules_path}")

    # Check template
    if not TEMPLATE_PATH.exists():
        issues.append(f"Missing: {TEMPLATE_PATH}")
    elif TEMPLATE_PATH.stat().st_size < 100:
        issues.append(f"Template too small: {TEMPLATE_PATH}")

    # Check SMTP credentials
    if not SMTP_USER:
        issues.append("GMAIL_ADDRESS not set in .env")
    if not SMTP_PASSWORD:
        issues.append("GMAIL_APP_PASSWORD not set in .env")

    # Check LLM API keys (dual Gemini key support)
    gk1 = os.getenv("GEMINI_API_KEY_1", "").strip()
    gk2 = os.getenv("GEMINI_API_KEY_2", "").strip()
    gk_legacy = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_keys = [k for k in [gk1, gk2, gk_legacy] if k]
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not gemini_keys and not openai_key:
        issues.append("No LLM API key set (GEMINI_API_KEY_1, GEMINI_API_KEY_2, or OPENAI_API_KEY)")

    # Check delivery recipient
    if not DELIVERY_TO:
        issues.append("DELIVERY_TO not set in .env")

    # Check feed directory exists
    if not FEEDS_DIR.exists():
        issues.append(f"Feed directory missing: {FEEDS_DIR}")

    # Report
    if issues:
        log.warning("Pre-flight check found issues:")
        for issue in issues:
            log.warning(f"  ! {issue}")
        return False
    else:
        log.info("Pre-flight check: all systems go.")
        log.info(f"  Pipeline:  news_fetcher.py → content_generator.py → SMTP")
        log.info(f"  Template:  {TEMPLATE_PATH.name}")
        log.info(f"  Feeds:     {FEEDS_DIR}")
        log.info(f"  Editions:  {EDITIONS_DIR}")
        log.info(f"  Delivery:  {DELIVERY_TO}")
        log.info(f"  SMTP:      {SMTP_HOST}:{SMTP_PORT} (TLS)")
        # LLM key summary
        if gemini_keys:
            key_count = len(set(gemini_keys))
            log.info(f"  LLM:       Gemini ({key_count} key{'s' if key_count > 1 else ''} configured)")
        elif openai_key:
            log.info(f"  LLM:       OpenAI")
        return True


# ==========================================================================
#                             MAIN
# ==========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="The Daily Scoop — Cron Delivery Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cron_delivery.py                 Run the full pipeline once (now)
  python cron_delivery.py --schedule      Start daemon (runs daily at 7:30 AM IST)
  python cron_delivery.py --install       Register with Windows Task Scheduler
  python cron_delivery.py --uninstall     Remove from Windows Task Scheduler
  python cron_delivery.py --test-email    Send a test email to verify SMTP
  python cron_delivery.py --check         Pre-flight check only
        """,
    )

    parser.add_argument(
        "--schedule", action="store_true",
        help="Start as a persistent daemon (triggers daily at 7:30 AM IST)",
    )
    parser.add_argument(
        "--install", action="store_true",
        help="Register as a Windows Task Scheduler task (runs without daemon)",
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="Remove the Windows Task Scheduler task",
    )
    parser.add_argument(
        "--test-email", action="store_true",
        help="Send a test email to verify SMTP configuration",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Run pre-flight check only (verify all components)",
    )
    parser.add_argument(
        "--skip-email", action="store_true",
        help="Run pipeline but skip email delivery",
    )

    args = parser.parse_args()

    # -- Banner ------------------------------------------------------------
    now = datetime.now(IST)
    print()
    print("  +----------------------------------------------+")
    print("  |      THE DAILY SCOOP — CRON DELIVERY         |")
    print(f"  |      {now.strftime('%Y-%m-%d %H:%M:%S')} IST                  |")
    print("  +----------------------------------------------+")
    print()

    # -- Route to the right mode -------------------------------------------

    if args.check:
        preflight_check()
        return

    if args.test_email:
        preflight_check()
        ok = send_test_email()
        if ok:
            print("\n  ✓ Test email sent successfully!\n")
        else:
            print("\n  ✗ Test email failed. Check logs above.\n")
        return

    if args.install:
        install_windows_task()
        return

    if args.uninstall:
        uninstall_windows_task()
        return

    if args.schedule:
        preflight_check()
        run_scheduler()
        return

    # -- Default: run full pipeline once -----------------------------------
    if not preflight_check():
        log.warning("Proceeding despite pre-flight issues...\n")

    if args.skip_email:
        # Run fetch + generate only
        log.info("STEP 1/2: Fetching news data...")
        fetch_ok = run_step(NEWS_FETCHER, "Fetch")
        if fetch_ok:
            log.info("STEP 2/2: Generating content via LLM...")
            run_step(CONTENT_GENERATOR, "Generate")
        else:
            log.error("Fetch failed. Aborting.")
    else:
        run_full_pipeline()


# ==========================================================================

if __name__ == "__main__":
    main()
