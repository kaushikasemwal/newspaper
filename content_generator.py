#!/usr/bin/env python3
"""
+==================================================================+
|         THE DAILY SCOOP -- CONTENT GENERATOR                     |
|                                                                  |
|  Reads raw JSON from news_fetcher.py, passes each section        |
|  through a Gemini LLM using rules.md as the system prompt,       |
|  and injects the rewritten "gossip tea" content into             |
|  newspaper_template.html to produce the final daily edition.     |
|                                                                  |
|  Usage:                                                          |
|    python content_generator.py                                   |
|    python content_generator.py --feed data/datasets/custom.json  |
|    python content_generator.py --dry-run                         |
|                                                                  |
+==================================================================+
"""

import os
import re
import sys
import json
import logging
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# -- Fix Windows console encoding -----------------------------------------
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

try:
    import google.generativeai as genai
except ImportError:
    genai = None

try:
    import anthropic
except ImportError:
    anthropic = None

# -- Load environment ------------------------------------------------------
load_dotenv(Path(__file__).parent / ".env")

# -- Logging ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("content_gen")

# -- Timezone --------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST)
TODAY = NOW.strftime("%Y-%m-%d")
TODAY_PRETTY = NOW.strftime("%A, %B %d, %Y")

# ==========================================================================
#                          CONFIGURATION
# ==========================================================================

SCRIPT_DIR = Path(__file__).parent

# -- Paths -----------------------------------------------------------------
RULES_PATH = SCRIPT_DIR / "rules.md"
TEMPLATE_PATH = SCRIPT_DIR / "newspaper_template.html"
FEED_DIR = Path(os.getenv("OUTPUT_DIR", str(SCRIPT_DIR / "data" / "datasets")))
OUTPUT_DIR = SCRIPT_DIR / "data" / "editions"

# -- LLM Config -----------------------------------------------------------
# Load dual Gemini keys (fall back to legacy single-key var for compat)
_gk1 = os.getenv("GEMINI_API_KEY_1", "").strip()
_gk2 = os.getenv("GEMINI_API_KEY_2", "").strip()
_gk_legacy = os.getenv("GEMINI_API_KEY", "").strip()

GEMINI_API_KEYS = [k for k in [_gk1, _gk2, _gk_legacy] if k]
# Deduplicate while preserving order
GEMINI_API_KEYS = list(dict.fromkeys(GEMINI_API_KEYS))

CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "").strip()

# Gemini model (free tier: gemini-2.0-flash)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# -- Generation params -----------------------------------------------------
TEMPERATURE = 0.85  # Creative but controlled
MAX_OUTPUT_TOKENS = 4096

# -- Blacklisted words (from rules.md -- enforced as a safety net) ---------
BLACKLIST = [
    "gmat", "g.m.a.t.",
    "preparation", "prep", "preparing", "preparatory",
    "exam", "examination", "exams",
    "practice", "practise", "practicing",
    "syllabus", "syllabi",
    "aptitude",
    "mba admissions",
]


# ==========================================================================
#                        RULES & TEMPLATE LOADER
# ==========================================================================

def load_rules() -> str:
    """Load rules.md as the LLM system prompt."""
    if not RULES_PATH.exists():
        log.error(f"rules.md not found at {RULES_PATH}")
        sys.exit(1)
    content = RULES_PATH.read_text(encoding="utf-8")
    log.info(f"Loaded rules.md ({len(content)} chars)")
    return content


def load_template() -> str:
    """Load the HTML email template."""
    if not TEMPLATE_PATH.exists():
        log.error(f"Template not found at {TEMPLATE_PATH}")
        sys.exit(1)
    content = TEMPLATE_PATH.read_text(encoding="utf-8")
    log.info(f"Loaded template ({len(content)} chars)")
    return content


