#!/usr/bin/env bash
set -euo pipefail

# OSTI Hourly Sync - Refactored for shared machine paths
# Pulls CBI records from OSTI E-Link API, filters into publications and datasets,
# transforms to BRC format, and publishes to web locations.

# Shared path defaults (can be overridden via environment or /opt/osti/env)
REPO_DIR="${REPO_DIR:-/opt/osti/brc-schema}"
STATE_DIR="${STATE_DIR:-/opt/osti/state}"
OUT_DIR="${OUT_DIR:-$STATE_DIR/runs}"
LOG_DIR="${LOG_DIR:-/opt/osti/logs}"
LOCK_FILE="${LOCK_FILE:-$STATE_DIR/osti_hourly_sync.lock}"

# Web publishing destinations (can be customized per host)
WEB_OSTI_JSON="${WEB_OSTI_JSON:-/var/www/html/CBI/cbi_osti.json}"
WEB_BRC_JSON="${WEB_BRC_JSON:-/var/www/html/CBI/cbi.json}"
WEB_PUBLICATIONS_JSON="${WEB_PUBLICATIONS_JSON:-/var/www/html/CBI/cbi_publications.json}"

# Configuration file location
CONFIG_INI="${CONFIG_INI:-/var/www/OSTI_config.ini}"

# E-Link API settings
# ELINK_API_URL="${ELINK_API_URL:-https://www.osti.gov/elink2api/records}"
ELINK_API_URL="${ELINK_API_URL:-https://www.osti.gov/elink2api/records/}"
PAGES_API_URL="${PAGES_API_URL:-https://www.osti.gov/pages/api/v1/records}"
SITE_OWNERSHIP_CODE="${SITE_OWNERSHIP_CODE:-CBI}"

# CSV file containing OSTI IDs to fetch
OSTI_MATCHED_CSV="${OSTI_MATCHED_CSV:-/opt/osti/osti_matched.csv}"

# Generated ID file fallback
GENERATED_ID_FILE="${GENERATED_ID_FILE:-$OUT_DIR/cbi_ids.txt}"

# Load environment overrides if present
if [[ -f "/opt/osti/env" ]]; then
  # shellcheck disable=SC1091
  source "/opt/osti/env"
fi


mkdir -p "$OUT_DIR" "$LOG_DIR"

exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] another sync already running; skipping" >> "$LOG_DIR/osti_hourly_sync.log"
  exit 0
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OSTI_JSON="$OUT_DIR/osti_records_${TS}.json"
OSTI_PUBS_JSON="$OUT_DIR/osti_publications_${TS}.json"
OSTI_DATA_JSON="$OUT_DIR/osti_datasets_${TS}.json"
BRC_JSON="$OUT_DIR/brc_datasets_${TS}.json"
RUN_LOG="$LOG_DIR/osti_hourly_sync_${TS}.log"

cd "$REPO_DIR"

