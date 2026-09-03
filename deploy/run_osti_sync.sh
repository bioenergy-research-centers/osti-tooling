#!/usr/bin/env bash

set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true

# This script lives in the same repo it git-pulls below, and bash reads scripts
# incrementally, so run from an immutable snapshot instead of the tracked file.
if [[ -z ${OSTI_SYNC_SNAPSHOT:-} ]]; then
  OSTI_SYNC_SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  OSTI_SYNC_SNAPSHOT=$(mktemp -t osti-sync-XXXXXX.sh)
  export OSTI_SYNC_SCRIPT_DIR OSTI_SYNC_SNAPSHOT
  cat -- "${BASH_SOURCE[0]}" > "$OSTI_SYNC_SNAPSHOT"
  exec bash "$OSTI_SYNC_SNAPSHOT" "$@"
fi
trap 'rm -f -- "$OSTI_SYNC_SNAPSHOT"' EXIT

SCRIPT_DIR=$OSTI_SYNC_SCRIPT_DIR
ENV_FILE=${OSTI_SYNC_ENV_FILE:-"$SCRIPT_DIR/osti-sync.env"}

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing environment file: $ENV_FILE" >&2
  echo "Copy osti-sync.env.example to osti-sync.env and set machine-specific values." >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

export GIT_TERMINAL_PROMPT=0

: "${OSTI_ROOT:?OSTI_ROOT must be set}"
: "${OSTI_TOOLING_DIR:?OSTI_TOOLING_DIR must be set}"
: "${BRC_SCHEMA_DIR:?BRC_SCHEMA_DIR must be set}"
: "${BRC_DATA_FEEDS_DIR:?BRC_DATA_FEEDS_DIR must be set}"
: "${STATE_DIR:?STATE_DIR must be set}"
: "${OUT_DIR:?OUT_DIR must be set}"
: "${ELINK_OUT_DIR:?ELINK_OUT_DIR must be set}"
: "${BRC_OUT_DIR:?BRC_OUT_DIR must be set}"
: "${LOG_DIR:?LOG_DIR must be set}"
: "${WORKFLOW_LOG:?WORKFLOW_LOG must be set}"
: "${LOCK_FILE:?LOCK_FILE must be set}"
: "${SCHOLAR_OUTPUT_DIR:?SCHOLAR_OUTPUT_DIR must be set}"
: "${SCHOLAR_OUTPUT_FILE:?SCHOLAR_OUTPUT_FILE must be set}"
: "${UV_CACHE_DIR:?UV_CACHE_DIR must be set}"
: "${XDG_CACHE_HOME:?XDG_CACHE_HOME must be set}"
: "${XDG_DATA_HOME:?XDG_DATA_HOME must be set}"
: "${PYSTOW_HOME:?PYSTOW_HOME must be set}"
: "${WEB_BRC_JSON:?WEB_BRC_JSON must be set}"
: "${WEB_OSTI_JSON:?WEB_OSTI_JSON must be set}"
: "${WEB_PUBLICATIONS_JSON:?WEB_PUBLICATIONS_JSON must be set}"
: "${SCRAPER_DEDUPE_JSON:?SCRAPER_DEDUPE_JSON must be set}"
: "${ELINK_BEARER_TOKEN:?ELINK_BEARER_TOKEN must be set}"

RUN_TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
RUN_DIR="$OUT_DIR/$RUN_TIMESTAMP"
TMP_DIR="$STATE_DIR/tmp"
PUBLISHED_DIR=$(dirname "$WEB_BRC_JSON")
OSTI_PUBLISHED_DIR=$(dirname "$WEB_OSTI_JSON")
PUBLICATIONS_PUBLISHED_DIR=$(dirname "$WEB_PUBLICATIONS_JSON")

mkdir -p \
  "$STATE_DIR" \
  "$OUT_DIR" \
  "$ELINK_OUT_DIR" \
  "$BRC_OUT_DIR" \
  "$LOG_DIR" \
  "$SCHOLAR_OUTPUT_DIR" \
  "$UV_CACHE_DIR" \
  "$XDG_CACHE_HOME" \
  "$XDG_DATA_HOME" \
  "$PYSTOW_HOME" \
  "$TMP_DIR" \
  "$PUBLISHED_DIR" \
  "$OSTI_PUBLISHED_DIR" \
  "$PUBLICATIONS_PUBLISHED_DIR"

touch "$WORKFLOW_LOG"
exec > >(tee -a "$WORKFLOW_LOG") 2>&1

log() {
  printf '[%s] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*"
}

on_error() {
  local line_no=$1
  local exit_code=$2
  log "ERROR line=$line_no exit_code=$exit_code"
  exit "$exit_code"
}
trap 'on_error ${LINENO} $?' ERR

# Distinct from LOCK_FILE, which downstream_sync.py acquires for itself.
RUNNER_LOCK_FILE=${RUNNER_LOCK_FILE:-"${LOCK_FILE}.runner"}
exec 9>"$RUNNER_LOCK_FILE"
if ! flock -n 9; then
  log "Another OSTI sync run is already active; exiting."
  exit 0
fi

require_command() {
  local command_name=$1
  if ! command -v "$command_name" >/dev/null 2>&1; then
    log "Required command not found: $command_name"
    exit 1
  fi
}

