#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║              🗞️  DAILY NEWSPAPER — DATA FETCHER                ║
║                                                                  ║
║  Pulls live data from news feeds, vocabulary APIs, and finance   ║
║  sources into four clean JSON buckets:                           ║
║                                                                  ║
║    1. Domestic Tea      — Indian news (RSS)                      ║
║    2. Global Gossip     — International news (RSS + scrape)      ║
║    3. Word of the Day   — Vocabulary (API)                       ║
║    4. The Bag Check     — Finance (IMAP + MFAPI fallback)        ║
║                                                                  ║
║  Usage:                                                          ║
║    python news_fetcher.py                                        ║
║    python news_fetcher.py --bucket domestic_tea                  ║
║    python news_fetcher.py --dry-run                              ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import json
import hashlib
import logging
import imaplib
import email as email_lib
import argparse
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path
from typing import Optional

# ── Fix Windows console encoding ─────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass  # Older Python or non-standard terminal

# ── Third-party imports ──────────────────────────────────────────
try:
    import feedparser
    import requests
    from bs4 import BeautifulSoup
    from dotenv import load_dotenv
except ImportError as e:
    print(f"\n❌ Missing dependency: {e}")
    print("   Run: pip install -r requirements.txt\n")
    sys.exit(1)

# ── Load environment ─────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

# ── Logging setup ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("newspaper")

# ── Timezone ─────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST)
TODAY = NOW.strftime("%Y-%m-%d")

# ══════════════════════════════════════════════════════════════════
#                        CONFIGURATION
# ══════════════════════════════════════════════════════════════════

# ── Output ───────────────────────────────────────────────────────
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(Path(__file__).parent / "data" / "datasets")))
OUTPUT_FILE = OUTPUT_DIR / f"newspaper_feed_{TODAY}.json"

# ── User-Agent for requests ──────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36 "
        "DailyNewspaper/1.0"
    )
}
REQUEST_TIMEOUT = 15  # seconds

# ── RSS Feed URLs ────────────────────────────────────────────────
DOMESTIC_FEEDS = {
    "The Hindu": [
        "https://www.thehindu.com/news/national/feeder/default.rss",
        "https://www.thehindu.com/feeder/default.rss",
    ],
    "Indian Express": [
        "https://indianexpress.com/section/india/feed/",
        "https://indianexpress.com/print/front-page/feed/",
    ],
    "Livemint": [
        "https://www.livemint.com/rss/news",
        "https://www.livemint.com/rss/homepage",
    ],
}

INTERNATIONAL_FEEDS = {
    "BBC News": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.bbci.co.uk/news/rss.xml",
    ],
    "Financial Times": [
        "https://www.ft.com/world?format=rss",
        "https://www.ft.com/rss/home",
    ],
    "The Guardian": [
        "https://www.theguardian.com/world/rss",
    ],
    "The Guardian Business": [
        "https://www.theguardian.com/business/rss",
        "https://www.theguardian.com/business/economics/rss",
    ],
}

# ── Articles per source ─────────────────────────────────────────
MAX_ARTICLES_DOMESTIC = 5
MAX_ARTICLES_INTERNATIONAL = 4

# ── Gmail IMAP ───────────────────────────────────────────────────
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GROWW_SENDER = os.getenv("GROWW_SENDER", "digest@groww.in")
IMAP_SERVER = "imap.gmail.com"

# ── MFAPI (free, no auth) ───────────────────────────────────────
MFAPI_BASE = "https://api.mfapi.in/mf"

# ── Fallback fund tickers ───────────────────────────────────────
# Add your own AMFI scheme codes here. Find codes at:
# https://www.amfiindia.com/spages/NAVAll.txt
# or search via: https://api.mfapi.in/mf/search?q=fund+name
FALLBACK_FUNDS = [
    {"scheme_code": "146381", "name": "DSP Nifty Next 50 Index Fund - Direct Plan - Growth"},
    {"scheme_code": "118778", "name": "Nippon India Small Cap Fund - Direct Plan Growth Plan - Growth Option"},
    {"scheme_code": "118989", "name": "HDFC Mid Cap Fund - Growth Option - Direct Plan"},
    {"scheme_code": "119091", "name": "HDFC Liquid Fund - Growth Option - Direct Plan"},
    {"scheme_code": "120389", "name": "Axis Liquid Fund - Direct Plan - Growth Option"},
    
]

