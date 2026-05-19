# gscholscrape.py

Code is set to run playwright or selenium.
```
# Test with 10 titles using browser mode
python3 gscholscrape.py --sample 10 --browser playwright

# Default development mode (first 10 titles)
python3 gscholscrape.py --browser playwright

# Full run
python3 gscholscrape.py --all --browser playwright
```

The standalone script writes JSON output files next to the script:
- `osti_matched.json`
- `osti_unmatched.json`

`osti_matched.json` now stores enriched records for each match, including:
- validated `osti_id` and `osti_url`
- normalized publication fields (`title`, `authors`, `doi`, `publication_date`, etc.)
- captured OSTI detail-page JSON-LD `payload` for downstream bioenergy.org-ready processing

The collector reads live `/var/www/html/cbi.json` and skips OSTI IDs already
present there to reduce repeat OSTI lookups.

# OSTI 6-Hour Sync Deployment

This directory contains the refactored OSTI sync infrastructure, designed for shared machine access and multi-developer/multi-agent operations.

## Installation Layout

Pick one root directory for the OSTI workspace, for example `OSTI_ROOT=/path/to/osti-root`.
This repo should live at `$OSTI_ROOT/osti-tooling`, and the `brc-schema` repo
should live alongside it at `$OSTI_ROOT/brc-schema`. The scripts default to
those paths, but the location can be changed by setting the environment
variables in `$OSTI_ROOT/env` or by exporting them before running the scripts.

## Quick Start

### 1. Configuration
Edit `$OSTI_ROOT/env` to set your ELINK_BEARER_TOKEN (or ensure
`/var/www/OSTI_config.ini` has it):
```bash
nano $OSTI_ROOT/env
# Uncomment and fill: ELINK_BEARER_TOKEN="your-token-here"
```

### 2. Manual Test Run
```bash
python3 $OSTI_ROOT/osti-tooling/gscholscrape.py --all
tail -50 $OSTI_ROOT/logs/osti_workflow.log
```

### 3. Install 6-Hour Cron Job
```bash
echo "0 */6 * * * nobody /usr/bin/python3 $OSTI_ROOT/osti-tooling/gscholscrape.py --all >> $OSTI_ROOT/logs/osti_workflow.log 2>&1" | sudo tee /etc/cron.d/osti-sync >/dev/null
sudo chmod 644 /etc/cron.d/osti-sync
```

This runs at 00:00, 06:00, 12:00, and 18:00 as the `nobody` user.

### 4. Verify Cron Installation
```bash
sudo cat /etc/cron.d/osti-sync
sudo tail -20 $OSTI_ROOT/logs/cron.log
```

## Directory Structure

```
$OSTI_ROOT/
├── downstream_sync.py                # Main downstream sync script
├── gscholscrape.py                   # Scholar scrape + trigger logic
├── bin/
│   └── install_osti_hourly_cron.sh   # Legacy cron installer
├── env                                # Environment config (edit this)
├── etc_cron.d_osti-sync.template     # Cron template
├── brc-schema/                        # Symlink to brc-schema repo
├── state/
│   ├── runs/                          # Generated output files
│   └── osti_hourly_sync.lock         # Lock file for preventing concurrent runs
└── logs/
    ├── osti_hourly_sync_*.log         # Individual run logs
    ├── latest_osti_hourly_sync.log    # Latest run symlink
    └── cron.log                       # System cron execution log
```

## Output Files

Each run generates timestamped JSON files:

- `latest_osti_scholar_records.json` - Additive scholar cache used by the 6-hour cron job
- `osti_records_*.json` - All OSTI records (raw, with metadata cleanup)
- `osti_publications_*.json` - Publications only (product_type: JA, B, TR, AR, P, PA)
- `osti_datasets_*.json` - Datasets only (product_type: DA)
- `brc_datasets_*.json` - BRC-transformed datasets
- `brc_publications_*.json` - BRC-transformed publications

Plus symlinks to latest:
- `latest_osti_records.json`
- `latest_osti_publications.json`
- `latest_osti_datasets.json`
- `latest_brc_datasets.json`
- `latest_brc_publications.json`

## Web Publishing

Published files are copied to:
- `/var/www/html/CBI/cbi_osti.json` - All records (backward compatible)
- `/var/www/html/cbi.json` - BRC datasets (append-only, validated before publish)

The downstream OSTI sync runs only when the scholar cache changes.
If the scrape output is unchanged, downstream processing is skipped.

## Configuration

Edit `$OSTI_ROOT/env` to customize:

```bash
# Core paths
REPO_DIR="$OSTI_ROOT/brc-schema"
STATE_DIR="$OSTI_ROOT/state"
OUT_DIR="$OSTI_ROOT/state/runs"
LOG_DIR="$OSTI_ROOT/logs"
LOCK_FILE="$OSTI_ROOT/state/osti_hourly_sync.lock"

# API configuration (required)
ELINK_BEARER_TOKEN="your-bearer-token"  # OR use /var/www/OSTI_config.ini
ELINK_API_URL="https://www.osti.gov/elink2api/records"
SITE_OWNERSHIP_CODE="CBI"
ELINK_PAGE_SIZE="500"

# Web publishing
WEB_OSTI_JSON="/var/www/html/CBI/cbi_osti.json"
WEB_BRC_JSON="/var/www/html/cbi.json"

# Retention policy (168 = 42 days at 4 runs/day)
KEEP_RUNS="168"
```

## Troubleshooting

### Token not found
```bash
# Check token in config file
grep -i token /var/www/OSTI_config.ini

# Or set in env file
echo 'ELINK_BEARER_TOKEN="your-token"' >> $OSTI_ROOT/env
```

### Manual run fails
```bash
$OSTI_ROOT/osti-tooling/downstream_sync.py
# Check log immediately:
cat $OSTI_ROOT/logs/latest_osti_hourly_sync.log
```
Note if the script errors it will not symlink latest_osti_hourly_sync.log. The log can still be found in $OSTI_ROOT/logs/YYYYMMDDTHHMMSSZ.log.

### Cron not running
```bash
# Check cron file
sudo cat /etc/cron.d/osti-sync

# Check cron logs
sudo tail -50 $OSTI_ROOT/logs/cron.log

# Check system cron logs (if available)
sudo journalctl -u cron --since "1 hour ago"
```

### Permission issues
If you can't write to web directories:
```bash
# The script will attempt sudo -n cp
# Ensure your user is in sudoers with NOPASSWD for cp:
# sudo visudo
# o9h ALL=(ALL) NOPASSWD: /bin/cp, /bin/chown
```


## Monitoring

### Live log monitoring
```bash
tail -f $OSTI_ROOT/logs/cron.log
```

### Check last 5 runs
```bash
ls -lt $OSTI_ROOT/state/runs/osti_records_*.json | head -5
```

### Verify latest files
```bash
# Show latest files and their size
ls -lh $OSTI_ROOT/state/runs/latest_*.json
```

### Check record counts
```bash
jq '.records | length' $OSTI_ROOT/state/runs/latest_osti_publications.json
jq '.records | length' $OSTI_ROOT/state/runs/latest_osti_datasets.json
```
