"""
osti_id_sitrep.py
=================
Two-stage pipeline:
  Stage 1 – Scrape publication titles from a Google Scholar profile page
             sorted by date descending, stopping when a publication year
             older than STOP_YEAR (2016) is encountered.
  Stage 2 – For each title, search OSTI.gov, traverse to the best-matching
             result page, extract the OSTI ID, and write to one of two files:
               - MATCHED_FILE  : title + OSTI ID (CSV)
               - UNMATCHED_FILE: title + reason code (CSV)

Dependencies
------------
    pip install requests beautifulsoup4
    # For the Google Scholar browser-automation fallback only:
    pip install playwright && playwright install chromium
    # OR:
    pip install selenium webdriver-manager

Run
---
    python osti_id_sitrep.py [--sample N] [--browser {playwright,selenium}]

    --sample N   Process only the first N titles (default: all)
    --browser    Force browser mode for Scholar even if static works
                 (required when Scholar returns a CAPTCHA/empty page)

Output files are written to the same directory as this script.
"""

import argparse
import csv
import logging
import os
import re
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Iterator

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCHOLAR_URL = (
    "https://scholar.google.com/citations"
    "?hl=en&user=6BzBj3IAAAAJ&view_op=list_works&sortby=pubdate"
)

OSTI_SEARCH_BASE = "https://www.osti.gov/search/semantic:"
OSTI_BIBLIO_BASE = "https://www.osti.gov/biblio/"

# Stop traversing Scholar pages when a publication year <= this value is seen.
STOP_YEAR = 2016

# Fuzzy-match similarity threshold (0.0–1.0).  Titles below this are unmatched.
MATCH_THRESHOLD = 0.80

# Seconds to wait between HTTP requests (be respectful of server rate limits).
REQUEST_DELAY = 1.5

# HTTP request timeout in seconds.
REQUEST_TIMEOUT = 20

# Maximum retries on transient errors (429 / 5xx).
MAX_RETRIES = 3

# Backoff multiplier (seconds × attempt number).
BACKOFF_BASE = 3

# Output files (relative to script directory).
SCRIPT_DIR = Path(__file__).parent
MATCHED_FILE = SCRIPT_DIR / "osti_matched.csv"
UNMATCHED_FILE = SCRIPT_DIR / "osti_unmatched.csv"
LOG_FILE = SCRIPT_DIR / "osti_id_sitrep.log"