def load_feed(feed_path: Optional[str] = None) -> dict:
    """Load the latest newspaper feed JSON."""
    if feed_path:
        p = Path(feed_path)
    else:
        # Find the most recent feed file
        feeds = sorted(FEED_DIR.glob("newspaper_feed_*.json"), reverse=True)
        if not feeds:
            log.error(f"No feed files found in {FEED_DIR}")
            log.error("Run news_fetcher.py first to generate a feed.")
            sys.exit(1)
        p = feeds[0]

    data = json.loads(p.read_text(encoding="utf-8"))
    date = data.get("metadata", {}).get("edition_date", "unknown")
    log.info(f"Loaded feed: {p.name} (edition: {date})")
    return data


# ==========================================================================
#                          LLM CLIENT
# ==========================================================================

class LLMClient:
    """
    Unified LLM client with multi-key failover.

    Key rotation strategy:
      1. Try Gemini Key 1 → if 429 quota error → switch to Key 2
      2. Try Gemini Key 2 → if 429 quota error → fall back to Claude
      3. Within each key, retry up to 2 times with backoff for transient errors

    Supports:
      - Google Gemini (primary -- dual-key rotation)
      - Anthropic Claude (final fallback)
    """

    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
        self.provider = None

        # Gemini multi-key state
        self._gemini_keys = list(GEMINI_API_KEYS)  # copy
        self._current_key_index = 0
        self._exhausted_keys = set()  # keys that hit daily quota
        self.model = None

        # Claude state
        self.claude_client = None

        self._init_provider()

    def _init_gemini(self, api_key: str) -> bool:
        """Initialize (or re-initialize) Gemini with a specific API key."""
        if not genai or not api_key:
            return False
        try:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel(
                model_name=GEMINI_MODEL,
                system_instruction=self.system_prompt,
                generation_config=genai.GenerationConfig(
                    temperature=TEMPERATURE,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                ),
            )
            self.provider = "gemini"
            key_label = self._key_label(api_key)
            log.info(f"LLM provider: Gemini ({GEMINI_MODEL}) — {key_label}")
            return True
        except Exception as e:
            log.warning(f"Gemini init failed for {self._key_label(api_key)}: {e}")
            return False

    def _key_label(self, key: str) -> str:
        """Readable label for logging (e.g., 'Key 1 (...sDE)')."""
        if not key:
            return "Key ?"
        idx = self._gemini_keys.index(key) + 1 if key in self._gemini_keys else "?"
        return f"Key {idx} (...{key[-4:]})"

    def _init_provider(self):
        """Initialize the best available LLM provider."""
        # Try Gemini keys in order
        for i, key in enumerate(self._gemini_keys):
            if self._init_gemini(key):
                self._current_key_index = i
                return

        # Try Claude as fallback
        if CLAUDE_API_KEY and anthropic:
            try:
                self.claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
                self.provider = "claude"
                log.info(f"LLM provider: Claude ({CLAUDE_MODEL})")
                return
            except Exception as e:
                log.warning(f"Claude init failed: {e}")

        log.error(
            "No LLM provider available!\n"
            "  Add GEMINI_API_KEY_1 / GEMINI_API_KEY_2 or CLAUDE_API_KEY to .env\n"
            "  Gemini: https://aistudio.google.com/apikey (free)\n"
            "  Claude: https://console.anthropic.com/settings/keys"
        )
        sys.exit(1)

    def _switch_to_next_gemini_key(self) -> bool:
        """
        Rotate to the next available Gemini key.
        Returns True if a new key was activated, False if all keys are exhausted.
        """
        # Mark current key as exhausted
        if self._current_key_index < len(self._gemini_keys):
            exhausted_key = self._gemini_keys[self._current_key_index]
            self._exhausted_keys.add(exhausted_key)
            log.warning(f"  Marking {self._key_label(exhausted_key)} as exhausted (quota hit)")

        # Try each remaining key
        for i, key in enumerate(self._gemini_keys):
            if key not in self._exhausted_keys:
                log.info(f"  Switching to Gemini {self._key_label(key)}...")
                if self._init_gemini(key):
                    self._current_key_index = i
                    return True

        # All Gemini keys exhausted — try Claude
        if CLAUDE_API_KEY and anthropic and self.provider != "claude":
            log.warning("  All Gemini keys exhausted. Falling back to Claude...")
            try:
                self.claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
                self.provider = "claude"
                log.info(f"  Switched to Claude ({CLAUDE_MODEL})")
                return True
            except Exception as e:
                log.error(f"  Claude fallback failed: {e}")

        return False

    def _is_quota_error(self, error_str: str) -> bool:
        """Check if an error is a quota/rate-limit error."""
        lower = error_str.lower()
        return "429" in error_str or "quota" in lower or "rate limit" in lower

    def _is_daily_quota_error(self, error_str: str) -> bool:
        """Check if the error is specifically a DAILY quota exhaust (not per-minute)."""
        lower = error_str.lower()
        return "limit: 0" in lower or "perdayperproject" in lower.replace("_", "")

    def generate(self, prompt: str, section_name: str = "") -> str:
        """
        Generate content with automatic key failover and retry.

        Strategy:
          1. Try current key (up to 2 retries for transient rate limits)
          2. If daily quota exhausted → switch to next key and retry immediately
          3. If all Gemini keys exhausted → fall back to OpenAI
        """
        import time as _time

        label = f" [{section_name}]" if section_name else ""
        log.info(f"  Generating{label}...")

        max_retries_per_key = 2
        base_delay = 10  # seconds

        # Outer loop: try each available key
        keys_tried = 0
        max_key_switches = len(self._gemini_keys) + 1  # +1 for OpenAI fallback

        while keys_tried < max_key_switches:
            # Inner loop: retry within the current key
            for attempt in range(1, max_retries_per_key + 1):
                try:
                    if self.provider == "gemini":
                        response = self.model.generate_content(prompt)
                        text = response.text
                    elif self.provider == "claude":
                        response = self.claude_client.messages.create(
                            model=CLAUDE_MODEL,
                            system=self.system_prompt,
                            messages=[
                                {"role": "user", "content": prompt},
                            ],
                            temperature=TEMPERATURE,
                            max_tokens=MAX_OUTPUT_TOKENS,
                        )
                        text = response.content[0].text
                    else:
                        return "[Generation failed -- no provider]"

                    # Safety: strip any blacklisted words that slipped through
                    text = self._enforce_blacklist(text)
                    log.info(f"  OK{label} ({len(text)} chars)")

                    # Small inter-request delay to avoid per-minute quota spikes
                    _time.sleep(2)

                    return text.strip()

                except Exception as e:
                    error_str = str(e)

                    if self._is_quota_error(error_str):
                        # Daily quota exhausted → switch key immediately
                        if self._is_daily_quota_error(error_str):
                            current_label = "current key"
                            if self.provider == "gemini" and self._current_key_index < len(self._gemini_keys):
                                current_label = self._key_label(self._gemini_keys[self._current_key_index])
                            log.warning(f"  DAILY QUOTA HIT{label} on {current_label}. Switching...")
                            break  # break inner loop → switch key in outer loop

                        # Per-minute rate limit → wait and retry same key
                        retry_delay = base_delay * attempt
                        delay_match = re.search(r"retry in (\d+\.?\d*)", error_str, re.IGNORECASE)
                        if delay_match:
                            retry_delay = max(float(delay_match.group(1)) + 2, retry_delay)

                        if attempt < max_retries_per_key:
                            log.warning(
                                f"  RATE LIMITED{label} (attempt {attempt}/{max_retries_per_key}). "
                                f"Waiting {retry_delay:.0f}s..."
                            )
                            _time.sleep(retry_delay)
                            continue
                        else:
                            log.warning(f"  Rate limit persists{label}. Switching key...")
                            break  # break inner loop → switch key
                    else:
                        # Non-quota error — don't retry
                        log.error(f"  FAIL{label}: {e}")
                        return f"[Content generation failed: {e}]"

            # Try switching to the next key
            keys_tried += 1
            if not self._switch_to_next_gemini_key():
                log.error(f"  FAIL{label}: All API keys exhausted. No fallback available.")
                return "[Content generation failed: all API keys exhausted]"

        return "[Content generation failed: all keys and retries exhausted]"

    def _enforce_blacklist(self, text: str) -> str:
        """Final safety net: remove any blacklisted words."""
        for word in BLACKLIST:
            pattern = re.compile(re.escape(word), re.IGNORECASE)
            if pattern.search(text):
                log.warning(f"  BLACKLIST CAUGHT: '{word}' removed from output")
                text = pattern.sub("***", text)
        return text


