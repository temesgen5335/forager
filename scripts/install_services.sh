#!/usr/bin/env bash
# Install the systemd units for the Personal Job Agent. Run ON THE VPS with sudo.
# Substitutes the real repo path / user / venv python into the unit templates.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
PYTHON="$REPO/.venv/bin/python"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"

if [[ ! -x "$PYTHON" ]]; then
  echo "venv python not found at $PYTHON — run: uv venv && uv pip install -e '.[telegram,llm]'" >&2
  exit 1
fi

echo "Repo:   $REPO"
echo "User:   $RUN_USER"
echo "Python: $PYTHON"

# Schedule cadence is config-driven: read the friendly knobs from .env (falling back to
# the same defaults the units used to hardcode) and translate them to systemd OnCalendar.
env_get() {  # env_get KEY DEFAULT — reads KEY from .env, else DEFAULT
  local key="$1" def="$2" val=""
  if [[ -f "$REPO/.env" ]]; then
    # `|| true`: a no-match grep exits 1, which under `set -euo pipefail` can abort the
    # install on the "key absent -> use default" path (behavior varies by bash version).
    val="$(grep -E "^[[:space:]]*${key}=" "$REPO/.env" | tail -1 | cut -d= -f2- || true)"
    val="${val%%#*}"                    # strip trailing comment
    val="${val//\"/}"; val="${val//\'/}"   # strip quotes
    val="$(echo -n "$val" | xargs)"     # trim surrounding whitespace
  fi
  echo "${val:-$def}"
}

INGEST_EVERY_HOURS="$(env_get INGEST_EVERY_HOURS 4)"
DIGEST_AT="$(env_get DIGEST_AT 07:00)"
INGEST_JITTER_SEC="$(env_get INGEST_JITTER_SEC 300)"

if ! [[ "$INGEST_JITTER_SEC" =~ ^[0-9]+$ ]]; then
  echo "INGEST_JITTER_SEC must be a whole number of seconds, got: $INGEST_JITTER_SEC" >&2
  exit 1
fi
INGEST_ONCALENDAR="$("$PYTHON" -m jobagent.scheduling oncalendar-every-hours "$INGEST_EVERY_HOURS")" \
  || { echo "invalid INGEST_EVERY_HOURS=$INGEST_EVERY_HOURS (want 1-24)" >&2; exit 1; }
DIGEST_ONCALENDAR="$("$PYTHON" -m jobagent.scheduling oncalendar-daily-at "$DIGEST_AT")" \
  || { echo "invalid DIGEST_AT=$DIGEST_AT (want HH:MM)" >&2; exit 1; }

echo "Ingest: every ${INGEST_EVERY_HOURS}h ($INGEST_ONCALENDAR, +${INGEST_JITTER_SEC}s jitter)"
echo "Digest: daily at $DIGEST_AT ($DIGEST_ONCALENDAR)"

for unit in jobagent-bot.service jobagent-pipeline.service jobagent-pipeline.timer \
            jobagent-ingest.service jobagent-ingest.timer; do
  sed -e "s|__REPO__|$REPO|g" \
      -e "s|__USER__|$RUN_USER|g" \
      -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__INGEST_ONCALENDAR__|$INGEST_ONCALENDAR|g" \
      -e "s|__DIGEST_ONCALENDAR__|$DIGEST_ONCALENDAR|g" \
      -e "s|__INGEST_JITTER__|$INGEST_JITTER_SEC|g" \
      "$REPO/deploy/$unit" | sudo tee "$UNIT_DIR/$unit" >/dev/null
  echo "installed $unit"
done

sudo systemctl daemon-reload
sudo systemctl enable --now jobagent-bot.service
sudo systemctl enable --now jobagent-pipeline.timer
sudo systemctl enable --now jobagent-ingest.timer

echo
echo "Done. Check status:"
echo "  systemctl status jobagent-bot.service"
echo "  systemctl list-timers 'jobagent-*'"
echo "  journalctl -u jobagent-bot -f"