# Realistic browser-like headers for static HTTP requests.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    """Lower-case, strip accents, collapse whitespace, remove punctuation."""
    title = unicodedata.normalize("NFD", title)
    title = "".join(c for c in title if unicodedata.category(c) != "Mn")
    title = title.lower()
    title = re.sub(r"[^\w\s]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def title_similarity(a: str, b: str) -> float:
    """
    Token-overlap Jaccard similarity between two normalised title strings.
    Fast and good enough for publication-title matching.
    """
    set_a = set(normalize_title(a).split())
    set_b = set(normalize_title(b).split())
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union


def fetch_with_retry(
    session: requests.Session,
    url: str,
    params: dict | None = None,
) -> requests.Response | None:
    """GET request with exponential backoff on 429/5xx responses."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_DELAY)
            resp = session.get(
                url,
                params=params,
                headers=HTTP_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = BACKOFF_BASE * attempt
                log.warning(
                    "HTTP %s from %s – retrying in %ss (attempt %d/%d)",
                    resp.status_code, url, wait, attempt, MAX_RETRIES,
                )
                time.sleep(wait)
            else:
                log.error("HTTP %s from %s – skipping", resp.status_code, url)
                return None
        except requests.RequestException as exc:
            wait = BACKOFF_BASE * attempt
            log.warning("Request error (%s) – retrying in %ss", exc, wait)
            time.sleep(wait)
    log.error("All retries exhausted for %s", url)
    return None


# ---------------------------------------------------------------------------
# Stage 1 – Collect Scholar titles
# ---------------------------------------------------------------------------

def _extract_titles_and_years_from_html(html: str) -> list[tuple[str, int | None]]:
    """
    Parse a Google Scholar 'list_works' HTML page.

    Returns a list of (title, year) tuples.  Year is None if not found.
    The Scholar page renders each publication as a row with:
      - title in <a class="gsc_a_at">
      - year in a <span class="gsc_a_h gsc_a_hc gs_ibl"> element inside
        a <td class="gsc_a_y"> cell
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("tr.gsc_a_tr")
    results: list[tuple[str, int | None]] = []
    for row in rows:
        title_tag = row.select_one("a.gsc_a_at")
        year_tag = row.select_one("td.gsc_a_y span")
        title = title_tag.get_text(strip=True) if title_tag else None
        year_text = year_tag.get_text(strip=True) if year_tag else ""
        try:
            year = int(year_text)
        except ValueError:
            year = None
        if title:
            results.append((title, year))
    return results


def collect_titles_static(max_titles: int | None = None) -> list[str]:
    """
    Attempt to collect publication titles from Google Scholar using plain HTTP.

    Google Scholar aggressively blocks bots, so this will often fail (empty
    result set or CAPTCHA redirect).  If it returns an empty list, call
    collect_titles_browser() instead.

    Scholar paginates via &cstart=<offset>&pagesize=<n>.  We iterate pages
    until we encounter a year <= STOP_YEAR or run out of results.
    """
    session = requests.Session()
    titles: list[str] = []
    page_size = 100
    offset = 0
    seen: set[str] = set()

    log.info("Stage 1 (static): collecting Scholar titles from %s", SCHOLAR_URL)

    while True:
        params: dict = {"cstart": offset, "pagesize": page_size}
        url = SCHOLAR_URL  # base URL already has required query params
        full_url = f"{SCHOLAR_URL}&cstart={offset}&pagesize={page_size}"
        resp = fetch_with_retry(session, full_url)
        if resp is None:
            log.warning("Failed to fetch Scholar page at offset %d", offset)
            break

        rows = _extract_titles_and_years_from_html(resp.text)
        if not rows:
            log.info("No publication rows found at offset %d – stopping.", offset)
            break

        stop_flag = False
        for title, year in rows:
            if year is not None and year <= STOP_YEAR:
                log.info("Reached year %d (<= %d) – stopping pagination.", year, STOP_YEAR)
                stop_flag = True
                break
            key = normalize_title(title)
            if key not in seen:
                seen.add(key)
                titles.append(title)
                if max_titles and len(titles) >= max_titles:
                    stop_flag = True
                    break

        if stop_flag:
            break

        offset += page_size

    log.info("Stage 1 (static): collected %d titles.", len(titles))
    return titles


def collect_titles_playwright(max_titles: int | None = None) -> list[str]:
    """
    Collect publication titles using Playwright (headless Chromium).

    Required when Scholar's anti-bot measures block static requests.

    Install once with:
        pip install playwright && playwright install chromium

    Scholar's JavaScript-rendered publication list exposes an infinite-scroll
    'Show more' button.  We click it repeatedly until we see a year <= STOP_YEAR
    or reach max_titles.  Titles and years are read from the live DOM after
    each expansion.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        raise RuntimeError(
            "Playwright is not installed.  Run:\n"
            "    pip install playwright && playwright install chromium"
        )

    titles: list[str] = []
    seen: set[str] = set()

    log.info("Stage 1 (Playwright): launching headless browser for Scholar.")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=HTTP_HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page.goto(SCHOLAR_URL, wait_until="networkidle", timeout=60_000)

        stop_flag = False
        while not stop_flag:
            # Parse the current DOM state.
            html = page.content()
            rows = _extract_titles_and_years_from_html(html)

            for title, year in rows:
                key = normalize_title(title)
                if key in seen:
                    continue
                if year is not None and year <= STOP_YEAR:
                    log.info(
                        "Reached year %d (<= %d) – stopping.", year, STOP_YEAR
                    )
                    stop_flag = True
                    break
                seen.add(key)
                titles.append(title)
                if max_titles and len(titles) >= max_titles:
                    stop_flag = True
                    break

            if stop_flag:
                break

            # Try to click 'Show more'.
            show_more = page.query_selector("button#gsc_bpf_more:not([disabled])")
            if show_more is None:
                log.info("No 'Show more' button found – pagination complete.")
                break
            show_more.click()
            page.wait_for_timeout(2000)  # wait for DOM update

        browser.close()

    log.info("Stage 1 (Playwright): collected %d titles.", len(titles))
    return titles


def collect_titles_selenium(max_titles: int | None = None) -> list[str]:
    """
    Collect publication titles using Selenium + ChromeDriver.

    Alternative to Playwright when Selenium is already available.

    Install once with:
        pip install selenium webdriver-manager
    """
    try:
        from selenium import webdriver  # type: ignore
        from selenium.webdriver.chrome.options import Options  # type: ignore
        from selenium.webdriver.common.by import By  # type: ignore
        from selenium.webdriver.support import expected_conditions as EC  # type: ignore
        from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
        from webdriver_manager.chrome import ChromeDriverManager  # type: ignore
        from selenium.webdriver.chrome.service import Service  # type: ignore
    except ImportError:
        raise RuntimeError(
            "Selenium or webdriver-manager is not installed.  Run:\n"
            "    pip install selenium webdriver-manager"
        )

    titles: list[str] = []
    seen: set[str] = set()

    log.info("Stage 1 (Selenium): launching headless browser for Scholar.")

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-agent={HTTP_HEADERS['User-Agent']}")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        driver.get(SCHOLAR_URL)
        stop_flag = False
        while not stop_flag:
            html = driver.page_source
            rows = _extract_titles_and_years_from_html(html)

            for title, year in rows:
                key = normalize_title(title)
                if key in seen:
                    continue
                if year is not None and year <= STOP_YEAR:
                    log.info(
                        "Reached year %d (<= %d) – stopping.", year, STOP_YEAR
                    )
                    stop_flag = True
                    break
                seen.add(key)
                titles.append(title)
                if max_titles and len(titles) >= max_titles:
                    stop_flag = True
                    break

            if stop_flag:
                break

            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.ID, "gsc_bpf_more"))
                )
                if not btn.is_enabled():
                    break
                btn.click()
                time.sleep(2)
            except Exception:
                log.info("No 'Show more' button – pagination complete.")
                break
    finally:
        driver.quit()

    log.info("Stage 1 (Selenium): collected %d titles.", len(titles))
    return titles


# ---------------------------------------------------------------------------
# Stage 2 – Look up each title on OSTI
# ---------------------------------------------------------------------------

def _osti_id_from_url(url: str) -> str | None:
    """Extract numeric OSTI ID from a /biblio/<id> URL."""
    m = re.search(r"/biblio/(\d+)", url)
    return m.group(1) if m else None


def search_osti_for_title(
    session: requests.Session,
    title: str,
) -> tuple[str | None, str | None]:
    """
    Search OSTI.gov for *title* and return (osti_id, detail_url) of the
    best-matching result, or (None, None) if no confident match is found.

    Strategy:
      1. Fetch the OSTI search results page for the title.
      2. Collect all /biblio/<id> candidate links with their heading text.
      3. Score each candidate by token-overlap Jaccard similarity to *title*.
      4. Accept the best candidate only if score >= MATCH_THRESHOLD.
      5. Extract OSTI ID from the candidate URL (avoids a second HTTP request
         in the common case; detail-page traversal is used only for
         confirmation if the ID cannot be parsed from the URL).
    """
    search_url = OSTI_SEARCH_BASE + urllib.parse.quote(title, safe="")
    resp = fetch_with_retry(session, search_url)
    if resp is None:
        return None, None

    soup = BeautifulSoup(resp.text, "html.parser")

    best_score = 0.0
    best_url: str | None = None
    best_title: str | None = None

    # Result headings are <h2> or <h3> tags containing <a href="/biblio/...">
    for heading in soup.find_all(["h2", "h3"]):
        link = heading.find("a", href=re.compile(r"^https://www\.osti\.gov/biblio/\d+"))
        if link is None:
            link = heading.find("a", href=re.compile(r"^/biblio/\d+"))
        if link is None:
            continue
        candidate_title = link.get_text(strip=True)
        href = link["href"]
        if not href.startswith("http"):
            href = "https://www.osti.gov" + href
        score = title_similarity(title, candidate_title)
        if score > best_score:
            best_score = score
            best_url = href
            best_title = candidate_title

    if best_score < MATCH_THRESHOLD or best_url is None:
        log.info(
            "No confident OSTI match for: %r (best score=%.2f)", title, best_score
        )
        return None, None

    log.info(
        "Matched (score=%.2f): %r -> %s", best_score, best_title, best_url
    )

    # Extract OSTI ID from URL first (fastest path).
    osti_id = _osti_id_from_url(best_url)
    if osti_id:
        return osti_id, best_url

    # Fallback: traverse to detail page and parse the OSTI ID field.
    osti_id = extract_osti_id_from_detail_page(session, best_url)
    return osti_id, best_url


def extract_osti_id_from_detail_page(
    session: requests.Session,
    url: str,
) -> str | None:
    """
    Fetch an OSTI biblio detail page and extract the 'OSTI ID' metadata field.

    The page renders metadata as <dt>OSTI ID:</dt><dd>12345678</dd> pairs.
    """
    resp = fetch_with_retry(session, url)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Method 1: <dt>OSTI ID:</dt> -> sibling <dd>
    for dt in soup.find_all("dt"):
        if "osti id" in dt.get_text(strip=True).lower():
            dd = dt.find_next_sibling("dd")
            if dd:
                osti_id = dd.get_text(strip=True)
                if re.match(r"^\d+$", osti_id):
                    return osti_id

    # Method 2: text pattern "OSTI ID:12345678" anywhere on page
    m = re.search(r"OSTI\s+ID[:\s]+(\d+)", resp.text)
    if m:
        return m.group(1)

    log.warning("Could not extract OSTI ID from detail page: %s", url)
    return None


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _ensure_csv_header(path: Path, fieldnames: list[str]) -> None:
    """Write CSV header row if the file does not yet exist or is empty."""
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()


def _csv_contains_title(path: Path, title: str) -> bool:
    """Return True when *title* is already present in the CSV's title column."""
    if not path.exists() or path.stat().st_size == 0:
        return False

    target_title = normalize_title(title)
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            existing_title = row.get("title")
            if existing_title and normalize_title(existing_title) == target_title:
                return True
    return False


def append_matched(title: str, osti_id: str, osti_url: str) -> None:
    fieldnames = ["title", "osti_id", "osti_url"]
    _ensure_csv_header(MATCHED_FILE, fieldnames)
    if _csv_contains_title(MATCHED_FILE, title):
        log.info("Skipping matched duplicate already in CSV: %r", title)
        return
    with MATCHED_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="|")
        writer.writerow({"title": title, "osti_id": osti_id, "osti_url": osti_url})


