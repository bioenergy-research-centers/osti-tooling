# OSTI Hourly Sync Deployment

This directory contains the refactored OSTI sync infrastructure, designed for shared machine access and multi-developer/multi-agent operations.

## Quick Start

### 1. Configuration
Edit `/opt/osti/env` to set your ELINK_BEARER_TOKEN (or ensure `/var/www/OSTI_config.ini` has it):
```bash
nano /opt/osti/env
# Uncomment and fill: ELINK_BEARER_TOKEN="your-token-here"
```

### 2. Manual Test Run
```bash
/opt/osti/bin/osti_hourly_sync.sh
tail -50 /opt/osti/logs/latest_osti_hourly_sync.log
```

### 3. Install Hourly Cron Job
```bash
sudo /opt/osti/bin/install_osti_hourly_cron.sh
```

### 4. Verify Cron Installation
```bash
sudo crontab -l
sudo tail -20 /opt/osti/logs/cron.log
```

## Directory Structure

```
/opt/osti/
├── bin/
│   ├── osti_hourly_sync.sh           # Main sync script
│   └── install_osti_hourly_cron.sh   # Cron installer
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
- `/var/www/html/CBI/cbi.json` - BRC datasets (backward compatible)

## Configuration

Edit `/opt/osti/env` to customize:

```bash
# Core paths
REPO_DIR="/opt/osti/brc-schema"
STATE_DIR="/opt/osti/state"
OUT_DIR="/opt/osti/state/runs"
LOG_DIR="/opt/osti/logs"
LOCK_FILE="/opt/osti/state/osti_hourly_sync.lock"

# API configuration (required)
ELINK_BEARER_TOKEN="your-bearer-token"  # OR use /var/www/OSTI_config.ini
ELINK_API_URL="https://www.osti.gov/elink2api/records"
SITE_OWNERSHIP_CODE="CBI"
ELINK_PAGE_SIZE="500"

# Web publishing
WEB_OSTI_JSON="/var/www/html/CBI/cbi_osti.json"
WEB_BRC_JSON="/var/www/html/CBI/cbi.json"

# Retention policy (168 = 7 days of hourly runs)
KEEP_RUNS="168"
```

## Troubleshooting

### Token not found
```bash
# Check token in config file
grep -i token /var/www/OSTI_config.ini

# Or set in env file
echo 'ELINK_BEARER_TOKEN="your-token"' >> /opt/osti/env
```

### Manual run fails
```bash
/opt/osti/bin/osti_hourly_sync.sh
# Check log immediately:
cat /opt/osti/logs/latest_osti_hourly_sync.log
```
Note if the script errors it will not symlink latest_osti_hourly_sync.log. The log can still be found in /opt/osti/logs/YYYYMMDDTHHMMSSZ.log.

### Cron not running
```bash
# Check cron file
sudo cat /etc/cron.d/osti-sync

# Check cron logs
sudo tail -50 /opt/osti/logs/cron.log

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
tail -f /opt/osti/logs/cron.log
```

### Check last 5 runs
```bash
ls -lt /opt/osti/state/runs/osti_records_*.json | head -5
```

### Verify latest files
```bash
# Show latest files and their size
ls -lh /opt/osti/state/runs/latest_*.json
```

### Check record counts
```bash
jq '.records | length' /opt/osti/state/runs/latest_osti_publications.json
jq '.records | length' /opt/osti/state/runs/latest_osti_datasets.json
```