# ==========================================================================
#                     SECTION PROMPT BUILDERS
# ==========================================================================

def build_domestic_prompt(articles: list[dict]) -> str:
    """Build the prompt for the Domestic Dispatch section."""
    stories = []
    for i, a in enumerate(articles, 1):
        stories.append(
            f"STORY {i}:\n"
            f"  Headline: {a.get('headline', 'N/A')}\n"
            f"  Source: {a.get('source', 'N/A')}\n"
            f"  Summary: {a.get('summary', 'No summary available')}\n"
            f"  URL: {a.get('url', '')}"
        )

    return f"""You are writing the "Domestic Dispatch" section of The Daily Scoop newsletter.

Below are {len(articles)} raw Indian domestic news stories. Your job:

1. SELECT the 3-4 most interesting, consequential, or gossip-worthy stories.
2. REWRITE each into the "witty best friend spilling tea" voice from rules.md.
3. For EACH story, naturally weave in at least ONE analytical insight -- expose an assumption, a logical gap, a cause-and-effect chain, or a "what nobody's asking" angle. This must feel organic, NOT academic.
4. Format each story as an HTML paragraph: use <p> tags, <strong> for the lead sentence, and <span style="color: #D4918F;"> for any emphasized asides.
5. Keep each story to 2-3 short paragraphs max. Fast-paced. Punchy.
6. NEVER use any words from the blacklist in rules.md.

OUTPUT: Only the HTML content (multiple <p> blocks). No markdown. No section headers. No wrapper tags.

RAW STORIES:
{chr(10).join(stories)}"""