def append_unmatched(title: str, reason: str) -> None:
    """
    reason codes:
      no_result        – OSTI search returned no results
      low_confidence   – best match scored below MATCH_THRESHOLD
      fetch_error      – HTTP error prevented lookup
    """
    fieldnames = ["title", "reason"]
    _ensure_csv_header(UNMATCHED_FILE, fieldnames)
    if _csv_contains_title(UNMATCHED_FILE, title):
        log.info("Skipping unmatched duplicate already in CSV: %r", title)
        return
    with UNMATCHED_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="|")
        writer.writerow({"title": title, "reason": reason})


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(
    sample: int | None = None,
    browser: str | None = None,
) -> None:
    """
    Main entry point.

    Args:
        sample  : If set, process only the first N Scholar titles.
        browser : 'playwright' or 'selenium' to force browser-mode for Stage 1.
                  If None, static HTTP is tried first; browser mode must be
                  explicitly requested because it requires extra installation.
    """
    # --- Stage 1: collect titles ---
    if browser == "playwright":
        titles = collect_titles_playwright(max_titles=sample)
    elif browser == "selenium":
        titles = collect_titles_selenium(max_titles=sample)
    else:
        titles = collect_titles_static(max_titles=sample)
        if not titles:
            log.warning(
                "Static Scholar fetch returned no titles.\n"
                "Google Scholar likely blocked the request.\n"
                "Re-run with --browser playwright  (or --browser selenium)\n"
                "after installing the required package:\n"
                "    pip install playwright && playwright install chromium\n"
                "    # OR:\n"
                "    pip install selenium webdriver-manager"
            )
            return

    if not titles:
        log.warning("No titles collected – nothing to look up on OSTI.")
        return

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique_titles: list[str] = []
    for t in titles:
        key = normalize_title(t)
        if key not in seen:
            seen.add(key)
            unique_titles.append(t)
    log.info("Unique titles to process: %d", len(unique_titles))

    # --- Stage 2: OSTI lookup ---
    session = requests.Session()
    matched = 0
    unmatched = 0
    errors = 0

    for i, title in enumerate(unique_titles, start=1):
        log.info("[%d/%d] Looking up: %r", i, len(unique_titles), title)
        try:
            osti_id, osti_url = search_osti_for_title(session, title)
            if osti_id:
                append_matched(title, osti_id, osti_url or "")
                matched += 1
            else:
                append_unmatched(title, "low_confidence")
                unmatched += 1
        except Exception as exc:
            log.error("Unexpected error processing %r: %s", title, exc)
            append_unmatched(title, "fetch_error")
            errors += 1

    log.info(
        "Done. matched=%d  unmatched=%d  errors=%d",
        matched, unmatched, errors,
    )
    log.info("Matched output  : %s", MATCHED_FILE)
    log.info("Unmatched output: %s", UNMATCHED_FILE)
    log.info("Log             : %s", LOG_FILE)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrape Scholar titles and find OSTI IDs."
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N titles (useful for testing).",
    )
    parser.add_argument(
        "--browser",
        choices=["playwright", "selenium"],
        default=None,
        help=(
            "Use a headless browser for Google Scholar (required when static "
            "requests are blocked).  Playwright or Selenium must be installed "
            "separately before use."
        ),
    )
    args = parser.parse_args()
    run(sample=args.sample, browser=args.browser)