{
  if [[ -e "$REPO_DIR/cbi_ids.txt" ]]; then
    if [[ -w "$REPO_DIR/cbi_ids.txt" ]]; then
      ID_FILE_TARGET="$REPO_DIR/cbi_ids.txt"
    else
      ID_FILE_TARGET="$GENERATED_ID_FILE"
      echo "WARN: $REPO_DIR/cbi_ids.txt is not writable; using $ID_FILE_TARGET instead"
    fi
  else
    if [[ -w "$REPO_DIR" ]]; then
      ID_FILE_TARGET="$REPO_DIR/cbi_ids.txt"
    else
      ID_FILE_TARGET="$GENERATED_ID_FILE"
      echo "WARN: $REPO_DIR is not writable; using $ID_FILE_TARGET instead"
    fi
  fi

  ID_FILE="$ID_FILE_TARGET"

  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] sync start"
  echo "repo=$REPO_DIR"
  echo "state_dir=$STATE_DIR"
  echo "id_file=$ID_FILE"

  if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl is required to fetch CBI OSTI IDs"
    exit 1
  fi

  if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq is required to extract OSTI IDs"
    exit 1
  fi

  if [[ -z "${ELINK_BEARER_TOKEN:-}" && -n "${OSTI_API_KEY:-}" ]]; then
    ELINK_BEARER_TOKEN="$OSTI_API_KEY"
  fi

  if [[ -z "${ELINK_BEARER_TOKEN:-}" && -f "$CONFIG_INI" ]]; then
    ELINK_BEARER_TOKEN="$(awk -F'=' '/^token[[:space:]]*=/{gsub(/[[:space:]]/, "", $2); print $2}' "$CONFIG_INI")"
  fi

  if [[ -z "${ELINK_BEARER_TOKEN:-}" ]]; then
    echo "ERROR: ELINK_BEARER_TOKEN is not set and no token was found in $CONFIG_INI"
    exit 1
  fi

  echo $ELINK_BEARER_TOKEN

  TMP_IDS="$(mktemp)"
  TMP_HEADERS="$(mktemp)"
  TMP_BODY="$(mktemp)"
  TMP_PAGES="$(mktemp)"
  trap 'rm -f "$TMP_IDS" "$TMP_HEADERS" "$TMP_BODY" "$TMP_PAGES"' EXIT

  # Load OSTI IDs from CSV (column 2, skip header)
  if [[ ! -f "$OSTI_MATCHED_CSV" ]]; then
    echo "ERROR: OSTI_MATCHED_CSV not found: $OSTI_MATCHED_CSV"
    exit 1
  fi
  mapfile -t OSTI_IDS < <(awk -F'|' 'NR>1 {print $2}' "$OSTI_MATCHED_CSV")
  echo "osti_ids_loaded=${#OSTI_IDS[@]}"

  echo "========================================================================================================================="
  echo "--Fetching E-Link and Pages APIs for ${#OSTI_IDS[@]} OSTI IDs ---------------------------------------------------------"
  echo "========================================================================================================================="

  elink_success=0
  elink_failed=0
  pages_success=0
  pages_failed=0

  for OSTI_ID in "${OSTI_IDS[@]}"; do
    # E-Link API (single record returns array per docs)
    if curl -fsS \
      -H "Authorization: Bearer $ELINK_BEARER_TOKEN" \
      -o "$TMP_BODY" \
      "${ELINK_API_URL}${OSTI_ID}" 2>/dev/null; then
      jq -c '.[] | select(type == "object") | . + {__source: "elink"}' "$TMP_BODY" >> "$TMP_PAGES"
      echo "$OSTI_ID" >> "$TMP_IDS"
      elink_success=$((elink_success + 1))
      echo "elink osti_id=$OSTI_ID status=ok"
    else
      elink_failed=$((elink_failed + 1))
      echo "WARN: elink osti_id=$OSTI_ID status=failed"
    fi

    # Pages API (handle both array and object responses)
    if curl -fsS \
      -o "$TMP_BODY" \
      "${PAGES_API_URL}/${OSTI_ID}" 2>/dev/null; then
      if jq -e 'type == "array"' "$TMP_BODY" >/dev/null 2>&1; then
        jq -c '.[] | select(type == "object") | . + {__source: "pages"}' "$TMP_BODY" >> "$TMP_PAGES"
      else
        jq -c 'select(type == "object") | . + {__source: "pages"}' "$TMP_BODY" >> "$TMP_PAGES"
      fi
      pages_success=$((pages_success + 1))
      echo "pages osti_id=$OSTI_ID status=ok"
    else
      pages_failed=$((pages_failed + 1))
      echo "WARN: pages osti_id=$OSTI_ID status=failed"
    fi

    sleep 0.1
  done

  # echo $TMP_PAGES
  # echo "$TMP_PAGES"

  echo "elink_success=$elink_success elink_failed=$elink_failed"
  echo "pages_success=$pages_success pages_failed=$pages_failed"

  sort -n -u "$TMP_IDS" > "$ID_FILE"
  id_count="$(wc -l < "$ID_FILE")"
  echo "id_count=$id_count"

  # Sanitize OSTI records: remove incompatible keys and normalize structure
  jq -s '
    def as_list:
      if . == null then
        []
      elif type == "array" then
        .
      else
        [.] 
      end;

    def non_empty:
      select(. != null and . != "");

    def keep_keys($allowed):
      with_entries(select(.key as $k | $allowed | index($k)));

    def strip_legacy_keys:
      walk(
        if type == "object" then
          with_entries(select(.key | test("^[A-Z0-9_]+$") | not))
        else
          .
        end
      );

    def sanitize_media:
      if (.media | type) == "array" then
        .media |= map(
          .
          | del(.input_code, .date_released, .workflow_status)
          | keep_keys([
              "media_id", "revision", "access_limitations", "osti_id", "status",
              "added_by", "document_page_count", "mime_type", "media_title",
              "media_location", "media_source", "date_added", "date_updated",
              "date_valid_end", "files"
            ])
          | if (.files | type) == "array" then
              .files |= map(
                keep_keys([
                  "media_file_id", "media_id", "checksum", "revision", "parent_media_file_id",
                  "status", "added_by", "mime_type", "media_type", "url", "url_type",
                  "date_file_added", "date_file_updated", "date_valid_end", "document_page_count",
                  "duration_seconds", "subtitle_tracks", "video_tracks",
                  "pdf_version", "pdfa_part", "pdfa_conformance", "processing_exceptions"
                ])
              )
            else
              .
            end
        )
      else
        .
      end;

    def parse_author:
      if (. // "") | contains(",") then
        (split(",") | {
          type: "AUTHOR",
          last_name: (.[0] | gsub("^\\s+|\\s+$"; "")),
          first_name: ((.[1:] | join(",")) | gsub("^\\s+|\\s+$"; ""))
        })
      else
        {
          type: "AUTHOR",
          last_name: (. // "" | gsub("^\\s+|\\s+$"; ""))
        }
      end;

    # Keep a small country lookup ready. Only map when known; otherwise leave source field untouched.
    def country_code_lookup:
      {
        "United States": "US",
        "United Kingdom": "GB",
        "Canada": "CA",
        "Germany": "DE",
        "France": "FR",
        "Japan": "JP"
      };

    def normalize_pages_to_elink:
      . as $r
      | ($r.organizations // []) as $orgs_existing
      | (($r.sponsor_orgs | as_list) | map(non_empty | {type: "SPONSOR", name: .})) as $orgs_sponsor
      | (($r.research_orgs | as_list) | map(non_empty | {type: "RESEARCHING", name: .})) as $orgs_research_multi
      | (($r.research_org | as_list) | map(non_empty | {type: "RESEARCHING", name: .})) as $orgs_research_single
      | (($r.contributing_org | as_list) | map(non_empty | {type: "CONTRIBUTING", name: .})) as $orgs_contrib_a
      | (($r.contributor_org | as_list) | map(non_empty | {type: "CONTRIBUTING", name: .})) as $orgs_contrib_b
      | (($orgs_existing + $orgs_sponsor + $orgs_research_multi + $orgs_research_single + $orgs_contrib_a + $orgs_contrib_b)
          | map(select((.name // "") != ""))
          | unique_by((.type // "") + "|" + (.name // ""))) as $orgs_all
      | ($r.persons // []) as $persons_existing
      | (($r.authors | as_list) | map(parse_author)) as $persons_authors
      | (($persons_existing + $persons_authors)
          | map(select((.last_name // .name // "") != ""))
          | unique_by((.type // "") + "|" + (.first_name // "") + "|" + (.last_name // "") + "|" + (.name // ""))) as $persons_all
      | (
          if (($r.languages | type) != "array") and (($r.language // "") != "") then
            ($r | .languages = [(.language)])
          else
            $r
          end
        )
      | (
          if (($r.country_publication_code // "") == "") and (($r.country_publication // "") != "") then
            .country_publication_code = (country_code_lookup[.country_publication] // .country_publication_code)
          else
            .
          end
        )
      | (
          if (($r.publisher_information // "") == "") and (($r.publisher // "") != "") then
            .publisher_information = .publisher
          else
            .
          end
        )
      | (
          if (($r.volume // "") == "") and (($r.journal_volume // "") != "") then
            .volume = .journal_volume
          else
            .
          end
        )
      | (
          if (($r.issue // "") == "") and (($r.journal_issue // "") != "") then
            .issue = .journal_issue
          else
            .
          end
        )
      | (
          if (($r.date_metadata_added // "") == "") and (($r.entry_date // "") != "") then
            .date_metadata_added = .entry_date
          else
            .
          end
        )
      | (
          if (($r.keywords | type) == "array") and (($r.subjects | type) == "array") then
            .keywords = ((.keywords + .subjects) | unique)
          elif (($r.keywords | type) != "array") and (($r.subjects | type) == "array") then
            .keywords = .subjects
          else
            .
          end
        )
      | (
          if ($orgs_all | length) > 0 then
            .organizations = $orgs_all
          else
            .
          end
        )
      | (
          if ($persons_all | length) > 0 then
            .persons = $persons_all
          else
            .
          end
        )
      | del(
          .authors,
          .sponsor_orgs,
          .research_org,
          .research_orgs,
          .contributing_org,
          .contributor_org,
          .publisher,
          .journal_volume,
          .journal_issue,
          .country_publication,
          .language,
          .entry_date,
          .subjects
        );

  # Keep duplicate OSTI IDs as separate records by API source.
  # This preserves one E-Link and one Pages record when both exist.
  def merge_duplicate_osti_ids:
  map(select(type == "object"))
  | sort_by([((.osti_id // "__no_id__") | tostring), (.__source // "__unknown__")])
  | group_by(((.osti_id // "__no_id__") | tostring) + "|" + (.__source // "__unknown__"))
  | map(.[0]);

    {
        records: [
          (. | merge_duplicate_osti_ids)[]
          | strip_legacy_keys
          | normalize_pages_to_elink
          | del(
          .input_code,
          .dataset_type,
          .details_url,
          .doi_url,
          .fulltext_url,
          .osti_repository,
          .record_category,
          .__source
          )
          | sanitize_media
        ]
      }
  ' "$TMP_PAGES" > "$OSTI_JSON"
  record_count="$(jq '.records | length' "$OSTI_JSON")"
  echo "elink_record_count=$record_count"

  # Split records into publications and datasets based on product_type for counting
  # Publications: Journal Article, Book, Technical Report, Accomplishment Report, Patent, Patent Application
  # Datasets: Dataset
  jq '{records: [.records[] | select(.product_type == "Journal Article" or .product_type == "Book" or .product_type == "Technical Report" or .product_type == "Accomplishment Report" or .product_type == "Patent" or .product_type == "Patent Application")]}' "$OSTI_JSON" > "$OSTI_PUBS_JSON"
  pubs_count="$(jq '.records | length' "$OSTI_PUBS_JSON")"
  echo "elink_publications_count=$pubs_count"

  jq '{records: [.records[] | select(.product_type == "Dataset")]}' "$OSTI_JSON" > "$OSTI_DATA_JSON"
  data_count="$(jq '.records | length' "$OSTI_DATA_JSON")"
  echo "elink_datasets_count=$data_count"

  # Merge publications and datasets for unified BRC transform
  jq -s '{records: ([.[0].records[], .[1].records[]])}' "$OSTI_PUBS_JSON" "$OSTI_DATA_JSON" > "$OSTI_JSON.merged"

  # Verify problematic Pages-only keys were normalized away.
  contributing_org_count="$(jq '[.records[] | select(any(..; type == "object" and (has("contributing_org") or has("contributor_org"))))] | length' "$OSTI_JSON.merged")"
  echo "normalized_contributing_org_records=$contributing_org_count"

  if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv is required to run brcschema transform"
    exit 1
  fi

  # Transform merged publications and datasets to BRC format
  merged_count=$((pubs_count + data_count))
  if [[ "$merged_count" -gt 0 ]]; then
    uv run brcschema transform \
      -T osti_to_brc \
      -o "$BRC_JSON" \
      "$OSTI_JSON.merged"
    echo "brc_merged_records_generated=true"
  else
    echo "{\"records\": []}" > "$BRC_JSON"
    echo "brc_merged_records_generated=false (empty merged list)"
  fi

  # Inject schema_version at the top of merged BRC JSON output
  BRC_SCHEMA_VERSION="$(awk -F'"' '/^version:/{print $2; exit}' \
    "$REPO_DIR/src/brc_schema/schema/brc_schema.yaml")"

  jq --arg v "$BRC_SCHEMA_VERSION" '{"schema_version": $v} + .' "$BRC_JSON" \
    > "${BRC_JSON}.tmp" && mv "${BRC_JSON}.tmp" "$BRC_JSON"

  echo "brc_schema_version=$BRC_SCHEMA_VERSION"

  # Create symlink to latest merged BRC file
  ln -sfn "$BRC_JSON" "$OUT_DIR/latest_brc_datasets.json"

  # Publish files to web locations
  publish_file() {
    src="$1"
    dst="$2"

    if cp "$src" "$dst" 2>/dev/null; then
      echo "publish=direct destination=$dst"
      return 0
    fi

    if command -v sudo >/dev/null 2>&1; then
      if sudo -n cp "$src" "$dst" 2>/dev/null && sudo -n chown nobody:nogroup "$dst" 2>/dev/null; then
        echo "publish=sudo destination=$dst"
        return 0
      fi
    fi

    echo "WARN: unable to publish $dst (direct and sudo -n failed)"
    return 1
  }

  # Publish original OSTI records (all records)
  publish_file "$OSTI_JSON" "$WEB_OSTI_JSON" || true

  # Publish merged BRC records (publications and datasets)
  publish_file "$BRC_JSON" "$WEB_BRC_JSON" || true

  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] sync success (records=$record_count publications=$pubs_count datasets=$data_count)"
} >> "$RUN_LOG" 2>&1

# Retention: keep the most recent 168 runs (7 days hourly)
KEEP_RUNS="${KEEP_RUNS:-168}"
ls -1t "$OUT_DIR"/osti_records_*.json 2>/dev/null | tail -n +$((KEEP_RUNS + 1)) | xargs -r rm -f
ls -1t "$OUT_DIR"/osti_publications_*.json 2>/dev/null | tail -n +$((KEEP_RUNS + 1)) | xargs -r rm -f
ls -1t "$OUT_DIR"/osti_datasets_*.json 2>/dev/null | tail -n +$((KEEP_RUNS + 1)) | xargs -r rm -f
ls -1t "$OUT_DIR"/brc_datasets_*.json 2>/dev/null | tail -n +$((KEEP_RUNS + 1)) | xargs -r rm -f
rm -f "$OUT_DIR"/osti_records_*.json.merged 2>/dev/null

# Symlink to latest log
ln -sfn "$RUN_LOG" "$LOG_DIR/latest_osti_hourly_sync.log"