def build_international_prompt(articles: list[dict]) -> str:
    """Build the prompt for the Global Gossip section."""
    stories = []
    for i, a in enumerate(articles, 1):
        stories.append(
            f"STORY {i}:\n"
            f"  Headline: {a.get('headline', 'N/A')}\n"
            f"  Source: {a.get('source', 'N/A')}\n"
            f"  Summary: {a.get('summary', 'No summary available')}\n"
            f"  URL: {a.get('url', '')}"
        )

    return f"""You are writing the "Global Gossip" section of The Daily Scoop newsletter.

Below are {len(articles)} raw international news stories. Your job:

1. SELECT the 3-4 juiciest, most dramatic, or globally significant stories.
2. REWRITE each into the "witty best friend spilling tea" voice from rules.md.
3. For EACH story, naturally weave in at least ONE analytical insight -- question an assumption, highlight a contradiction, trace a cause-and-effect, or present "two sides" with a clear lean. Keep it conversational.
4. Format each story as HTML paragraphs: use <p> tags, <strong> for the lead, <em> for dramatic emphasis.
5. Keep each story to 2-3 short paragraphs. Punchy sentences. No walls of text.
6. NEVER use any words from the blacklist in rules.md.

OUTPUT: Only the HTML content (multiple <p> blocks). No markdown. No section headers.

RAW STORIES:
{chr(10).join(stories)}"""


