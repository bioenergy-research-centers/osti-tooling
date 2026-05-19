#!/usr/bin/env python3
"""Downstream OSTI sync pipeline implemented in Python.

This script mirrors the former shell workflow:
- Read OSTI IDs from scholar cache JSON.
- Fetch PAGES and E-Link records.
- Normalize records for BRC transformation.
- Emit raw/split JSON artifacts and latest symlinks.
- Run brcschema transform and optional validation.
- Publish outputs and prune old runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fcntl
import requests


PUBLICATION_TYPES = {
    "Journal Article",
    "Book",
    "Technical Report",
    "Accomplishment Report",
    "Patent",
    "Patent Application",
}

COUNTRY_CODE_LOOKUP = {
    "United States": "US",
    "United Kingdom": "GB",
    "Canada": "CA",
    "Germany": "DE",
    "France": "FR",
    "Japan": "JP",
}

MEDIA_ALLOWED_KEYS = {
    "media_id",
    "revision",
    "access_limitations",
    "osti_id",
    "status",
    "added_by",
    "document_page_count",
    "mime_type",
    "media_title",
    "media_location",
    "media_source",
    "date_added",
    "date_updated",
    "date_valid_end",
    "files",
}

MEDIA_FILE_ALLOWED_KEYS = {
    "media_file_id",
    "media_id",
    "checksum",
    "revision",
    "parent_media_file_id",
    "status",
    "added_by",
    "mime_type",
    "media_type",
    "url",
    "url_type",
    "date_file_added",
    "date_file_updated",
    "date_valid_end",
    "document_page_count",
    "duration_seconds",
    "subtitle_tracks",
    "video_tracks",
    "pdf_version",
    "pdfa_part",
    "pdfa_conformance",
    "processing_exceptions",
}

REMOVE_TOP_LEVEL_KEYS = {
    "input_code",
    "dataset_type",
    "details_url",
    "doi_url",
    "fulltext_url",
    "osti_repository",
    "record_category",
    "__source",
}


@dataclass
class Settings:
    repo_dir: Path = Path("/opt/osti/brc-schema")
    state_dir: Path = Path("/opt/osti/state")
    out_dir: Path = Path("/opt/osti/state/runs")
    scholar_out_dir: Path = Path("/opt/osti/scholar_output")
    elink_out_dir: Path = Path("/opt/osti/elink_output")
    brc_out_dir: Path = Path("/opt/osti/brc_output")
    log_dir: Path = Path("/opt/osti/logs")
    workflow_log: Path = Path("/opt/osti/logs/osti_workflow.log")
    lock_file: Path = Path("/opt/osti/state/osti_hourly_sync.lock")
    uv_cache_dir: Path = Path("/opt/osti/state/uv-cache")
    xdg_cache_home: Path = Path("/opt/osti/state/.cache")
    xdg_data_home: Path = Path("/opt/osti/state/.data")
    pystow_home: Path = Path("/opt/osti/state/pystow")

    web_osti_json: Path = Path("/var/www/html/CBI/cbi_osti.json")
    web_brc_json: Path = Path("/var/www/html/CBI/cbi.json")
    web_publications_json: Path = Path("/var/www/html/CBI/cbi_publications.json")

    config_ini: Path = Path("/var/www/OSTI_config.ini")
    elink_api_url: str = "https://www.osti.gov/elink2api/records/"
    pages_api_url: str = "https://www.osti.gov/pages/api/v1/records"
    site_ownership_code: str = "CBI"
    scholar_json: Path = Path("/opt/osti/scholar_output/latest_osti_scholar_records.json")

    validate_brc_output: bool = True
    validation_strict: bool = True
    resume_from_latest: bool = False
    keep_runs: int = 168


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_utc() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def log_line(msg: str, run_log: Path) -> None:
    run_log.parent.mkdir(parents=True, exist_ok=True)
    with run_log.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        env[key] = val
    return env


def load_settings() -> tuple[Settings, str | None]:
    env_file = load_dotenv(Path("/opt/osti/env"))

    def get(name: str, default: str) -> str:
        return os.getenv(name, env_file.get(name, default))

    s = Settings(
        repo_dir=Path(get("REPO_DIR", "/opt/osti/brc-schema")),
        state_dir=Path(get("STATE_DIR", "/opt/osti/state")),
        out_dir=Path(get("OUT_DIR", env_file.get("OUT_DIR", "/opt/osti/state/runs"))),
        scholar_out_dir=Path(get("SCHOLAR_OUT_DIR", "/opt/osti/scholar_output")),
        elink_out_dir=Path(get("ELINK_OUT_DIR", "/opt/osti/elink_output")),
        brc_out_dir=Path(get("BRC_OUT_DIR", "/opt/osti/brc_output")),
        log_dir=Path(get("LOG_DIR", "/opt/osti/logs")),
        workflow_log=Path(get("WORKFLOW_LOG", "/opt/osti/logs/osti_workflow.log")),
        lock_file=Path(get("LOCK_FILE", "/opt/osti/state/osti_hourly_sync.lock")),
        uv_cache_dir=Path(get("UV_CACHE_DIR", "/opt/osti/state/uv-cache")),
        xdg_cache_home=Path(get("XDG_CACHE_HOME", "/opt/osti/state/.cache")),
        xdg_data_home=Path(get("XDG_DATA_HOME", "/opt/osti/state/.data")),
        pystow_home=Path(get("PYSTOW_HOME", "/opt/osti/state/pystow")),
        web_osti_json=Path(get("WEB_OSTI_JSON", "/var/www/html/CBI/cbi_osti.json")),
        web_brc_json=Path(get("WEB_BRC_JSON", "/var/www/html/CBI/cbi.json")),
        web_publications_json=Path(get("WEB_PUBLICATIONS_JSON", "/var/www/html/CBI/cbi_publications.json")),
        config_ini=Path(get("CONFIG_INI", "/var/www/OSTI_config.ini")),
        elink_api_url=get("ELINK_API_URL", "https://www.osti.gov/elink2api/records/"),
        pages_api_url=get("PAGES_API_URL", "https://www.osti.gov/pages/api/v1/records"),
        site_ownership_code=get("SITE_OWNERSHIP_CODE", "CBI"),
        scholar_json=Path(get("SCHOLAR_JSON", "/opt/osti/scholar_output/latest_osti_scholar_records.json")),
        validate_brc_output=parse_bool(get("VALIDATE_BRC_OUTPUT", "1"), True),
        validation_strict=parse_bool(get("VALIDATION_STRICT", "1"), True),
        resume_from_latest=parse_bool(get("RESUME_FROM_LATEST", "0"), False),
        keep_runs=int(get("KEEP_RUNS", "168")),
    )

    token = os.getenv("ELINK_BEARER_TOKEN", env_file.get("ELINK_BEARER_TOKEN"))
    if not token:
        token = os.getenv("OSTI_API_KEY", env_file.get("OSTI_API_KEY"))
    if not token and s.config_ini.exists():
        parser = ConfigParser()
        parser.read(s.config_ini)
        if parser.has_option("DEFAULT", "token"):
            token = parser.get("DEFAULT", "token").strip()
        elif parser.has_option("auth", "token"):
            token = parser.get("auth", "token").strip()
        else:
            for line in s.config_ini.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("token") and "=" in line:
                    token = line.split("=", 1)[1].strip()
                    break

    return s, token


def ensure_dirs(settings: Settings) -> None:
    for path in (
        settings.out_dir,
        settings.scholar_out_dir,
        settings.elink_out_dir,
        settings.brc_out_dir,
        settings.log_dir,
        settings.uv_cache_dir,
        settings.xdg_cache_home,
        settings.xdg_data_home,
        settings.pystow_home,
    ):
        path.mkdir(parents=True, exist_ok=True)


def ensure_env_homes(settings: Settings) -> None:
    home = Path(os.getenv("HOME", ""))
    if not home.exists() or not os.access(home, os.W_OK):
        os.environ["HOME"] = str(settings.state_dir)
    os.environ["UV_CACHE_DIR"] = str(settings.uv_cache_dir)
    os.environ["XDG_CACHE_HOME"] = str(settings.xdg_cache_home)
    os.environ["XDG_DATA_HOME"] = str(settings.xdg_data_home)
    os.environ["PYSTOW_HOME"] = str(settings.pystow_home)


def load_osti_ids(scholar_json: Path) -> list[str]:
    if not scholar_json.exists():
        raise FileNotFoundError(
            f"Scholar JSON not found: {scholar_json}. "
            "Run scholar scraper first: python /opt/osti/osti-tooling/gscholscrape.py"
        )
    data = json.loads(scholar_json.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    ids = {
        str(item.get("osti_id")).strip()
        for item in data
        if isinstance(item, dict) and item.get("osti_id") is not None
    }
    return sorted(i for i in ids if i)


def is_pages_publication(body: Any) -> bool:
    def one(record: Any) -> bool:
        if not isinstance(record, dict):
            return False
        pt = str(record.get("product_type", ""))
        at = str(record.get("@type", ""))
        return pt in PUBLICATION_TYPES or at == "ScholarlyArticle"

    if isinstance(body, list):
        return any(one(r) for r in body)
    return one(body)


def strip_legacy_keys(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if re.fullmatch(r"[A-Z0-9_]+", k):
                continue
            out[k] = strip_legacy_keys(v)
        return out
    if isinstance(node, list):
        return [strip_legacy_keys(v) for v in node]
    return node


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def non_empty(value: Any) -> str | None:
    if isinstance(value, str):
        val = value.strip()
        return val or None
    return None


def parse_author(author: Any) -> dict[str, str]:
    if not isinstance(author, str):
        return {"type": "AUTHOR", "name": str(author or "")}
    author = author.strip()
    if "," in author:
        parts = [p.strip() for p in author.split(",")]
        return {
            "type": "AUTHOR",
            "last_name": parts[0] if parts else "",
            "first_name": ",".join(parts[1:]).strip() if len(parts) > 1 else "",
        }
    return {"type": "AUTHOR", "last_name": author}


def unique_by(items: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = key_fn(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def sanitize_media(record: dict[str, Any]) -> dict[str, Any]:
    media = record.get("media")
    if not isinstance(media, list):
        return record

    new_media: list[dict[str, Any]] = []
    for m in media:
        if not isinstance(m, dict):
            continue
        m = dict(m)
        m.pop("input_code", None)
        m.pop("date_released", None)
        m.pop("workflow_status", None)
        m = {k: v for k, v in m.items() if k in MEDIA_ALLOWED_KEYS}

        files = m.get("files")
        if isinstance(files, list):
            kept = []
            for f in files:
                if isinstance(f, dict):
                    kept.append({k: v for k, v in f.items() if k in MEDIA_FILE_ALLOWED_KEYS})
            m["files"] = kept
        new_media.append(m)

    record["media"] = new_media
    return record


def normalize_pages_to_elink(record: dict[str, Any]) -> dict[str, Any]:
    r = dict(record)

    orgs_existing = r.get("organizations") if isinstance(r.get("organizations"), list) else []
    orgs = list(orgs_existing)

    for field, org_type in (
        ("sponsor_orgs", "SPONSOR"),
        ("research_orgs", "RESEARCHING"),
        ("research_org", "RESEARCHING"),
        ("contributing_org", "CONTRIBUTING"),
        ("contributor_org", "CONTRIBUTING"),
    ):
        for value in as_list(r.get(field)):
            name = non_empty(value)
            if name:
                orgs.append({"type": org_type, "name": name})

    orgs = [o for o in orgs if isinstance(o, dict) and str(o.get("name", "")).strip()]
    orgs = unique_by(orgs, lambda o: (str(o.get("type", "")), str(o.get("name", ""))))
    if orgs:
        r["organizations"] = orgs

    persons_existing = r.get("persons") if isinstance(r.get("persons"), list) else []
    authors = [parse_author(a) for a in as_list(r.get("authors"))]
    persons = persons_existing + authors
    persons = [
        p
        for p in persons
        if isinstance(p, dict) and (p.get("last_name") or str(p.get("name", "")).strip())
    ]
    persons = unique_by(
        persons,
        lambda p: (
            str(p.get("type", "")),
            str(p.get("first_name", "")),
            str(p.get("last_name", "")),
            str(p.get("name", "")),
        ),
    )
    if persons:
        r["persons"] = persons

    if not isinstance(r.get("languages"), list) and non_empty(r.get("language")):
        r["languages"] = [r["language"]]

    if not non_empty(r.get("country_publication_code")) and non_empty(r.get("country_publication")):
        country = str(r["country_publication"])
        r["country_publication_code"] = COUNTRY_CODE_LOOKUP.get(country, country)

    if not non_empty(r.get("publisher_information")) and non_empty(r.get("publisher")):
        r["publisher_information"] = r["publisher"]

    if not non_empty(r.get("volume")) and non_empty(r.get("journal_volume")):
        r["volume"] = r["journal_volume"]

    if not non_empty(r.get("issue")) and non_empty(r.get("journal_issue")):
        r["issue"] = r["journal_issue"]

    if not non_empty(r.get("date_metadata_added")) and non_empty(r.get("entry_date")):
        r["date_metadata_added"] = r["entry_date"]

    keywords = r.get("keywords") if isinstance(r.get("keywords"), list) else []
    subjects = r.get("subjects") if isinstance(r.get("subjects"), list) else []
    if keywords and subjects:
        r["keywords"] = list(dict.fromkeys(keywords + subjects))
    elif not keywords and subjects:
        r["keywords"] = subjects

    for key in (
        "authors",
        "sponsor_orgs",
        "research_org",
        "research_orgs",
        "contributing_org",
        "contributor_org",
        "publisher",
        "journal_volume",
        "journal_issue",
        "country_publication",
        "language",
        "entry_date",
        "subjects",
    ):
        r.pop(key, None)

    return r


def merge_duplicate_osti_ids(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Keep one record per (osti_id, source) to preserve one E-Link and one Pages record.
    ordered = sorted(
        [r for r in records if isinstance(r, dict)],
        key=lambda r: (str(r.get("osti_id", "__no_id__")), str(r.get("__source", "__unknown__"))),
    )
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for rec in ordered:
        ident = (str(rec.get("osti_id", "__no_id__")), str(rec.get("__source", "__unknown__")))
        if ident in seen:
            continue
        seen.add(ident)
        out.append(rec)
    return out


def extract_schema_version(schema_file: Path) -> str:
    if not schema_file.exists():
        return "unknown"
    for line in schema_file.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^version:\s*\"?([^\"]+)\"?\s*$", line.strip())
        if m:
            return m.group(1)
    return "unknown"


def validate_brc(schema_file: Path, brc_json: Path) -> tuple[int, int, str, int]:
    try:
        from linkml.validator import Validator
        from linkml.validator.plugins import JsonschemaValidationPlugin
        from linkml.validator.report import Severity
    except Exception as exc:  # pragma: no cover
        return 0, 0, f"VALIDATION_IMPORT_ERROR {exc}", 2

    try:
        instance = json.loads(brc_json.read_text(encoding="utf-8"))
        validator = Validator(
            schema=str(schema_file),
            validation_plugins=[JsonschemaValidationPlugin(closed=True)],
        )
        report = validator.validate(instance, target_class="DatasetCollection")
        errors = [r for r in report.results if r.severity in (Severity.ERROR, Severity.FATAL)]
        warnings = [r for r in report.results if r.severity == Severity.WARNING]
        lines = [f"VALIDATION_ERRORS {len(errors)}", f"VALIDATION_WARNINGS {len(warnings)}"]
        for r in errors[:20]:
            path = r.path or "-"
            msg = str(r.message).replace("\n", " ")
            lines.append(f"VALIDATION_ERROR path={path} msg={msg}")
        return len(errors), len(warnings), "\n".join(lines), 0
    except Exception as exc:  # pragma: no cover
        return 0, 0, f"VALIDATION_RUNTIME_ERROR {exc}", 3


def publish_file(src: Path, dst: Path, run_log: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
        log_line(f"publish=direct destination={dst}", run_log)
        return
    except Exception:
        pass

    sudo = shutil.which("sudo")
    if sudo:
        cp_proc = subprocess.run([sudo, "-n", "cp", str(src), str(dst)], check=False)
        if cp_proc.returncode == 0:
            subprocess.run([sudo, "-n", "chown", "nobody:nogroup", str(dst)], check=False)
            log_line(f"publish=sudo destination={dst}", run_log)
            return

    log_line(f"WARN: unable to publish {dst} (direct and sudo -n failed)", run_log)


def update_symlink(link_path: Path, target_path: Path, run_log: Path | None = None) -> None:
    try:
        link_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = link_path.with_suffix(link_path.suffix + ".tmp")
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(target_path)
        tmp.replace(link_path)
    except Exception as exc:
        if run_log is not None:
            log_line(f"WARN: unable to update {link_path}: {exc}", run_log)


def prune_old_runs(directory: Path, pattern: str, keep_runs: int) -> None:
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep_runs:]:
        try:
            old.unlink()
        except FileNotFoundError:
            pass


def run() -> int:
    settings, token = load_settings()
    ensure_dirs(settings)
    ensure_env_homes(settings)

    lock_fh = settings.lock_file.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log_line(f"[{now_utc()}] another sync already running; skipping", settings.workflow_log)
        return 0

    run_ts = ts_utc()
    osti_json = settings.elink_out_dir / f"osti_records_{run_ts}.json"
    osti_pubs_json = settings.elink_out_dir / f"osti_publications_{run_ts}.json"
    osti_data_json = settings.elink_out_dir / f"osti_datasets_{run_ts}.json"
    brc_json = settings.brc_out_dir / f"brc_datasets_{run_ts}.json"
    run_log = settings.workflow_log

    log_line(f"[{now_utc()}] sync start", run_log)
    log_line(f"repo={settings.repo_dir}", run_log)
    log_line(f"state_dir={settings.state_dir}", run_log)

    if not token:
        log_line(f"ERROR: ELINK_BEARER_TOKEN is not set and no token was found in {settings.config_ini}", run_log)
        return 1

    try:
        osti_ids = load_osti_ids(settings.scholar_json)
    except Exception as exc:
        log_line(f"ERROR: {exc}", run_log)
        return 1

    log_line(f"osti_ids_loaded={len(osti_ids)}", run_log)

    records: list[dict[str, Any]] = []
    if settings.resume_from_latest:
        latest = settings.elink_out_dir / "latest_osti_records.json"
        if not latest.exists():
            log_line(f"ERROR: RESUME_FROM_LATEST=1 but checkpoint is missing: {latest}", run_log)
            return 1
        shutil.copy2(latest, osti_json)
        data = json.loads(osti_json.read_text(encoding="utf-8"))
        record_count = len(data.get("records", [])) if isinstance(data, dict) else 0
        log_line(f"resume_mode=true source={latest} record_count={record_count}", run_log)
    else:
        session = requests.Session()
        elink_success = 0
        elink_failed = 0
        elink_skipped = 0
        pages_success = 0
        pages_failed = 0

        for osti_id in osti_ids:
            pages_publication = False

            # PAGES API first.
            try:
                pages_resp = session.get(f"{settings.pages_api_url}/{osti_id}", timeout=30)
                pages_resp.raise_for_status()
                body = pages_resp.json()
                if isinstance(body, list):
                    records.extend([dict(r, __source="pages") for r in body if isinstance(r, dict)])
                elif isinstance(body, dict):
                    records.append(dict(body, __source="pages"))
                pages_publication = is_pages_publication(body)
                pages_success += 1
                log_line(f"pages osti_id={osti_id} status=ok", run_log)
            except Exception:
                pages_failed += 1
                log_line(f"WARN: pages osti_id={osti_id} status=failed", run_log)

            # E-Link API only if pages did not classify as publication.
            if pages_publication:
                elink_skipped += 1
                log_line(f"elink osti_id={osti_id} status=skipped_pages_publication", run_log)
            else:
                try:
                    elink_resp = session.get(
                        f"{settings.elink_api_url}{osti_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=30,
                    )
                    elink_resp.raise_for_status()
                    body = elink_resp.json()
                    if isinstance(body, list):
                        records.extend([dict(r, __source="elink") for r in body if isinstance(r, dict)])
                    elif isinstance(body, dict):
                        records.append(dict(body, __source="elink"))
                    elink_success += 1
                    log_line(f"elink osti_id={osti_id} status=ok", run_log)
                except Exception:
                    elink_failed += 1
                    log_line(f"WARN: elink osti_id={osti_id} status=failed", run_log)

            time.sleep(0.1)

        log_line(
            f"elink_success={elink_success} elink_failed={elink_failed} elink_skipped={elink_skipped}",
            run_log,
        )
        log_line(f"pages_success={pages_success} pages_failed={pages_failed}", run_log)
        log_line(f"id_count={elink_success}", run_log)

        merged = merge_duplicate_osti_ids(records)
        cleaned_records: list[dict[str, Any]] = []
        for rec in merged:
            rec = strip_legacy_keys(rec)
            rec = normalize_pages_to_elink(rec)
            if not str(rec.get("site_ownership_code", "")).strip() and settings.site_ownership_code:
                rec["site_ownership_code"] = settings.site_ownership_code
            for key in REMOVE_TOP_LEVEL_KEYS:
                rec.pop(key, None)
            rec = sanitize_media(rec)
            cleaned_records.append(rec)

        osti_json.write_text(json.dumps({"records": cleaned_records}, indent=2) + "\n", encoding="utf-8")
        record_count = len(cleaned_records)
        log_line(f"elink_record_count={record_count}", run_log)

    update_symlink(settings.elink_out_dir / "latest_osti_records.json", osti_json)

    raw = json.loads(osti_json.read_text(encoding="utf-8"))
    all_records = raw.get("records", []) if isinstance(raw, dict) else []
    pubs = [r for r in all_records if isinstance(r, dict) and r.get("product_type") in PUBLICATION_TYPES]
    data = [r for r in all_records if isinstance(r, dict) and r.get("product_type") == "Dataset"]

    osti_pubs_json.write_text(json.dumps({"records": pubs}, indent=2) + "\n", encoding="utf-8")
    log_line(f"elink_publications_count={len(pubs)}", run_log)
    update_symlink(settings.elink_out_dir / "latest_osti_publications.json", osti_pubs_json)

    osti_data_json.write_text(json.dumps({"records": data}, indent=2) + "\n", encoding="utf-8")
    log_line(f"elink_datasets_count={len(data)}", run_log)
    update_symlink(settings.elink_out_dir / "latest_osti_datasets.json", osti_data_json)

    merged_records = pubs + data
    merged_path = Path(f"{osti_json}.merged")
    merged_path.write_text(json.dumps({"records": merged_records}, indent=2) + "\n", encoding="utf-8")

    if shutil.which("uv") is None:
        log_line("ERROR: uv is required to run brcschema transform", run_log)
        return 1

    if merged_records:
        cmd = [
            "uv",
            "run",
            "brcschema",
            "transform",
            "-T",
            "osti_to_brc",
            "-o",
            str(brc_json),
            str(merged_path),
        ]
        proc = subprocess.run(cmd, cwd=settings.repo_dir, check=False)
        if proc.returncode != 0:
            log_line(f"ERROR: brcschema transform failed with return code {proc.returncode}", run_log)
            return 1
        log_line("brc_merged_records_generated=true", run_log)
    else:
        brc_json.write_text('{"records": []}\n', encoding="utf-8")
        log_line("brc_merged_records_generated=false (empty merged list)", run_log)

    # Post-process BRC output.
    brc_schema_version = extract_schema_version(settings.repo_dir / "src/brc_schema/schema/brc_schema.yaml")
    brc_payload = json.loads(brc_json.read_text(encoding="utf-8"))
    if isinstance(brc_payload, dict):
        brc_payload.pop("@type", None)
        datasets = brc_payload.get("datasets")
        if isinstance(datasets, list):
            for ds in datasets:
                if not isinstance(ds, dict):
                    continue
                durl = ds.get("dataset_url")
                if isinstance(durl, dict):
                    ds["dataset_url"] = durl.get("href")
                if not str(ds.get("brc", "")).strip() and settings.site_ownership_code:
                    ds["brc"] = settings.site_ownership_code
        brc_payload = {"schema_version": brc_schema_version} | brc_payload

    brc_json.write_text(json.dumps(brc_payload, indent=2) + "\n", encoding="utf-8")
    log_line(f"brc_schema_version={brc_schema_version}", run_log)

    if settings.validate_brc_output:
        errors, warnings, details, rc = validate_brc(
            settings.repo_dir / "src/brc_schema/schema/brc_schema.yaml",
            brc_json,
        )
        for line in details.splitlines():
            log_line(line, run_log)
        if rc != 0:
            msg = f"BRC validation execution failed (rc={rc})"
            if settings.validation_strict:
                log_line(f"ERROR: {msg}", run_log)
                return 1
            log_line(f"WARN: {msg}", run_log)
        elif errors > 0:
            msg = f"BRC validation failed: errors={errors}"
            if settings.validation_strict:
                log_line(f"ERROR: {msg}", run_log)
                return 1
            log_line(f"WARN: {msg}", run_log)
        else:
            log_line("brc_validation_status=pass", run_log)
    else:
        log_line("brc_validation_status=skipped", run_log)

    update_symlink(settings.brc_out_dir / "latest_brc_datasets.json", brc_json)

    update_symlink(Path("/opt/osti/latest_osti_records.json"), settings.elink_out_dir / "latest_osti_records.json", run_log)
    update_symlink(Path("/opt/osti/latest_osti_publications.json"), settings.elink_out_dir / "latest_osti_publications.json", run_log)
    update_symlink(Path("/opt/osti/latest_osti_datasets.json"), settings.elink_out_dir / "latest_osti_datasets.json", run_log)
    update_symlink(Path("/opt/osti/latest_brc_datasets.json"), settings.brc_out_dir / "latest_brc_datasets.json", run_log)

    publish_file(osti_json, settings.web_osti_json, run_log)
    publish_file(brc_json, settings.web_brc_json, run_log)

    log_line(
        f"[{now_utc()}] sync success (records={len(all_records)} publications={len(pubs)} datasets={len(data)})",
        run_log,
    )

    prune_old_runs(settings.elink_out_dir, "osti_records_*.json", settings.keep_runs)
    prune_old_runs(settings.elink_out_dir, "osti_publications_*.json", settings.keep_runs)
    prune_old_runs(settings.elink_out_dir, "osti_datasets_*.json", settings.keep_runs)
    prune_old_runs(settings.brc_out_dir, "brc_datasets_*.json", settings.keep_runs)

    for merged_file in settings.elink_out_dir.glob("osti_records_*.json.merged"):
        try:
            merged_file.unlink()
        except FileNotFoundError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(run())