# ── Vocabulary ───────────────────────────────────────────────────
FREE_DICT_API = "https://api.dictionaryapi.dev/api/v2/entries/en"
WORDNIK_API_KEY = os.getenv("WORDNIK_API_KEY", "")
WORDNIK_WOTD_URL = "https://api.wordnik.com/v4/words.json/wordOfTheDay"

# Curated word list — rotates daily by date hash
# These are sophisticated, interesting words perfect for a "Word of the Day" feature
CURATED_WORDS = [
    "perspicacious", "ephemeral", "sanguine", "ubiquitous", "pernicious",
    "obfuscate", "magnanimous", "egregious", "diaphanous", "recalcitrant",
    "mellifluous", "insouciant", "perfunctory", "equanimity", "vertiginous",
    "loquacious", "ineffable", "surreptitious", "pulchritudinous", "sesquipedalian",
    "serendipity", "petrichor", "luminous", "aplomb", "cacophony",
    "languid", "ebullient", "laconic", "mercurial", "nascent",
    "nefarious", "pellucid", "quixotic", "resilient", "sagacious",
    "truculent", "unctuous", "vicissitude", "winsome", "zealous",
    "aberrant", "bellicose", "churlish", "deleterious", "enervate",
    "fatuous", "garrulous", "hegemony", "iconoclast", "juxtapose",
    "kinetic", "lugubrious", "malfeasance", "nonplussed", "obsequious",
    "paradigm", "querulous", "rapacious", "soporific", "temerity",
    "umbrage", "venerable", "wistful", "xenial", "yearning",
    "adroit", "belligerent", "cogent", "diffident", "elucidate",
    "fastidious", "gregarious", "hapless", "impetuous", "judicious",
    "keen", "lissome", "munificent", "nuance", "ostensible",
    "pragmatic", "quintessential", "reticent", "sycophant", "tenacious",
    "unflappable", "voracious", "whimsical", "extemporaneous", "bucolic",
    "cantankerous", "desultory", "effervescent", "felicitous", "grandiloquent",
    "hubris", "indefatigable", "jejune", "kafkaesque", "labyrinthine",
    "meretricious", "nonchalant", "opulent", "prevaricate", "quandary",
    "redoubtable", "scintillating", "transient", "unpropitious", "vivacious",
    "wanderlust", "acquiesce", "beguile", "circumspect", "delectation",
    "exuberant", "flippant", "gratuitous", "histrionic", "intransigent",
    "jocund", "kismet", "lithe", "maverick", "nebulous",
    "obstreperous", "palatable", "quotidian", "ruminate", "stymie",
    "tautological", "urbane", "vexation", "acrimonious", "baroque",
    "capricious", "draconian", "ennui", "fortuitous", "gossamer",
    "halcyon", "implacable", "juggernaut", "kaleidoscopic", "lethargic",
    "maelstrom", "nihilistic", "oscillate", "paradoxical", "quagmire",
    "rapprochement", "sardonic", "taciturn", "utilitarian", "vacillate",
    "watershed", "ambivalent", "brazen", "circumvent", "debonair",
    "enigmatic", "frivolous", "galvanize", "heretical", "idiosyncratic",
    "jettison", "kudos", "laborious", "meticulous", "nondescript",
    "opaque", "plethora", "rhetoric", "stolid", "tantamount",
    "unequivocal", "vindicate", "amalgamate", "bravado", "conundrum",
    "diatribe", "esoteric", "flagrant", "ignominious", "lachrymose",
    "mendacious", "obstinate", "panache", "quiescent", "rancorous",
    "sublime", "treacherous", "usurp", "verisimilitude", "acumen",
    "benevolent", "clemency", "dubious", "exacerbate", "fervent",
    "guileless", "hermetic", "incandescent", "jubilant", "kinesthetic",
    "lustrous", "morose", "nomenclature", "ominous", "portentous",
    "rambunctious", "supercilious", "turpitude", "veracious", "zealot",
    "audacious", "blithe", "congenial", "disparate", "eclectic",
    "furtive", "grandiose", "incorrigible", "magniloquent", "penchant",
    "reciprocal", "sanguinary", "tempestuous", "voluminous", "auspicious",
    "beguiling", "copious", "discerning", "eloquent", "formidable",
    "garrulity", "inimitable", "laudable", "opulence", "prodigious",
    "resolute", "sumptuous", "trenchant", "virtuoso", "ameliorate",
    "brusque", "clandestine", "decorum", "eminent", "facetious",
    "germane", "impervious", "jurisprudence", "labyrinth", "mitigate",
    "obdurate", "precarious", "recondite", "salubrious", "travesty",
    "untenable", "verdant", "acerbic", "bonhomie", "conflagration",
    "demure", "efflorescence", "flamboyant", "imperturbable", "magnate",
    "paradox", "raconteur", "soliloquy", "tumultuous", "variegated",
    "wry", "alacrity", "capacious", "ebullience", "incisive",
    "parsimonious", "resplendent", "sonorous", "truculent", "vainglorious",
]