def build_finance_prompt(finance_data: dict) -> tuple[str, str]:
    """Build prompts for The Bag Check section. Returns (sentiment_prompt, body_prompt)."""
    funds = finance_data.get("funds", [])
    source = finance_data.get("source", "unknown")
    sentiment = finance_data.get("overall_sentiment", "neutral")

    fund_lines = []
    for f in funds:
        change = f.get("daily_change_pct", 0)
        arrow = "up" if change >= 0 else "down"
        fund_lines.append(
            f"  - {f.get('name', 'Unknown Fund')}: "
            f"NAV {f.get('nav', 'N/A')}, "
            f"Change: {change:+.2f}% ({arrow})"
        )

    fund_block = chr(10).join(fund_lines) if fund_lines else "  No fund data available today."

    sentiment_prompt = f"""Write a ONE-LINE "Quick Vibe" summary for our investment portfolio check-in.
Overall market sentiment: {sentiment}.
Fund performance data:
{fund_block}

Rules: Sound like a friend giving a casual read on our money. Examples:
- "Green across the board -- we love to see it."
- "A mixed bag today, but nothing scary."
- "Small dip, but our money is just being moody. We're fine."

OUTPUT: Just the one line. No quotes. No HTML tags."""

    body_prompt = f"""You are writing "The Bag Check" finance section of The Daily Scoop newsletter.

Fund data (source: {source}):
{fund_block}

Your job:
1. Summarize EACH fund's performance in 1-2 casual sentences using the gossipy voice.
2. Treat this like a friend checking in on our shared investments over brunch.
3. Use relatable analogies. Celebrate gains casually. Acknowledge dips without panic.
4. End with a "Takeaway" line -- 1-2 sentences, encouraging but grounded. No financial advice.
5. Format as HTML: each fund in a <p> tag. Use <strong> for fund names. Use <span style="color: #4A7C4A; font-weight: 600;"> for positive % and <span style="color: #C45A5A; font-weight: 600;"> for negative %.
6. After the fund paragraphs, add the takeaway in: <p style="margin: 0; font-family: 'Georgia', serif; font-style: italic; color: #5A8A5A; font-size: 14px;"><strong style="font-style: normal;">The Takeaway:</strong> ...</p>
7. NEVER use dense financial jargon. No "NAV decreased by X% due to sectoral headwinds." Keep it breezy.

OUTPUT: Only the HTML content. No markdown. No section headers."""

    return sentiment_prompt, body_prompt


def build_morning_brew_prompt(domestic: list[dict], international: list[dict]) -> str:
    """Build prompt for the intro teaser."""
    top_domestic = [a.get("headline", "") for a in domestic[:3]]
    top_intl = [a.get("headline", "") for a in international[:3]]

    return f"""Write the opening "Morning Brew" intro for The Daily Scoop newsletter -- today's date is {TODAY_PRETTY}.

Top domestic headlines:
{chr(10).join(f'  - {h}' for h in top_domestic)}

Top international headlines:
{chr(10).join(f'  - {h}' for h in top_intl)}

Rules:
- 2-3 sentences MAX. This is a teaser that makes them want to keep reading.
- Address the reader as "bestie" or casually.
- Hint at the juiciest stories without spoiling them.
- End with something like "Grab your coffee -- we have tea to spill."
- Use the witty, gossipy tone from rules.md.

OUTPUT: Plain text only. No HTML tags. No markdown."""


def build_hot_take_prompt(domestic: list[dict], international: list[dict]) -> str:
    """Build prompt for the editorial Hot Take closer."""
    all_headlines = [a.get("headline", "") for a in (domestic + international)[:8]]

    return f"""Write "The Hot Take" -- a spicy, opinionated editorial closer for The Daily Scoop newsletter.

Today's headlines covered:
{chr(10).join(f'  - {h}' for h in all_headlines)}

Rules:
- 3-4 sentences. One cohesive opinion that ties together the day's themes.
- Be bold. Take a stance. Use the gossipy, witty voice.
- Naturally weave in ONE sharp analytical observation -- an assumption everyone's making, a contradiction in the narratives, or a "nobody's connecting these dots" moment.
- End on a punchy, memorable line.
- NEVER use blacklisted words from rules.md.

OUTPUT: Plain text only. No HTML. No markdown. No "Hot Take:" prefix."""


def build_word_prompt(word_data: dict) -> str:
    """Build prompt for Word of the Day usage example."""
    word = word_data.get("word", "serendipity")
    definition = word_data.get("definition", "")
    pos = word_data.get("part_of_speech", "")

    return f"""Write a single clever, witty example sentence using the word "{word}" ({pos}: {definition}).

Rules:
- The sentence should feel like something a witty friend would say in conversation.
- It should naturally demonstrate the word's meaning in context.
- Make it memorable and a little funny if possible.
- 1 sentence only. No quotation marks around it.

OUTPUT: Just the sentence. Nothing else."""


