"""Constants for Spool Bridge."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "spool_bridge"

CONF_SPOOLMAN_URL = "spoolman_url"
CONF_PRINTER_SERIAL = "printer_serial"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_NAME_RULES = "filament_name_rules"
CONF_BAMBUDDY_URL = "bambuddy_url"
CONF_BAMBUDDY_KEY = "bambuddy_api_key"

DEFAULT_SPOOLMAN_URL = "http://3b88f413-spoolman:8000"
# Bambuddy's HA add-on and HA Core both use host networking.
DEFAULT_BAMBUDDY_URL = "http://127.0.0.1:8000"
# Bambuddy's Spoolman URL: the bridge's loopback pass-through (spoolman_proxy).
SPOOLMAN_PROXY_HOST = "127.0.0.1"
SPOOLMAN_PROXY_PORT = 7913
CONF_GATEWAY_PORT = "gateway_port"
BAMBUDDY_SYNC_INTERVAL = timedelta(seconds=30)
# Bambuddy looks for a print's 3MF once at print start, before the X2D has
# copied it to the USB stick; nudge it again after these delays (seconds).
BAMBUDDY_NUDGE_DELAYS_S = (60, 120)
BAMBULAB_DOMAIN = "bambu_lab"

EXTRA_RFID = "bambu_rfid"
EXTRA_SLOT = "bambu_slot"
EXTRA_FILAMENT_ID = "bambu_filament_id"
# Bambuddy's spool fields: the RFID/NFC tag it links a tray by, and its per-spool preset.
EXTRA_TAG = "tag"
EXTRA_SLICER_FILAMENT = "bambu_slicer_filament"

LOAD_WINDOW_S = 300
SETTLE_S = 10
RECONCILE_INTERVAL = timedelta(minutes=15)
RECONCILE_DEBOUNCE_S = 5
LEDGER_RETRY_INTERVAL = timedelta(minutes=5)
JOB_FETCH_ATTEMPTS = 10
JOB_FETCH_DELAY_S = 60
LEDGER_TTL_DAYS = 90
STUCK_AFTER_S = 3600
USED_WEIGHT_TOLERANCE_G = 0.5
RUNOUT_PAUSE_GRACE_S = 60  # a pause this soon after a tray ran dry means no auto-refill happened

TASK_FINISHED = 2
TASK_FAILED = 3
BUSY_STATES = {"prepare", "running", "pause", "slicing", "init"}
END_STATES = {"finish", "failed"}
# Only a real idle/end state before "busy" starts a new print (not offline/unknown).
IDLE_STATES = {"idle", "finish", "failed"}
SET_FILAMENT_ATTEMPTS = 3
SET_FILAMENT_RETRY_DELAY_S = 2
# How long the tray may take to report the profile it was given.
SET_FILAMENT_CONFIRM_S = 10

AMS_TYPE_BY_MODEL = {"AMS": 1, "AMS Lite": 2, "AMS 2 Pro": 3, "AMS HT": 4}

SIGNAL_STATE = f"{DOMAIN}_state"
ACTION_PREFIX = "SPOOLBRIDGE"
