"""
gscholscrape.py
===============
Hourly scholar collector for the OSTI workflow.

Stage 1 scrapes publication titles from a Google Scholar profile page, sorted
by date descending, stopping when a publication year older than STOP_YEAR
(2016) is encountered.

Stage 2 looks up only titles that are not already present in the existing
scholar cache, extracts OSTI IDs, and appends any new matches to
/opt/osti/scholar_output/latest_osti_scholar_records.json.

If that cache changes, the downstream OSTI sync is launched. That second
stage is therefore conditional on the scholar cache actually changing.

Dependencies
------------
    pip install requests beautifulsoup4
    # For the Google Scholar browser-automation fallback only:
    pip install playwright && playwright install chromium
    # OR:
    pip install selenium webdriver-manager

Run
---
    python gscholscrape.py [--sample N] [--all] [--browser {playwright,selenium}]

    --sample N   Process only the first N titles
    --all        Process all titles (overrides default development limit)
    --browser    Force browser mode for Scholar even if static works
                 (required when Scholar returns a CAPTCHA/empty page)

Output files are written to the same directory as this script.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

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
LIVE_CBI_JSON = Path("/var/www/html/cbi.json")
DEFAULT_DEV_SAMPLE = 10

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

# Output files (relative to script directory unless overridden).
SCRIPT_DIR = Path(__file__).parent
SCHOLAR_OUTPUT_DIR = Path(os.getenv("SCHOLAR_OUTPUT_DIR", "/opt/osti/scholar_output"))
MATCHED_FILE = SCHOLAR_OUTPUT_DIR / "osti_matched.json"
UNMATCHED_FILE = SCHOLAR_OUTPUT_DIR / "osti_unmatched.json"
TRANSFORM_INPUT_FILE = SCHOLAR_OUTPUT_DIR / "osti_transform_input.json"
LOG_FILE = Path(
    os.getenv(
        "LOG_FILE",
        os.getenv("WORKFLOW_LOG", "/opt/osti/logs/osti_workflow.log"),
    )
)
SCHOLAR_OUTPUT_FILE = Path(
    os.getenv(
        "SCHOLAR_OUTPUT_FILE",
        str(SCHOLAR_OUTPUT_DIR / "latest_osti_scholar_records.json"),
    )
)
DOWNSTREAM_SYNC_SCRIPT = Path(
    os.getenv("DOWNSTREAM_SYNC_SCRIPT", "/opt/osti/osti-tooling/downstream_sync.py")
)

# BRC schema transformation settings
BRC_SCHEMA_DIR = Path(os.getenv("BRC_SCHEMA_DIR", "/opt/osti/brc-schema"))
BRC_OUTPUT_FILE = SCRIPT_DIR / "osti_brc_transformed.json"

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

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
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


def _extract_osti_id_from_identifier(value: Any) -> str | None:
    """Extract numeric OSTI ID from identifier-like values."""
    if isinstance(value, str):
        m = re.search(r"(?:/biblio/|10\.11578/)(\d+)", value)
        if m:
            return m.group(1)
        if re.fullmatch(r"\d+", value.strip()):
            return value.strip()
    elif isinstance(value, list):
        for item in value:
            found = _extract_osti_id_from_identifier(item)
            if found:
                return found
    elif isinstance(value, dict):
        for key in ("identifier", "value", "@id", "url"):
            if key in value:
                found = _extract_osti_id_from_identifier(value.get(key))
                if found:
                    return found
    return None


def _extract_scholar_title(record: dict[str, Any]) -> str | None:
    """Best-effort extraction of a comparable title from scholar cache records."""
    for key in ("title", "source_title", "name", "dataset_title"):
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def load_existing_scholar_cache(
    path: Path = SCHOLAR_OUTPUT_FILE,
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Load existing scholar cache records, OSTI IDs, and normalized titles."""
    existing_records: list[dict[str, Any]] = []
    existing_ids: set[str] = set()
    existing_titles: set[str] = set()

    if not path.exists():
        log.info("Scholar cache file not found: %s (starting fresh)", path)
        return existing_records, existing_ids, existing_titles

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not parse scholar cache file %s: %s", path, exc)
        return existing_records, existing_ids, existing_titles

    if isinstance(raw, dict) and isinstance(raw.get("records"), list):
        records = raw["records"]
    elif isinstance(raw, list):
        records = raw
    else:
        log.warning("Scholar cache file has unexpected structure: %s", path)
        return existing_records, existing_ids, existing_titles

    malformed = 0
    for record in records:
        if not isinstance(record, dict):
            malformed += 1
            continue
        existing_records.append(record)

        osti_id = _extract_osti_id_from_identifier(record.get("osti_id"))
        if osti_id is None:
            osti_id = _extract_osti_id_from_identifier(record.get("identifier"))
        if osti_id is None:
            osti_id = _extract_osti_id_from_identifier(record.get("osti_url"))
        if osti_id is not None:
            existing_ids.add(osti_id)

        title = _extract_scholar_title(record)
        if title:
            existing_titles.add(normalize_title(title))

    log.info(
        "Scholar cache loaded: file=%s records=%d ids=%d titles=%d malformed=%d",
        path,
        len(existing_records),
        len(existing_ids),
        len(existing_titles),
        malformed,
    )
    return existing_records, existing_ids, existing_titles