# ==========================================================================
#                      TEMPLATE INJECTION
# ==========================================================================

def inject_into_template(template: str, content: dict) -> str:
    """
    Replace Jinja2-style {{ placeholders }} in the template with generated content.

    Uses simple string replacement rather than Jinja2 engine to avoid
    conflicts with HTML/CSS curly braces and SVG content in the template.
    """
    replacements = {
        "{{ edition_date }}": content.get("edition_date", TODAY_PRETTY),
        "{{ edition_number }}": content.get("edition_number", ""),
        "{{ morning_brew }}": content.get("morning_brew", ""),
        "{{ domestic_news }}": content.get("domestic_news", ""),
        "{{ international_news }}": content.get("international_news", ""),
        "{{ groww_sentiment }}": content.get("groww_sentiment", ""),
        "{{ groww_updates }}": content.get("groww_updates", ""),
        "{{ word_of_the_day }}": content.get("word_of_the_day", ""),
        "{{ word_phonetic }}": content.get("word_phonetic", ""),
        "{{ word_part_of_speech }}": content.get("word_part_of_speech", ""),
        "{{ word_definition }}": content.get("word_definition", ""),
        "{{ word_example }}": content.get("word_example", ""),
        "{{ hot_take }}": content.get("hot_take", ""),
        "{{ footer_quote }}": content.get("footer_quote", ""),
        "{{ signoff }}": content.get("signoff", ""),
    }

    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, str(value))

    return result


def compute_edition_number() -> str:
    """Compute edition number based on days since project start."""
    start = datetime(2026, 6, 20, tzinfo=IST)  # Project inception date
    delta = NOW - start
    return f"#{max(1, delta.days + 1)}"


