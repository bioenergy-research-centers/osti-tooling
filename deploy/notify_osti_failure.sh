#!/usr/bin/env bash

set -Eeuo pipefail

if [[ "${SERVICE_RESULT:-unknown}" == "success" ]]; then
  exit 0
fi

ENV_FILE=${OSTI_SYNC_ENV_FILE:-/home/cloud/bioenergy_OSTI_updates/osti-sync.env}
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${ALERT_EMAIL:-}" ]]; then
  echo "OSTI sync failed, but ALERT_EMAIL is not configured" >&2
  exit 1
fi

if [[ "$ALERT_EMAIL" == *$'\n'* || "$ALERT_EMAIL" == *$'\r'* ]]; then
  echo "ALERT_EMAIL contains an invalid newline" >&2
  exit 1
fi

SENDMAIL_BIN=${SENDMAIL_BIN:-/usr/sbin/sendmail}
if [[ ! -x "$SENDMAIL_BIN" ]]; then
  echo "OSTI sync failed, but sendmail is unavailable at $SENDMAIL_BIN" >&2
  exit 1
fi

ALERT_LOG_LINES=${ALERT_LOG_LINES:-80}
if [[ ! "$ALERT_LOG_LINES" =~ ^[0-9]+$ ]]; then
  ALERT_LOG_LINES=80
fi

HOST_NAME=$(hostname -f 2>/dev/null || hostname)
ALERT_FROM=${ALERT_FROM:-osti-sync@$HOST_NAME}
if [[ "$ALERT_FROM" == *$'\n'* || "$ALERT_FROM" == *$'\r'* ]]; then
  echo "ALERT_FROM contains an invalid newline" >&2
  exit 1
fi

WORKFLOW_LOG=${WORKFLOW_LOG:-/opt/osti/logs/osti_workflow.log}
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

{
  printf 'To: %s\n' "$ALERT_EMAIL"
  printf 'From: %s\n' "$ALERT_FROM"
  printf 'Subject: [OSTI sync failure] %s at %s\n' "$HOST_NAME" "$TIMESTAMP"
  printf 'Content-Type: text/plain; charset=UTF-8\n'
  printf '\n'
  printf 'The CBI OSTI sync service failed.\n\n'
  printf 'Host: %s\n' "$HOST_NAME"
  printf 'Time: %s\n' "$TIMESTAMP"
  printf 'Service result: %s\n' "${SERVICE_RESULT:-unknown}"
  printf 'Exit code: %s\n' "${EXIT_CODE:-unknown}"
  printf 'Exit status: %s\n' "${EXIT_STATUS:-unknown}"
  printf 'Log: %s\n' "$WORKFLOW_LOG"
  if [[ -r "$WORKFLOW_LOG" ]]; then
    printf '\nLast %s log lines:\n\n' "$ALERT_LOG_LINES"
    tail -n "$ALERT_LOG_LINES" "$WORKFLOW_LOG"
  else
    printf '\nWorkflow log is unavailable.\n'
  fi
} | "$SENDMAIL_BIN" -t