def load_existing_unmatched_checkpoint(
    path: Path = UNMATCHED_FILE,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Load unmatched checkpoint entries and normalized titles."""
    unmatched_entries: list[dict[str, Any]] = []
    unmatched_titles: set[str] = set()

    if not path.exists():
        return unmatched_entries, unmatched_titles

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not parse unmatched checkpoint file %s: %s", path, exc)
        return unmatched_entries, unmatched_titles

    records: list[Any]
    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict) and isinstance(raw.get("records"), list):
        records = raw["records"]
    else:
        return unmatched_entries, unmatched_titles

    for entry in records:
        if not isinstance(entry, dict):
            continue
        unmatched_entries.append(entry)
        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            unmatched_titles.add(normalize_title(title))

    return unmatched_entries, unmatched_titles


def _scholar_record_identity(record: dict[str, Any]) -> tuple[str, str] | None:
    osti_id = str(record.get("osti_id", "")).strip()
    if osti_id:
        return ("osti_id", osti_id)

    title = _extract_scholar_title(record)
    if title:
        return ("title", normalize_title(title))

    return None


def merge_scholar_records(
    existing_records: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge existing and new records while preserving additivity and order."""
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for record in new_records + existing_records:
        identity = _scholar_record_identity(record)
        if identity is not None and identity in seen:
            continue
        if identity is not None:
            seen.add(identity)
        merged.append(record)

    return merged


def write_json_if_changed(path: Path, records: list[dict[str, Any]]) -> bool:
    """Write JSON only if the serialized content differs from disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new_text = json.dumps(records, indent=2, ensure_ascii=False) + "\n"

    if path.exists():
        try:
            current_text = path.read_text(encoding="utf-8")
        except Exception as exc:
            log.warning("Could not read existing scholar cache %s: %s", path, exc)
        else:
            if current_text == new_text:
                return False

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(new_text, encoding="utf-8")
    tmp_path.replace(path)
    return True


def trigger_downstream_sync() -> bool:
    """Run the downstream OSTI sync only when the scholar cache changes."""
    if not DOWNSTREAM_SYNC_SCRIPT.exists():
        log.error("Downstream sync script not found: %s", DOWNSTREAM_SYNC_SCRIPT)
        return False

    if not os.access(DOWNSTREAM_SYNC_SCRIPT, os.X_OK):
        log.error("Downstream sync script is not executable: %s", DOWNSTREAM_SYNC_SCRIPT)
        return False

    log.info("Scholar cache changed; launching downstream sync: %s", DOWNSTREAM_SYNC_SCRIPT)
    try:
        result = subprocess.run(
            [str(DOWNSTREAM_SYNC_SCRIPT)],
            cwd="/opt/osti/bin",
            timeout=3600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.error("Downstream sync timed out after 3600 seconds")
        return False
    except Exception as exc:
        log.error("Unexpected error launching downstream sync: %s", exc)
        return False

    if result.returncode != 0:
        log.error("Downstream sync exited with code %d", result.returncode)
        return False

    return True


def search_osti_for_title(
    session: requests.Session,
    title: str,
    existing_ids: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Search OSTI.gov for *title* and return (record, reason).

    On success, record is a bioenergy-ready dict enriched with OSTI detail
    payload. On failure, record is None and reason indicates why.

    Strategy:
      1. Fetch the OSTI search results page for the title.
      2. Collect all /biblio/<id> candidate links with their heading text.
      3. Score each candidate by token-overlap Jaccard similarity to *title*.
      4. Accept the best candidate only if score >= MATCH_THRESHOLD.
            5. Always fetch the detail page and extract both OSTI ID and payload.
    """
    search_url = OSTI_SEARCH_BASE + urllib.parse.quote(title, safe="")
    resp = fetch_with_retry(session, search_url)
    if resp is None:
                return None, "fetch_error"

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
        return None, "low_confidence"

    log.info(
        "Matched (score=%.2f): %r -> %s", best_score, best_title, best_url
    )

    candidate_id = _osti_id_from_url(best_url)
    if candidate_id and existing_ids and candidate_id in existing_ids:
        log.info("Skipping existing OSTI ID from live cbi.json: %s", candidate_id)
        return None, "already_exists"

    # Always hit the detail page to validate ID extraction and capture payload.
    osti_id_from_page, payload = fetch_osti_detail_payload(session, best_url)
    osti_id = osti_id_from_page or _osti_id_from_url(best_url)
    if not osti_id:
        log.warning("Could not determine OSTI ID for matched URL: %s", best_url)
        return None, "fetch_error"
    if payload is None:
        log.warning("No structured payload available for matched URL: %s", best_url)
        return None, "fetch_error"
    if existing_ids and osti_id in existing_ids:
        log.info("Skipping existing OSTI ID from live cbi.json: %s", osti_id)
        return None, "already_exists"

    return build_bioenergy_ready_record(
        source_title=title,
        osti_id=osti_id,
        osti_url=best_url,
        payload=payload,
    ), None


def _extract_osti_id_from_soup(
    soup: BeautifulSoup,
    html_text: str,
) -> str | None:
    """Extract OSTI ID metadata from detail-page HTML content."""
    for dt in soup.find_all("dt"):
        if "osti id" in dt.get_text(strip=True).lower():
            dd = dt.find_next_sibling("dd")
            if dd:
                osti_id = dd.get_text(strip=True)
                if re.match(r"^\d+$", osti_id):
                    return osti_id

    m = re.search(r"OSTI\s+ID[:\s]+(\d+)", html_text)
    if m:
        return m.group(1)

    return None


def _extract_json_ld_payload(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Return the best JSON-LD object from an OSTI detail page."""
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    candidates: list[dict[str, Any]] = []

    for script in scripts:
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, dict):
            candidates.append(parsed)
        elif isinstance(parsed, list):
            candidates.extend(item for item in parsed if isinstance(item, dict))

    if not candidates:
        return None

    # Prefer payloads that look like publication records.
    for item in candidates:
        if item.get("@type") in {
            "ScholarlyArticle",
            "Article",
            "Dataset",
            "CreativeWork",
            "Report",
        }:
            return item

    return candidates[0]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_authors_from_payload(payload: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for author in _as_list(payload.get("author")):
        if isinstance(author, str):
            authors.append(author)
        elif isinstance(author, dict):
            name = author.get("name")
            if isinstance(name, str) and name.strip():
                authors.append(name.strip())
    return authors


def _extract_keywords_from_payload(payload: dict[str, Any]) -> list[str]:
    keywords = payload.get("keywords")
    if isinstance(keywords, str):
        return [k.strip() for k in keywords.split(",") if k.strip()]
    if isinstance(keywords, list):
        return [str(k).strip() for k in keywords if str(k).strip()]
    return []


def _extract_doi_from_payload(payload: dict[str, Any]) -> str | None:
    identifier = payload.get("identifier")
    for item in _as_list(identifier):
        if isinstance(item, str):
            m = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", item)
            if m:
                return m.group(0)
        elif isinstance(item, dict):
            for key in ("value", "@id", "url"):
                val = item.get(key)
                if isinstance(val, str):
                    m = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", val)
                    if m:
                        return m.group(0)

    same_as = payload.get("sameAs")
    for item in _as_list(same_as):
        if isinstance(item, str):
            m = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", item)
            if m:
                return m.group(0)

    return None


def fetch_osti_detail_payload(
    session: requests.Session,
    url: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """
    Fetch an OSTI biblio detail page and extract both OSTI ID and payload.

    Payload is taken from JSON-LD (<script type="application/ld+json">).
    """
    resp = fetch_with_retry(session, url)
    if resp is None:
        return None, None

    soup = BeautifulSoup(resp.text, "html.parser")
    osti_id = _extract_osti_id_from_soup(soup, resp.text)
    payload = _extract_json_ld_payload(soup)

    if osti_id is None:
        log.warning("Could not extract OSTI ID from detail page: %s", url)
    if payload is None:
        log.warning("Could not extract JSON-LD payload from detail page: %s", url)

    return osti_id, payload


def build_bioenergy_ready_record(
    source_title: str,
    osti_id: str,
    osti_url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Build a bioenergy-oriented matched record from OSTI detail payload."""
    publisher = payload.get("publisher")
    if isinstance(publisher, dict):
        publisher_name = publisher.get("name")
    else:
        publisher_name = publisher

    payload_title = payload.get("name") or payload.get("headline") or source_title
    payload_url = payload.get("url") or osti_url

    return {
        "osti_id": osti_id,
        "title": payload_title,
        "source_title": source_title,
        "osti_url": osti_url,
        "url": payload_url,
        "product_type": payload.get("@type"),
        "publication_date": payload.get("datePublished"),
        "doi": _extract_doi_from_payload(payload),
        "authors": _extract_authors_from_payload(payload),
        "keywords": _extract_keywords_from_payload(payload),
        "publisher": publisher_name,
        "description": payload.get("description"),
        "payload": payload,
    }


def _write_json_file(path: Path, records: list[dict]) -> None:
    """Write records as a JSON array with stable formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _write_transform_input(path: Path, matched_records: list[dict[str, Any]]) -> None:
    """Write a transform-oriented OSTI records artifact from matched payloads."""
    records: list[dict[str, Any]] = []
    for match in matched_records:
        payload = match.get("payload")
        if not isinstance(payload, dict):
            continue
        record = dict(payload)
        osti_id = str(match.get("osti_id", "")).strip()
        if osti_id and not record.get("osti_id"):
            record["osti_id"] = osti_id
        if osti_id and not record.get("identifier"):
            record["identifier"] = f"https://www.osti.gov/biblio/{osti_id}"
        records.append(record)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps({"records": records}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _run_brc_transform(input_file: Path, output_file: Path) -> bool:
    """Run the brcschema transform on the input file.
    
    Returns True if successful, False otherwise.
    """
    if not input_file.exists():
        log.error("Transform input file does not exist: %s", input_file)
        return False
    
    try:
        log.info("Running brcschema transform on %s", input_file)
        cmd = [
            "uv", "run", "brcschema", "transform",
            "-T", "osti_to_brc",
            "-o", str(output_file),
            str(input_file)
        ]
        
        result = subprocess.run(
            cmd,
            cwd=str(BRC_SCHEMA_DIR),
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode == 0:
            log.info("Transform completed successfully: %s", output_file)
            return True
        else:
            log.error("Transform failed with return code %d", result.returncode)
            if result.stdout:
                log.error("stdout: %s", result.stdout)
            if result.stderr:
                log.error("stderr: %s", result.stderr)
            return False
    except FileNotFoundError:
        log.error("uv command not found; ensure uv is installed and in PATH")
        return False
    except subprocess.TimeoutExpired:
        log.error("Transform timed out after 300 seconds")
        return False
    except Exception as exc:
        log.error("Unexpected error running transform: %s", exc)
        return False


    def _run_elink_to_brc_transform(input_file: Path, output_file: Path) -> bool:
        """Run the ELINK to BRC schema transformation via brcschema.
    
        Returns True if successful, False otherwise.
        """
        if not input_file.exists():
            log.error("Transform input file does not exist: %s", input_file)
            return False
    
        try:
            log.info("Running ELINK to BRC transform on %s", input_file)
            cmd = [
                "uv", "run", "brcschema", "transform",
                "-T", "osti_to_brc",
                "-o", str(output_file),
                str(input_file)
            ]
        
            result = subprocess.run(
                cmd,
                cwd=str(BRC_SCHEMA_DIR),
                capture_output=True,
                text=True,
                timeout=300
            )
        
            if result.returncode == 0:
                log.info("ELINK to BRC transform completed: %s", output_file)
                return True
            else:
                log.error("ELINK to BRC transform failed with return code %d", result.returncode)
                if result.stdout:
                    log.error("stdout: %s", result.stdout)
                if result.stderr:
                    log.error("stderr: %s", result.stderr)
                return False
        except FileNotFoundError:
            log.error("uv command not found; ensure uv is installed and in PATH")
            return False
        except subprocess.TimeoutExpired:
            log.error("ELINK to BRC transform timed out after 300 seconds")
            return False
        except Exception as exc:
            log.error("Unexpected error running ELINK to BRC transform: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(
    sample: int | None = None,
    browser: str | None = None,
) -> bool:
    """
    Main entry point.

    Args:
        sample  : If set, process only the first N Scholar titles.
        browser : 'playwright' or 'selenium' to force browser-mode for Stage 1.
                  If None, static HTTP is tried first; browser mode must be
                  explicitly requested because it requires extra installation.
    """
    existing_records, live_osti_ids, live_titles = load_existing_scholar_cache()
    unmatched_checkpoint_entries, unmatched_checkpoint_titles = load_existing_unmatched_checkpoint()
    processed_titles = set(live_titles) | set(unmatched_checkpoint_titles)

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
    cache_changed = False
    matched_records: list[dict] = []
    unmatched_records: list[dict] = list(unmatched_checkpoint_entries)

    for i, title in enumerate(unique_titles, start=1):
        normalized = normalize_title(title)
        if normalized in processed_titles:
            log.info("[%d/%d] Skipping previously processed title from checkpoint", i, len(unique_titles))
            continue
        log.info("[%d/%d] Looking up: %r", i, len(unique_titles), title)
        try:
            matched_record, reason = search_osti_for_title(session, title, existing_ids=live_osti_ids)
            if matched_record:
                matched_records.append(matched_record)
                live_osti_ids.add(str(matched_record.get("osti_id", "")))
                processed_titles.add(normalized)
                matched += 1

                # Persist newly matched records immediately so interrupted runs can resume quickly.
                merged_records = merge_scholar_records(existing_records, matched_records)
                if write_json_if_changed(SCHOLAR_OUTPUT_FILE, merged_records):
                    cache_changed = True
            else:
                if reason == "already_exists":
                    # Persist this title in checkpoint so future runs skip it.
                    unmatched_records.append({"title": title, "reason": "already_exists"})
                    processed_titles.add(normalized)
                else:
                    unmatched_records.append({"title": title, "reason": reason or "fetch_error"})
                    unmatched += 1
                    processed_titles.add(normalized)
        except Exception as exc:
            log.error("Unexpected error processing %r: %s", title, exc)
            unmatched_records.append({"title": title, "reason": "fetch_error"})
            processed_titles.add(normalized)
            errors += 1

        # Checkpoint progress after each processed title.
        _write_json_file(MATCHED_FILE, matched_records)
        _write_json_file(UNMATCHED_FILE, unmatched_records)
        _write_transform_input(TRANSFORM_INPUT_FILE, matched_records)

    log.info(
        "Done. matched=%d  unmatched=%d  errors=%d",
        matched, unmatched, errors,
    )
    log.info("Matched output  : %s", MATCHED_FILE)
    log.info("Unmatched output: %s", UNMATCHED_FILE)
    log.info("Transform input : %s", TRANSFORM_INPUT_FILE)
    log.info("Scholar cache   : %s", SCHOLAR_OUTPUT_FILE)
    log.info("scholar_cache_changed=%s", str(cache_changed).lower())
    log.info("Log             : %s", LOG_FILE)
    if cache_changed and matched_records:
        trigger_downstream_sync()
    else:
        log.info("Scholar cache unchanged; downstream sync skipped")

    return cache_changed


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
        help="Process only the first N titles.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Process all collected titles. If omitted and --sample is not set, "
            f"the script defaults to the first {DEFAULT_DEV_SAMPLE} titles."
        ),
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
    if args.all and args.sample is not None:
        parser.error("--all cannot be used with --sample")

    effective_sample = None if args.all else (args.sample if args.sample is not None else DEFAULT_DEV_SAMPLE)
    if effective_sample is None:
        log.info("Running full title set (--all enabled).")
    else:
        log.info("Development mode active: processing first %d titles.", effective_sample)

    run(sample=effective_sample, browser=args.browser)