# ==========================================================================
#                          MAIN PIPELINE
# ==========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="The Daily Scoop -- Content Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--feed", type=str, default=None,
        help="Path to a specific feed JSON file (default: latest in data/datasets/)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate content but don't save the final HTML",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Custom output path for the final HTML",
    )
    args = parser.parse_args()

    # -- Banner ------------------------------------------------------------
    print()
    print("  +----------------------------------------------+")
    print("  |      THE DAILY SCOOP -- CONTENT GENERATOR    |")
    print(f"  |      Date: {TODAY}                       |")
    print(f"  |      Time: {NOW.strftime('%H:%M:%S')} IST                     |")
    print("  +----------------------------------------------+")
    print()

    # -- Step 1: Load inputs -----------------------------------------------
    log.info("Step 1/5: Loading inputs...")
    rules = load_rules()
    template = load_template()
    feed = load_feed(args.feed)

    domestic_articles = feed.get("domestic_tea", [])
    international_articles = feed.get("global_gossip", [])
    word_data = feed.get("word_of_the_day", {})
    finance_data = feed.get("the_bag_check", {})
    edition_date_raw = feed.get("metadata", {}).get("edition_date", TODAY)

    # Format the edition date nicely
    try:
        dt = datetime.strptime(edition_date_raw, "%Y-%m-%d")
        edition_date_pretty = dt.strftime("%A, %B %d, %Y")
    except Exception:
        edition_date_pretty = edition_date_raw

    log.info(
        f"  Feed: {len(domestic_articles)} domestic, "
        f"{len(international_articles)} international, "
        f"{len(finance_data.get('funds', []))} funds"
    )

    # -- Step 2: Initialize LLM --------------------------------------------
    log.info("Step 2/5: Initializing LLM...")
    llm = LLMClient(system_prompt=rules)

    # -- Step 3: Generate content for each section -------------------------
    log.info("Step 3/5: Generating content sections...")

    # 3a. Morning Brew (intro teaser)
    morning_brew = llm.generate(
        build_morning_brew_prompt(domestic_articles, international_articles),
        section_name="Morning Brew",
    )

    # 3b. Domestic Dispatch
    domestic_news = llm.generate(
        build_domestic_prompt(domestic_articles),
        section_name="Domestic Dispatch",
    )

    # 3c. Global Gossip
    international_news = llm.generate(
        build_international_prompt(international_articles),
        section_name="Global Gossip",
    )

    # 3d. The Bag Check (finance)
    sentiment_prompt, finance_body_prompt = build_finance_prompt(finance_data)
    groww_sentiment = llm.generate(sentiment_prompt, section_name="Bag Check Vibe")
    groww_updates = llm.generate(finance_body_prompt, section_name="Bag Check Body")

    # 3e. Word of the Day (example sentence)
    word_example = llm.generate(
        build_word_prompt(word_data),
        section_name="Word Example",
    )

    # 3f. Hot Take (editorial closer)
    hot_take = llm.generate(
        build_hot_take_prompt(domestic_articles, international_articles),
        section_name="Hot Take",
    )

    # 3g. Footer quote
    footer_quote = llm.generate(
        "Generate a single witty, memorable quote about staying informed, "
        "curiosity, or the absurdity of modern news. It should sound like "
        "something a clever friend would say. 1 sentence only. No attribution. "
        "No quotation marks.",
        section_name="Footer Quote",
    )

    # -- Step 4: Assemble all content --------------------------------------
    log.info("Step 4/5: Assembling content...")

    content = {
        "edition_date": edition_date_pretty,
        "edition_number": compute_edition_number(),
        "morning_brew": morning_brew,
        "domestic_news": domestic_news,
        "international_news": international_news,
        "groww_sentiment": groww_sentiment,
        "groww_updates": groww_updates,
        "word_of_the_day": word_data.get("word", "serendipity"),
        "word_phonetic": word_data.get("phonetic", ""),
        "word_part_of_speech": word_data.get("part_of_speech", ""),
        "word_definition": word_data.get("definition", ""),
        "word_example": word_example,
        "hot_take": hot_take,
        "footer_quote": footer_quote,
        "signoff": f"See you tomorrow, bestie. Same time, same place, more tea.",
    }

    # Inject into template
    final_html = inject_into_template(template, content)

    # -- Step 5: Save output -----------------------------------------------
    log.info("Step 5/5: Saving final edition...")

    if args.dry_run:
        print("\n" + "-" * 50)
        print("  DRY RUN -- Content preview (not saved)")
        print("-" * 50)
        for key, val in content.items():
            preview = str(val)[:120].replace("\n", " ")
            print(f"  {key:>22}: {preview}...")
        print("-" * 50 + "\n")
    else:
        if args.output:
            output_path = Path(args.output)
        else:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            output_path = OUTPUT_DIR / f"the_daily_scoop_{edition_date_raw}.html"

        output_path.write_text(final_html, encoding="utf-8")
        size_kb = output_path.stat().st_size / 1024
        log.info(f"  Saved: {output_path} ({size_kb:.1f} KB)")

    # -- Summary -----------------------------------------------------------
    print()
    print("  +---------------------------------------------+")
    print("  |            GENERATION SUMMARY               |")
    print("  +---------------------------------------------+")
    print(f"  |  Morning Brew:      {len(morning_brew):>5} chars              |")
    print(f"  |  Domestic Dispatch:  {len(domestic_news):>5} chars              |")
    print(f"  |  Global Gossip:     {len(international_news):>5} chars              |")
    print(f"  |  Bag Check:         {len(groww_updates):>5} chars              |")
    print(f"  |  Word Example:      {len(word_example):>5} chars              |")
    print(f"  |  Hot Take:          {len(hot_take):>5} chars              |")
    print(f"  |  LLM Provider:      {llm.provider:>10}              |")
    print("  +---------------------------------------------+")
    if not args.dry_run:
        print(f"  |  Output: {str(output_path):<35}|")
        print("  +---------------------------------------------+")
    print()


# ==========================================================================

if __name__ == "__main__":
    main()