# ══════════════════════════════════════════════════════════════════
#                   BUCKET 1: DOMESTIC TEA 🇮🇳
# ══════════════════════════════════════════════════════════════════

class DomesticTeaFetcher:
    """Fetches Indian domestic news from RSS feeds."""

    def __init__(self):
        self.articles = []

    def fetch(self) -> list[dict]:
        """Fetch articles from all domestic RSS sources."""
        log.info("🇮🇳 Fetching Domestic Tea...")

        for source_name, feed_urls in DOMESTIC_FEEDS.items():
            fetched = self._fetch_source(source_name, feed_urls)
            self.articles.extend(fetched)
            log.info(f"   ✓ {source_name}: {len(fetched)} articles")

        # Deduplicate by similar headlines
        self.articles = self._deduplicate(self.articles)
        log.info(f"   📰 Total domestic articles: {len(self.articles)}")
        return self.articles

    def _fetch_source(self, source_name: str, feed_urls: list[str]) -> list[dict]:
        """Try each feed URL until one works."""
        for url in feed_urls:
            try:
                feed = feedparser.parse(url)
                if feed.bozo and not feed.entries:
                    continue

                articles = []
                for entry in feed.entries[:MAX_ARTICLES_DOMESTIC]:
                    articles.append(self._parse_entry(entry, source_name))
                return articles

            except Exception as e:
                log.warning(f"   ⚠ {source_name} feed failed ({url}): {e}")
                continue

        log.error(f"   ✗ {source_name}: all feeds failed")
        return []

    def _parse_entry(self, entry: dict, source_name: str) -> dict:
        """Parse a single RSS entry into our standard format."""
        # Extract published date
        published = ""
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published = datetime(*entry.published_parsed[:6], tzinfo=IST).isoformat()
            except Exception:
                published = getattr(entry, "published", "")
        elif hasattr(entry, "published"):
            published = entry.published

        # Clean summary (strip HTML tags)
        summary = getattr(entry, "summary", "")
        if summary:
            summary = BeautifulSoup(summary, "html.parser").get_text(strip=True)
            # Truncate overly long summaries
            if len(summary) > 500:
                summary = summary[:497] + "..."

        return {
            "headline": getattr(entry, "title", "Untitled"),
            "summary": summary,
            "source": source_name,
            "url": getattr(entry, "link", ""),
            "published_at": published,
            "category": "national",
        }

    def _deduplicate(self, articles: list[dict]) -> list[dict]:
        """Remove articles with very similar headlines."""
        seen_keys = set()
        unique = []
        for article in articles:
            # Normalize headline for comparison
            key = article["headline"].lower().strip()
            # Use first 50 chars as dedup key (catches minor variations)
            short_key = key[:50]
            if short_key not in seen_keys:
                seen_keys.add(short_key)
                unique.append(article)
        return unique


# ══════════════════════════════════════════════════════════════════
#                 BUCKET 2: GLOBAL GOSSIP 🌍
# ══════════════════════════════════════════════════════════════════

