#!/usr/bin/env python3
"""
Transform OSTI records from PAGES schema to ELINK schema format.

The PAGES API returns records with fields like:
  - authors (array)
  - sponsor_orgs
  - research_orgs
  - publisher
  - language (string)
  - country_publication (string)
  - journal_volume
  - journal_issue
  - subjects (array)

The ELINK schema expects:
  - persons (array of Person objects)
  - organizations (array of Organization objects)
  - publisher_information
  - languages (array)
  - country_publication_code (ISO code)
  - volume
  - issue
  - keywords (consolidated from keywords + subjects)
"""

import json
import sys
from pathlib import Path
from typing import Any, Optional


# Country name to ISO code mapping
COUNTRY_CODE_LOOKUP = {
    "United States": "US",
    "United Kingdom": "GB",
    "Canada": "CA",
    "Germany": "DE",
    "France": "FR",
    "Japan": "JP",
}


def as_list(value: Any) -> list:
    """Convert value to list, handling None and string inputs."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def non_empty(value: Any) -> Optional[str]:
    """Return value if non-empty string, else None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_author(author_str: str) -> dict:
    """Parse author string into Person dict with first_name and last_name."""
    if not author_str or not isinstance(author_str, str):
        return {"name": str(author_str) if author_str else "", "type": "AUTHOR"}
    
    author_str = author_str.strip()
    parts = author_str.split(",")
    
    if len(parts) >= 2:
        # "Last, First" format
        return {
            "last_name": parts[0].strip(),
            "first_name": parts[1].strip(),
            "type": "AUTHOR"
        }
    elif " " in author_str:
        # "First Last" format
        parts = author_str.rsplit(" ", 1)
        return {
            "first_name": parts[0].strip(),
            "last_name": parts[1].strip() if len(parts) > 1 else "",
            "type": "AUTHOR"
        }
    else:
        return {
            "name": author_str,
            "type": "AUTHOR"
        }


def normalize_pages_to_elink(record: dict) -> dict:
    """Convert a PAGES-format record to ELINK format."""
    r = record.copy()
    
    # Normalize organizations
    orgs_existing = r.get("organizations", []) or []
    orgs_sponsor = [
        {"type": "SPONSOR", "name": name}
        for name in as_list(r.get("sponsor_orgs"))
        if non_empty(name)
    ]
    orgs_research_multi = [
        {"type": "RESEARCHING", "name": name}
        for name in as_list(r.get("research_orgs"))
        if non_empty(name)
    ]
    orgs_research_single = [
        {"type": "RESEARCHING", "name": name}
        for name in as_list(r.get("research_org"))
        if non_empty(name)
    ]
    orgs_contrib_a = [
        {"type": "CONTRIBUTING", "name": name}
        for name in as_list(r.get("contributing_org"))
        if non_empty(name)
    ]
    orgs_contrib_b = [
        {"type": "CONTRIBUTING", "name": name}
        for name in as_list(r.get("contributor_org"))
        if non_empty(name)
    ]
    
    # Combine and deduplicate organizations
    all_orgs = orgs_existing + orgs_sponsor + orgs_research_multi + orgs_research_single + orgs_contrib_a + orgs_contrib_b
    all_orgs = [org for org in all_orgs if org.get("name", "").strip()]
    
    # Deduplicate by type and name
    seen = set()
    unique_orgs = []
    for org in all_orgs:
        key = (org.get("type", "") or "", org.get("name", "") or "")
        if key not in seen:
            seen.add(key)
            unique_orgs.append(org)
    
    if unique_orgs:
        r["organizations"] = unique_orgs
    
    # Normalize persons/authors
    persons_existing = r.get("persons", []) or []
    authors = as_list(r.get("authors"))
    persons_authors = [parse_author(author) for author in authors if author]
    
    all_persons = persons_existing + persons_authors
    all_persons = [p for p in all_persons if p.get("last_name") or p.get("name", "").strip()]
    
    # Deduplicate persons
    seen = set()
    unique_persons = []
    for person in all_persons:
        key = (
            person.get("type", "") or "",
            person.get("first_name", "") or "",
            person.get("last_name", "") or "",
            person.get("name", "") or ""
        )
        if key not in seen:
            seen.add(key)
            unique_persons.append(person)
    
    if unique_persons:
        r["persons"] = unique_persons
    
    # Normalize languages
    if isinstance(r.get("languages"), list):
        pass  # Already a list
    elif r.get("language"):
        r["languages"] = [r["language"]]
    
    # Map country publication
    if not r.get("country_publication_code") and r.get("country_publication"):
        r["country_publication_code"] = COUNTRY_CODE_LOOKUP.get(
            r["country_publication"],
            r["country_publication"]
        )
    
    # Consolidate publisher info
    if not r.get("publisher_information") and r.get("publisher"):
        r["publisher_information"] = r["publisher"]
    
    # Consolidate volume
    if not r.get("volume") and r.get("journal_volume"):
        r["volume"] = r["journal_volume"]
    
    # Consolidate issue
    if not r.get("issue") and r.get("journal_issue"):
        r["issue"] = r["journal_issue"]
    
    # Consolidate date_metadata_added
    if not r.get("date_metadata_added") and r.get("entry_date"):
        r["date_metadata_added"] = r["entry_date"]
    
    # Merge keywords and subjects
    keywords = r.get("keywords", []) or []
    subjects = r.get("subjects", []) or []
    
    if isinstance(keywords, list) and isinstance(subjects, list):
        r["keywords"] = list(dict.fromkeys(keywords + subjects))  # Preserve order, remove duplicates
    elif isinstance(subjects, list):
        r["keywords"] = subjects
    
    # Map url to site_url (ELINK expects site_url)
    if r.get("url") and not r.get("site_url"):
        r["site_url"] = r["url"]
    
    # Remove PAGES-specific fields that have been normalized
    fields_to_remove = [
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
        "source_title",
        "osti_url",
        "url",
        "product_type",
        "payload",
    ]
    
    for field in fields_to_remove:
        r.pop(field, None)
    
    return r


def transform_file(input_path: Path, output_path: Path) -> int:
    """Transform a PAGES-format JSON file to ELINK format."""
    try:
        with open(input_path, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error reading JSON from {input_path}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error reading {input_path}: {e}", file=sys.stderr)
        return 1
    
    # Handle both array and {records: [...]} format
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict) and "records" in data:
        records = data["records"]
    else:
        print(f"Unexpected JSON structure in {input_path}", file=sys.stderr)
        return 1
    
    # Transform records
    transformed_records = []
    for record in records:
        if isinstance(record, dict):
            try:
                transformed = normalize_pages_to_elink(record)
                transformed_records.append(transformed)
            except Exception as e:
                print(f"Error transforming record: {e}", file=sys.stderr)
                return 1
    
    # Output in {records: [...]} format for osti_to_brc transform
    output_data = {"records": transformed_records}
    
    try:
        with open(output_path, "w") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except Exception as e:
        print(f"Error writing to {output_path}: {e}", file=sys.stderr)
        return 1
    
    print(f"Transformed {len(transformed_records)} records", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python pages_to_elink_transform.py <input.json> <output.json>", file=sys.stderr)
        sys.exit(1)
    
    input_file = Path(sys.argv[1])
    output_file = Path(sys.argv[2])
    
    sys.exit(transform_file(input_file, output_file))
