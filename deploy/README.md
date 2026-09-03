# Linux OSTI sync handoff implementation

This workspace contains a Linux-native replacement for the scheduled GitHub Actions OSTI/CBI sync.

## Files

- `run_osti_sync.sh`: non-interactive runner that refreshes all three repos, stages the scraper dedupe input, refreshes scholar cache, always runs downstream sync, snapshots outputs, and commits/pushes `brc_data_feeds/cbi.json` only when it changed.
- `osti-sync.env.example`: machine-specific configuration template.
- `systemd/osti-sync.service`: `systemd` unit for the runner.
- `systemd/osti-sync.timer`: 6-hour schedule.
- `cron/osti-sync.cron`: cron alternative if `systemd` timers are not desired.

## Publish path policy

The workflow uses one explicit policy to resolve the path mismatch between `gscholscrape.py` and `downstream_sync.py`.

1. The current feed from `brc_data_feeds/cbi.json` is copied to `SCRAPER_DEDUPE_JSON` before each run.
2. The same current feed is also copied to `WEB_BRC_JSON` before each run so downstream code sees a consistent canonical input/output path.
3. `SCRAPER_DEDUPE_JSON` defaults to `/var/www/html/cbi.json` because that is the scraper's live dedupe input.
4. `WEB_BRC_JSON`, `WEB_OSTI_JSON`, and `WEB_PUBLICATIONS_JSON` are set explicitly to canonical output files under `$OSTI_ROOT/state/published`.
5. After downstream sync completes, the canonical `cbi.json` is copied into `brc_data_feeds/cbi.json` and pushed only if it changed.

This keeps dedupe behavior consistent while avoiding implicit dependency on `/var/www/html/CBI/cbi.json`.

## One-time host setup

These steps assume the target host uses `/opt/osti` and has the three sibling repositories checked out there.

```bash
sudo mkdir -p /opt/osti
sudo chown -R "$USER":"$USER" /opt/osti
git clone <osti-tooling-remote> /opt/osti/osti-tooling
git clone <brc-schema-remote> /opt/osti/brc-schema
git clone <brc_data_feeds-remote> /opt/osti/brc_data_feeds
```

Install the runtime prerequisites.

```bash
python3 --version
git --version
uv --version
```

`playwright` and Chromium must be available to the interpreter used by `SCHOLAR_PYTHON_BIN`. If they are not already installed on the host, install them before enabling the schedule.

```bash
python3 -m pip install requests beautifulsoup4 playwright
python3 -m playwright install chromium --with-deps
```

Create the local environment file and set real values.

```bash
cp osti-sync.env.example osti-sync.env
chmod 600 osti-sync.env
```

Minimum values to confirm in `osti-sync.env`:

- `ELINK_BEARER_TOKEN`
- `OSTI_TOOLING_DIR`
- `BRC_SCHEMA_DIR`
- `BRC_DATA_FEEDS_DIR`
- `SCRAPER_DEDUPE_JSON`
- `WEB_BRC_JSON`
- `WEB_OSTI_JSON`
- `WEB_PUBLICATIONS_JSON`
- `SCHOLAR_PYTHON_BIN`

## Manual run

Run the workflow once before enabling the schedule.

```bash
OSTI_SYNC_ENV_FILE=$PWD/osti-sync.env ./run_osti_sync.sh
```

The stable log file is written to `$WORKFLOW_LOG`, which defaults to `/opt/osti/logs/osti_workflow.log`.

## Validation checklist

The runner is designed to preserve the existing orchestration and fallback discovery behavior, but end-to-end validation must be executed on the Linux host with the real repositories and credentials.

Run these checks after the first successful manual execution.

1. Confirm `uv sync` succeeds in `brc-schema`.
2. Confirm the scholar scrape runs headlessly with Playwright/Chromium.
3. Confirm `downstream_sync.py` runs on every scheduled execution, even when scholar cache content is unchanged.
4. Confirm the workflow log contains a `recent_site_discovery status=ok` line.
5. Confirm OSTI record `3408037` appears in the discovery log output.
6. Confirm the generated `cbi.json` contains `3408037`.
7. Confirm the first run pushes successfully to `brc_data_feeds`.
8. Confirm a second run with no feed changes logs `No cbi.json changes detected; skipping commit and push`.

## `systemd` install

Copy the service and timer units into `/etc/systemd/system`, then enable the timer.

```bash
sudo cp systemd/osti-sync.service /etc/systemd/system/osti-sync.service
sudo cp systemd/osti-sync.timer /etc/systemd/system/osti-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now osti-sync.timer
sudo systemctl status osti-sync.timer
```

Adjust `User`, `Group`, `WorkingDirectory`, and `OSTI_SYNC_ENV_FILE` in `systemd/osti-sync.service` before installation if this workspace path differs from the target host path.

## Cron alternative

Install `cron/osti-sync.cron` if you prefer cron over `systemd`.

```bash
sudo cp cron/osti-sync.cron /etc/cron.d/osti-sync
sudo chmod 644 /etc/cron.d/osti-sync
```

## Notes

- The wrapper keeps its own non-blocking `flock` lock file to avoid overlapping scheduled runs.
- `downstream_sync.py` still manages its own lock behavior inside the Python layer.
- Git errors are not suppressed; fetch, pull, commit, or push failures will stop the run and remain visible in the stable log.