class GlobalGossipFetcher:
    """Fetches international news from RSS feeds."""

    def __init__(self):
        self.articles = []

    def fetch(self) -> list[dict]:
        """Fetch articles from all international RSS sources."""
        log.info("Fetching Global Gossip...")

        for source_name, feed_urls in INTERNATIONAL_FEEDS.items():
            fetched = self._fetch_source(source_name, feed_urls)
            self.articles.extend(fetched)
            log.info(f"   + {source_name}: {len(fetched)} articles")

        self.articles = self._deduplicate(self.articles)
        log.info(f"   Total international articles: {len(self.articles)}")
        return self.articles

    def _fetch_source(self, source_name: str, feed_urls: list[str]) -> list[dict]:
        """Try each feed URL until one works."""
        for url in feed_urls:
            try:
                feed = feedparser.parse(url)
                if feed.bozo and not feed.entries:
                    continue

                articles = []
                for entry in feed.entries[:MAX_ARTICLES_INTERNATIONAL]:
                    articles.append(self._parse_entry(entry, source_name))
                return articles

            except Exception as e:
                log.warning(f"   ! {source_name} feed failed ({url}): {e}")
                continue

        log.error(f"   X {source_name}: all feeds failed")
        return []

    def _parse_entry(self, entry: dict, source_name: str) -> dict:
        """Parse a single RSS entry into our standard format."""
        published = ""
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                published = getattr(entry, "published", "")
        elif hasattr(entry, "published"):
            published = entry.published

        summary = getattr(entry, "summary", "")
        if summary:
            summary = BeautifulSoup(summary, "html.parser").get_text(strip=True)
            if len(summary) > 500:
                summary = summary[:497] + "..."

        return {
            "headline": getattr(entry, "title", "Untitled"),
            "summary": summary,
            "source": source_name,
            "url": getattr(entry, "link", ""),
            "published_at": published,
            "region": "world",
        }

    def _deduplicate(self, articles: list[dict]) -> list[dict]:
        """Remove articles with very similar headlines."""
        seen_keys = set()
        unique = []
        for article in articles:
            key = article["headline"].lower().strip()[:50]
            if key not in seen_keys:
                seen_keys.add(key)
                unique.append(article)
        return unique


# ══════════════════════════════════════════════════════════════════
#              BUCKET 3: WORD OF THE DAY 📖
# ══════════════════════════════════════════════════════════════════

