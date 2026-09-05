# The Daily Scoop — Automated Daily Newsletter

A fully automated pipeline that fetches Indian and international news, rewrites it in a witty "gossipy best friend" voice using LLMs, injects it into a beautiful HTML template, and delivers it via email every morning.

## Features

- **News Fetching** (`news_fetcher.py`): Aggregates from RSS feeds (The Hindu, Indian Express, Economic Times, BBC, Reuters, etc.) + Groww Digest emails via IMAP
- **Content Generation** (`content_generator.py`): Rewrites raw news into conversational "tea-spilling" format using LLMs with multi-provider failover
- **Email Delivery** (`cron_delivery.py`): Sends the final HTML edition via SMTP (Gmail) with retry logic
- **Multi-LLM Failover**: Gemini (up to 5 keys with quota rotation) → OpenAI → NVIDIA NIM
- **Scheduled Execution**: Runs daily at 7:30 AM IST via cron/Task Scheduler

## Architecture

```
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  news_fetcher   │────▶│  content_generator   │────▶│ cron_delivery   │
│                 │     │                      │     │                 │
│ • RSS feeds     │     │ • rules.md (system   │     │ • SMTP send     │
│ • IMAP (Groww)  │     │   prompt)            │     │ • Failure alert │
│ • JSON output   │     │ • newspaper_template │     │ • Pre-flight    │
│                 │     │   .html              │     │   checks        │
└─────────────────┘     │ • Multi-LLM client   │     └─────────────────┘
                        │   (Gemini/OpenAI/    │
                        │    NVIDIA)           │
                        └──────────────────────┘
```

## Project Structure

```
newspaper/
├── content_generator.py      # LLM content generation with failover
├── cron_delivery.py          # Main pipeline orchestrator + email delivery
├── news_fetcher.py           # News aggregation from RSS + IMAP
├── newspaper_template.html   # HTML email template (28KB)
├── rules.md                  # LLM system prompt (voice & style guide)
├── requirements.txt          # Python dependencies
├── .env                      # API keys & config (NOT in git)
├── .gitignore
├── data/
│   ├── datasets/             # Raw JSON feeds (gitignored)
│   ├── editions/             # Generated HTML editions (gitignored)
│   ├── logs/                 # Pipeline logs (gitignored)
│   ├── header_banner.png     # Email header image
│   └── footer_scenery.png    # Email footer image
└── README.md
```

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/kaushikasemwal/newspaper.git
cd newspaper
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

### 2. Configure `.env`

Copy the template and fill in your credentials:

```bash
# Gmail IMAP (for Groww Digest parsing)
GMAIL_ADDRESS=your_email@gmail.com
GMAIL_APP_PASSWORD=your_16_char_app_password
GROWW_SENDER=digest@groww.in

# Output directory
OUTPUT_DIR=d:/newspaper/data/datasets

# LLM API Keys (at least one required)
GEMINI_API_KEY_1=your_key_from_aistudio_google_com
GEMINI_API_KEY_2=optional_second_key
GEMINI_API_KEY_3=optional_third_key
GEMINI_API_KEY_4=optional_fourth_key
GEMINI_API_KEY_5=optional_fifth_key
GEMINI_MODEL=gemini-3.6-flash

# Fallbacks (optional but recommended)
OPENAI_API_KEY=sk-...         # OpenAI fallback
OPENAI_MODEL=gpt-4o-mini

NVIDIA_API_KEY=nvapi-...      # NVIDIA NIM fallback
NVIDIA_MODEL=nvidia/nemotron-3-ultra-550b-a55b
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1

# Email delivery
DELIVERY_TO=recipient@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587

# Schedule (24h, IST)
SCHEDULE_HOUR=7
SCHEDULE_MINUTE=30
```

**Get API Keys:**
- **Gemini** (free): https://aistudio.google.com/apikey — create up to 4 keys across different Google Cloud projects for quota rotation
- **OpenAI**: https://platform.openai.com/api-keys
- **NVIDIA NIM**: https://build.nvidia.com/ — get API key from your account

### 3. Run Manually

```bash
# Fetch news only
python news_fetcher.py

# Generate content from latest feed
python content_generator.py

# Full pipeline (fetch → generate → email)
python cron_delivery.py

# Dry run (generate but don't save/send)
python content_generator.py --dry-run
```

### 4. Schedule Automatically

**Windows (Task Scheduler):**
```powershell
# Create a basic task running daily at 7:30 AM
# Action: python.exe
# Arguments: D:\newspaper\cron_delivery.py
# Start in: D:\newspaper
```

**Linux/macOS (cron):**
```bash
crontab -e
# Add:
30 7 * * * cd /path/to/newspaper && .venv/bin/python cron_delivery.py >> data/logs/cron.log 2>&1
```

## LLM Failover Strategy

The system tries providers in order until one succeeds:

| Priority | Provider | Keys | Use Case |
|----------|----------|------|----------|
| 1 | Google Gemini | Up to 5 (rotated on quota/rate-limit) | Primary — free tier, fast |
| 2 | OpenAI | 1 | Fallback when all Gemini keys exhausted |
| 3 | NVIDIA NIM | 1 | Final fallback — OpenAI-compatible API |

**Key Rotation (Gemini):**
- Tries Key 1 → on daily quota (429) → Key 2 → Key 3 → Key 4 → Key 5
- On per-minute rate limit: retries same key 2× with exponential backoff
- If all 5 keys exhausted → switches to OpenAI
- If OpenAI fails → switches to NVIDIA

## Configuration Files

### `rules.md` — Voice & Style Guide
The system prompt that defines the "witty best friend spilling tea" persona. Includes:
- Tone guidelines (conversational, punchy, analytical)
- Blacklisted words (GMAT, prep, exam, syllabus, etc.)
- Section-specific formatting rules
- HTML output requirements

### `newspaper_template.html` — Email Template
Responsive HTML template with:
- Header banner image
- Morning Brew (intro teaser)
- Domestic Dispatch (Indian news)
- Global Gossip (international news)
- The Bag Check (Groww fund performance)
- Word of the Day
- The Hot Take (editorial closer)
- Footer quote + scenery image

## Output

- **JSON feeds**: `data/datasets/newspaper_feed_YYYY-MM-DD.json`
- **HTML editions**: `data/editions/the_daily_scoop_YYYY-MM-DD.html`
- **Logs**: `data/logs/cron_delivery.log`

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "No LLM provider available" | Add at least one `GEMINI_API_KEY_1` to `.env` |
| Gemini 429 errors | Add more keys (`GEMINI_API_KEY_2`–`_5`) from different GCP projects |
| OpenAI 429/quota | Add credits at platform.openai.com or rely on NVIDIA fallback |
| NVIDIA 404/503 | Check model name in `.env` (see available models at build.nvidia.com) |
| Email not sending | Verify Gmail App Password & IMAP enabled; check `SMTP_HOST`/`PORT` |
| Groww Digest not parsed | Ensure `GMAIL_APP_PASSWORD` is correct and IMAP is enabled in Gmail |

## License

MIT License — feel free to fork and customize for your own newsletter.