git_refresh_repo() {
  local repo_dir=$1
  local branch_name
  log "Refreshing repo $repo_dir"
  git -C "$repo_dir" fetch --all --prune

  branch_name=$(git -C "$repo_dir" symbolic-ref --quiet --short HEAD)
  if git -C "$repo_dir" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1; then
    git -C "$repo_dir" pull --ff-only
    return 0
  fi

  if git -C "$repo_dir" show-ref --verify --quiet "refs/remotes/origin/$branch_name"; then
    git -C "$repo_dir" branch --set-upstream-to "origin/$branch_name" "$branch_name"
    git -C "$repo_dir" pull --ff-only
    return 0
  fi

  log "No upstream branch configured for $repo_dir and origin/$branch_name does not exist"
  exit 1
}

stage_current_feed_inputs() {
  local current_feed="$BRC_DATA_FEEDS_DIR/cbi.json"
  mkdir -p "$(dirname "$SCRAPER_DEDUPE_JSON")"

  if [[ -f "$current_feed" ]]; then
    cp "$current_feed" "$SCRAPER_DEDUPE_JSON"
    cp "$current_feed" "$WEB_BRC_JSON"
    log "Staged current cbi.json for scraper dedupe at $SCRAPER_DEDUPE_JSON"
    log "Seeded canonical published CBI feed at $WEB_BRC_JSON"
  else
    printf '[]\n' > "$SCRAPER_DEDUPE_JSON"
    printf '[]\n' > "$WEB_BRC_JSON"
    log "No existing cbi.json found in brc_data_feeds; seeded empty dedupe file at $SCRAPER_DEDUPE_JSON"
    log "No existing canonical CBI feed found; seeded empty file at $WEB_BRC_JSON"
  fi
}

copy_if_present() {
  local source_file=$1
  local target_file=$2

  if [[ -f "$source_file" ]]; then
    mkdir -p "$(dirname "$target_file")"
    cp "$source_file" "$target_file"
    log "Copied $source_file -> $target_file"
  fi
}

publish_outputs() {
  if [[ ! -f "$WEB_BRC_JSON" ]]; then
    log "Expected generated feed missing: $WEB_BRC_JSON"
    exit 1
  fi

  log "Published canonical CBI feed to $WEB_BRC_JSON"
}

push_cbi_if_changed() {
  local target_repo="$BRC_DATA_FEEDS_DIR"
  local target_file="$target_repo/cbi.json"
  local branch_name

  cp "$WEB_BRC_JSON" "$target_file"
  log "Copied canonical CBI feed into brc_data_feeds checkout"

  git -C "$target_repo" add cbi.json
  if git -C "$target_repo" diff --cached --quiet; then
    log "No cbi.json changes detected; skipping commit and push"
    git -C "$target_repo" reset --quiet HEAD -- cbi.json
    return 0
  fi

  branch_name=$(git -C "$target_repo" symbolic-ref --quiet --short HEAD)
  git -C "$target_repo" commit -m "chore: update cbi.json from OSTI sync $RUN_TIMESTAMP"
  git -C "$target_repo" push origin "$branch_name"
  log "Pushed updated cbi.json to origin/$branch_name"
}

run_scholar_scrape() {
  local python_bin=${SCHOLAR_PYTHON_BIN:-python3}
  local -a browser_args=()

  if [[ -n "${SCHOLAR_BROWSER_MODE:-}" ]]; then
    browser_args=(--browser "$SCHOLAR_BROWSER_MODE")
  fi

  log "Running scholar scrape"
  DOWNSTREAM_SYNC_SCRIPT=/bin/true \
    "$python_bin" "$OSTI_TOOLING_DIR/gscholscrape.py" --all "${browser_args[@]}"
}

run_downstream_sync() {
  local python_bin="$BRC_SCHEMA_DIR/.venv/bin/python"
  log "Running downstream sync"
  "$python_bin" "$OSTI_TOOLING_DIR/downstream_sync.py"
}

snapshot_run_outputs() {
  mkdir -p "$RUN_DIR"
  copy_if_present "$WEB_BRC_JSON" "$RUN_DIR/cbi.json"
  copy_if_present "$WEB_OSTI_JSON" "$RUN_DIR/osti.json"
  copy_if_present "$WEB_PUBLICATIONS_JSON" "$RUN_DIR/publications.json"
  copy_if_present "$SCHOLAR_OUTPUT_FILE" "$RUN_DIR/latest_osti_scholar_records.json"
}

require_command git
require_command uv
require_command flock

if [[ ! -x "$BRC_SCHEMA_DIR/.venv/bin/python" ]]; then
  log "brc-schema virtualenv missing; uv sync will create it"
fi

log "OSTI sync run started"
git_refresh_repo "$OSTI_TOOLING_DIR"
git_refresh_repo "$BRC_SCHEMA_DIR"
git_refresh_repo "$BRC_DATA_FEEDS_DIR"

log "Ensuring brc-schema environment is current"
uv sync --project "$BRC_SCHEMA_DIR"

stage_current_feed_inputs
run_scholar_scrape
run_downstream_sync
publish_outputs
snapshot_run_outputs
push_cbi_if_changed

log "OSTI sync run completed successfully"