class WordOfTheDayFetcher:
    """Fetches a daily vocabulary word with definition and usage."""

    def fetch(self) -> dict:
        """Get today's Word of the Day."""
        log.info("📖 Fetching Word of the Day...")

        # Strategy 1: Try Wordnik API if key is available
        if WORDNIK_API_KEY:
            result = self._fetch_wordnik()
            if result:
                log.info(f"   ✓ Wordnik: \"{result['word']}\"")
                return result
            log.warning("   ⚠ Wordnik failed, falling back to Free Dictionary API")

        # Strategy 2: Pick from curated list + fetch definition from Free Dictionary API
        word = self._pick_daily_word()
        result = self._fetch_free_dictionary(word)

        if result:
            log.info(f"   ✓ Free Dictionary: \"{result['word']}\"")
        else:
            # Bare minimum fallback
            log.warning(f"   ⚠ Definition lookup failed for \"{word}\"")
            result = {
                "word": word,
                "phonetic": "",
                "part_of_speech": "",
                "definition": "Definition unavailable — look this one up, it's a great word!",
                "example": "",
                "source": "curated_list",
            }

        return result

    def _pick_daily_word(self) -> str:
        """Deterministically pick a word based on today's date."""
        # Hash the date string to get a stable index for the day
        date_hash = int(hashlib.md5(TODAY.encode()).hexdigest(), 16)
        index = date_hash % len(CURATED_WORDS)
        return CURATED_WORDS[index]

    def _fetch_wordnik(self) -> Optional[dict]:
        """Fetch Word of the Day from Wordnik API."""
        try:
            resp = requests.get(
                WORDNIK_WOTD_URL,
                params={"api_key": WORDNIK_API_KEY},
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            definitions = data.get("definitions", [])
            examples = data.get("examples", [])

            return {
                "word": data.get("word", ""),
                "phonetic": "",  # Wordnik WOTD doesn't always include phonetics
                "part_of_speech": definitions[0].get("partOfSpeech", "") if definitions else "",
                "definition": definitions[0].get("text", "") if definitions else "",
                "example": examples[0].get("text", "") if examples else "",
                "source": "wordnik",
            }
        except Exception as e:
            log.warning(f"   ⚠ Wordnik API error: {e}")
            return None

    def _fetch_free_dictionary(self, word: str) -> Optional[dict]:
        """Fetch word definition from Free Dictionary API (no key required)."""
        try:
            resp = requests.get(
                f"{FREE_DICT_API}/{word}",
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            if not isinstance(data, list) or not data:
                return None

            entry = data[0]
            phonetic = entry.get("phonetic", "")
            if not phonetic:
                # Try to find phonetic from phonetics array
                for p in entry.get("phonetics", []):
                    if p.get("text"):
                        phonetic = p["text"]
                        break

            # Find the first good definition
            part_of_speech = ""
            definition = ""
            example = ""
            for meaning in entry.get("meanings", []):
                part_of_speech = meaning.get("partOfSpeech", "")
                defs = meaning.get("definitions", [])
                if defs:
                    definition = defs[0].get("definition", "")
                    example = defs[0].get("example", "")
                    break

            return {
                "word": word,
                "phonetic": phonetic,
                "part_of_speech": part_of_speech,
                "definition": definition,
                "example": example,
                "source": "free_dictionary_api",
            }
        except Exception as e:
            log.warning(f"   ⚠ Free Dictionary API error for \"{word}\": {e}")
            return None


# ══════════════════════════════════════════════════════════════════
#              BUCKET 4: THE BAG CHECK 💰
# ══════════════════════════════════════════════════════════════════

class BagCheckFetcher:
    """
    Fetches mutual fund / investment data.

    Primary:  Parse Groww Digest emails via Gmail IMAP
    Fallback: Fetch NAV data from MFAPI.in using scheme codes
    """

    def __init__(self):
        self.source = "unknown"

    def fetch(self) -> dict:
        """Fetch finance data — tries email first, then MFAPI fallback."""
        log.info("💰 Fetching The Bag Check...")

        # ── Attempt 1: Gmail IMAP (Groww Digest) ────────────────
        if GMAIL_ADDRESS and GMAIL_APP_PASSWORD:
            email_data = self._fetch_groww_digest()
            if email_data and email_data.get("funds"):
                self.source = "groww_digest"
                log.info(f"   ✓ Groww Digest: {len(email_data['funds'])} funds found")
                return self._build_result(email_data["funds"], "groww_digest")
            else:
                log.warning("   ⚠ No Groww Digest found, using MFAPI fallback")
        else:
            log.info("   ℹ No Gmail credentials configured, using MFAPI fallback")

        # ── Attempt 2: MFAPI.in fallback ────────────────────────
        funds = self._fetch_mfapi_fallback()
        self.source = "mfapi_fallback"
        log.info(f"   ✓ MFAPI: {len(funds)} funds fetched")
        return self._build_result(funds, "mfapi_fallback")

    def _fetch_groww_digest(self) -> Optional[dict]:
        """
        Connect to Gmail via IMAP, find latest Groww Digest email,
        and parse the HTML body for fund data.
        """
        mail = None
        try:
            # Connect
            mail = imaplib.IMAP4_SSL(IMAP_SERVER)
            mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            mail.select("inbox")

            # Search for Groww Digest emails from today or yesterday
            yesterday = (NOW - timedelta(days=1)).strftime("%d-%b-%Y")
            search_criteria = (
                f'(FROM "{GROWW_SENDER}" SUBJECT "digest" SINCE "{yesterday}")'
            )
            status, data = mail.search(None, search_criteria)

            if status != "OK" or not data[0]:
                log.info("   ℹ No matching Groww Digest emails found")
                return None

            # Fetch the most recent email
            email_ids = data[0].split()
            latest_id = email_ids[-1]  # Most recent

            status, msg_data = mail.fetch(latest_id, "(RFC822)")
            if status != "OK":
                return None

            # Parse email
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email_lib.message_from_bytes(response_part[1])
                    return self._parse_groww_email(msg)

            return None

        except imaplib.IMAP4.error as e:
            log.error(f"   ✗ IMAP authentication failed: {e}")
            log.error("     → Check your Gmail App Password and IMAP settings")
            return None
        except Exception as e:
            log.error(f"   ✗ Gmail fetch error: {e}")
            return None
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass

    def _parse_groww_email(self, msg) -> Optional[dict]:
        """Extract mutual fund data from Groww Digest email HTML."""
        html_body = ""

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    html_body = payload.decode(charset, errors="replace")
                    break
        else:
            content_type = msg.get_content_type()
            if content_type == "text/html":
                payload = msg.get_payload(decode=True)
                charset = msg.get_content_charset() or "utf-8"
                html_body = payload.decode(charset, errors="replace")

        if not html_body:
            # Try plain text
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        charset = part.get_content_charset() or "utf-8"
                        html_body = payload.decode(charset, errors="replace")
                        break
            else:
                payload = msg.get_payload(decode=True)
                charset = msg.get_content_charset() or "utf-8"
                html_body = payload.decode(charset, errors="replace")

        if not html_body:
            return None

        return self._extract_fund_data_from_html(html_body)

    def _extract_fund_data_from_html(self, html: str) -> dict:
        """
        Parse fund data from Groww Digest HTML.

        Groww Digest emails typically contain tables with:
        - Fund name
        - Current NAV
        - Daily/recent change percentage

        Since Groww may change their email format, we use multiple
        extraction strategies.
        """
        import re

        soup = BeautifulSoup(html, "html.parser")
        funds = []

        # Strategy 1: Look for table rows with fund data
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                cell_texts = [c.get_text(strip=True) for c in cells]

                # Skip header rows or empty rows
                if len(cell_texts) < 2:
                    continue

                # Look for rows that contain NAV-like numbers
                fund_entry = self._try_parse_row(cell_texts)
                if fund_entry:
                    funds.append(fund_entry)

        # Strategy 2: Regex fallback for common patterns
        if not funds:
            # Pattern: "Fund Name ... ₹123.45 ... +1.23%" or similar
            nav_pattern = re.compile(
                r'([A-Za-z][A-Za-z\s\-&().]+(?:Fund|Growth|Plan))'
                r'[\s\S]*?'
                r'(?:₹|Rs\.?|NAV:?\s*)[\s]*'
                r'([\d,]+\.?\d*)'
                r'[\s\S]*?'
                r'([+-]?\d+\.?\d*)\s*%',
                re.IGNORECASE,
            )
            text = soup.get_text()
            for match in nav_pattern.finditer(text):
                name = match.group(1).strip()
                nav = match.group(2).replace(",", "")
                change = match.group(3)
                funds.append({
                    "name": name,
                    "scheme_code": "",
                    "nav": float(nav),
                    "prev_nav": None,
                    "daily_change_pct": float(change),
                    "trend": "up" if float(change) >= 0 else "down",
                })

        return {"funds": funds}

    def _try_parse_row(self, cells: list[str]) -> Optional[dict]:
        """Try to extract fund data from a table row's cell texts."""
        import re

        # Look for a cell that looks like a fund name
        fund_name = None
        nav_value = None
        change_pct = None

        for cell in cells:
            # Check if cell is a fund name (contains "Fund", "Growth", etc.)
            if any(
                keyword in cell
                for keyword in ["Fund", "Growth", "Direct", "Plan", "Cap", "Flexi"]
            ):
                fund_name = cell.strip()

            # Check if cell looks like a NAV (number, possibly with ₹)
            nav_match = re.search(r'(?:₹|Rs\.?)?\s*([\d,]+\.\d{2,4})', cell)
            if nav_match and not nav_value:
                nav_value = float(nav_match.group(1).replace(",", ""))

            # Check if cell has a percentage
            pct_match = re.search(r'([+-]?\d+\.?\d*)\s*%', cell)
            if pct_match:
                change_pct = float(pct_match.group(1))

        if fund_name and (nav_value is not None or change_pct is not None):
            return {
                "name": fund_name,
                "scheme_code": "",
                "nav": nav_value or 0.0,
                "prev_nav": None,
                "daily_change_pct": change_pct or 0.0,
                "trend": "up" if (change_pct or 0) >= 0 else "down",
            }

        return None

    def _fetch_mfapi_fallback(self) -> list[dict]:
        """Fetch latest NAV data from MFAPI.in for fallback funds."""
        funds = []

        for fund_info in FALLBACK_FUNDS:
            try:
                # Fetch historical data (includes latest + previous NAVs)
                resp = requests.get(
                    f"{MFAPI_BASE}/{fund_info['scheme_code']}",
                    headers=HEADERS,
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()

                nav_data = data.get("data", [])
                meta = data.get("meta", {})

                if not nav_data:
                    log.warning(f"   ⚠ No NAV data for {fund_info['name']}")
                    continue

                # Latest NAV
                latest = nav_data[0]
                latest_nav = float(latest["nav"])
                latest_date = latest["date"]

                # Previous NAV (for daily change calculation)
                prev_nav = None
                daily_change = 0.0
                if len(nav_data) > 1:
                    prev_nav = float(nav_data[1]["nav"])
                    if prev_nav > 0:
                        daily_change = round(
                            ((latest_nav - prev_nav) / prev_nav) * 100, 2
                        )

                funds.append({
                    "name": meta.get("scheme_name", fund_info["name"]),
                    "scheme_code": fund_info["scheme_code"],
                    "nav": latest_nav,
                    "prev_nav": prev_nav,
                    "nav_date": latest_date,
                    "daily_change_pct": daily_change,
                    "trend": "up" if daily_change >= 0 else "down",
                })

                log.info(
                    f"   ✓ {fund_info['name'][:40]}... "
                    f"NAV: ₹{latest_nav:.2f} ({daily_change:+.2f}%)"
                )

            except Exception as e:
                log.warning(f"   ⚠ MFAPI error for {fund_info['name']}: {e}")
                continue

        return funds

    def _build_result(self, funds: list[dict], source: str) -> dict:
        """Assemble the final Bag Check result."""
        # Determine overall sentiment
        if not funds:
            sentiment = "no_data"
        else:
            changes = [f.get("daily_change_pct", 0) for f in funds]
            avg_change = sum(changes) / len(changes) if changes else 0
            if avg_change > 0.5:
                sentiment = "green"
            elif avg_change < -0.5:
                sentiment = "red"
            else:
                sentiment = "neutral"

        return {
            "source": source,
            "as_of": TODAY,
            "funds": funds,
            "overall_sentiment": sentiment,
        }


# ══════════════════════════════════════════════════════════════════
#                    OUTPUT ASSEMBLER
# ══════════════════════════════════════════════════════════════════

def assemble_newspaper(
    domestic: list[dict],
    international: list[dict],
    word: dict,
    finance: dict,
    statuses: dict[str, str],
) -> dict:
    """Assemble all four buckets into the final newspaper JSON."""
    return {
        "metadata": {
            "generated_at": NOW.isoformat(),
            "edition_date": TODAY,
            "generator": "news_fetcher.py v1.0",
            "fetch_status": statuses,
        },
        "domestic_tea": domestic,
        "global_gossip": international,
        "word_of_the_day": word,
        "the_bag_check": finance,
    }


def save_output(data: dict, filepath: Path) -> None:
    """Save the newspaper JSON to disk."""
    filepath.parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    size_kb = filepath.stat().st_size / 1024
    log.info(f"💾 Saved to: {filepath} ({size_kb:.1f} KB)")


# ══════════════════════════════════════════════════════════════════
#                     CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="🗞️  Daily Newspaper — Data Fetcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python news_fetcher.py                    Fetch all four buckets
  python news_fetcher.py --bucket domestic  Fetch only Domestic Tea
  python news_fetcher.py --bucket finance   Fetch only The Bag Check
  python news_fetcher.py --dry-run          Preview without saving
  python news_fetcher.py -o custom.json     Save to a custom path
        """,
    )
    parser.add_argument(
        "--bucket",
        choices=["domestic", "international", "word", "finance", "all"],
        default="all",
        help="Which bucket to fetch (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data but don't save to disk (prints to stdout instead)",
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        help="Custom output file path (default: data/datasets/newspaper_feed_YYYY-MM-DD.json)",
    )

    args = parser.parse_args()

    # ── Banner ───────────────────────────────────────────────────
    print()
    print("  +----------------------------------------------+")
    print("  |        DAILY NEWSPAPER FETCHER               |")
    print(f"  |        Date:  {TODAY}                    |")
    print(f"  |        Time:  {NOW.strftime('%H:%M:%S')} IST                  |")
    print("  +----------------------------------------------+")
    print()

    # ── Track statuses ───────────────────────────────────────────
    statuses = {
        "domestic_tea": "skipped",
        "global_gossip": "skipped",
        "word_of_the_day": "skipped",
        "the_bag_check": "skipped",
    }

    domestic_articles = []
    international_articles = []
    word_data = {}
    finance_data = {}

    # ── Fetch requested buckets ──────────────────────────────────
    try:
        if args.bucket in ("all", "domestic"):
            try:
                domestic_articles = DomesticTeaFetcher().fetch()
                statuses["domestic_tea"] = "success" if domestic_articles else "empty"
            except Exception as e:
                log.error(f"❌ Domestic Tea fetch failed: {e}")
                statuses["domestic_tea"] = "error"

        if args.bucket in ("all", "international"):
            try:
                international_articles = GlobalGossipFetcher().fetch()
                statuses["global_gossip"] = "success" if international_articles else "empty"
            except Exception as e:
                log.error(f"❌ Global Gossip fetch failed: {e}")
                statuses["global_gossip"] = "error"

        if args.bucket in ("all", "word"):
            try:
                word_data = WordOfTheDayFetcher().fetch()
                statuses["word_of_the_day"] = "success" if word_data.get("definition") else "partial"
            except Exception as e:
                log.error(f"❌ Word of the Day fetch failed: {e}")
                statuses["word_of_the_day"] = "error"

        if args.bucket in ("all", "finance"):
            try:
                fetcher = BagCheckFetcher()
                finance_data = fetcher.fetch()
                statuses["the_bag_check"] = (
                    "success" if fetcher.source == "groww_digest" else "fallback"
                )
            except Exception as e:
                log.error(f"❌ Bag Check fetch failed: {e}")
                statuses["the_bag_check"] = "error"

    except KeyboardInterrupt:
        log.info("\n⛔ Interrupted by user")
        sys.exit(1)

    # ── Assemble output ──────────────────────────────────────────
    newspaper = assemble_newspaper(
        domestic=domestic_articles,
        international=international_articles,
        word=word_data,
        finance=finance_data,
        statuses=statuses,
    )

    # ── Output ───────────────────────────────────────────────────
    if args.dry_run:
        print("\n" + "-" * 50)
        print("  DRY RUN -- Preview (not saved)")
        print("-" * 50 + "\n")
        print(json.dumps(newspaper, indent=2, ensure_ascii=False, default=str))
    else:
        output_path = Path(args.output) if args.output else OUTPUT_FILE
        save_output(newspaper, output_path)

    # ── Summary ──────────────────────────────────────────────────
    print()
    print("  +---------------------------------------------+")
    print("  |              FETCH SUMMARY                  |")
    print("  +---------------------------------------------+")
    print(f"  |  Domestic Tea:    {len(domestic_articles):>3} articles     [{statuses['domestic_tea']:>8}]  |")
    print(f"  |  Global Gossip:   {len(international_articles):>3} articles     [{statuses['global_gossip']:>8}]  |")
    print(f"  |  Word of the Day: {'Y' if word_data.get('word') else 'N':>3}              [{statuses['word_of_the_day']:>8}]  |")
    print(f"  |  Bag Check:       {len(finance_data.get('funds', [])):>3} funds        [{statuses['the_bag_check']:>8}]  |")
    print("  +---------------------------------------------+")
    print()


def _status_icon(status: str) -> str:
    """Map status string to a visual label."""
    icons = {
        "success": "OK",
        "partial": "PARTIAL",
        "fallback": "FALLBACK",
        "empty": "EMPTY",
        "error": "ERROR",
        "skipped": "SKIP",
    }
    return icons.get(status, "?")


# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()
