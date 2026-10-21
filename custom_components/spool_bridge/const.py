"""Constants for Spool Bridge."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "spool_bridge"

CONF_SPOOLMAN_URL = "spoolman_url"
CONF_PRINTER_SERIAL = "printer_serial"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_REGION = "region"
CONF_EMAIL = "email"
CONF_TOKEN = "token"
CONF_NAME_RULES = "filament_name_rules"

DEFAULT_SPOOLMAN_URL = "http://3b88f413-spoolman:8000"
BAMBULAB_DOMAIN = "bambu_lab"
BAMBU_FILAMENTS_DOMAIN = "bambu_filaments"

EXTRA_CLOUD_ID = "bambu_cloud_id"
EXTRA_RFID = "bambu_rfid"
EXTRA_SLOT = "bambu_slot"
EXTRA_FILAMENT_ID = "bambu_filament_id"

LOAD_WINDOW_S = 300
SETTLE_S = 10
RECONCILE_INTERVAL = timedelta(minutes=15)
RECONCILE_DEBOUNCE_S = 5
CLOUD_WRITE_MIN_INTERVAL_S = 10
LEDGER_RETRY_INTERVAL = timedelta(minutes=5)
JOB_FETCH_ATTEMPTS = 10
JOB_FETCH_DELAY_S = 60
JOB_MATCH_TOLERANCE_S = 300
LEDGER_TTL_DAYS = 90
STUCK_AFTER_S = 3600
USED_WEIGHT_TOLERANCE_G = 0.5

TASK_FINISHED = 2
TASK_FAILED = 3
BUSY_STATES = {"prepare", "running", "pause", "slicing", "init"}
END_STATES = {"finish", "failed"}
# Only a real idle/end state before "busy" starts a new print (not offline/unknown).
IDLE_STATES = {"idle", "finish", "failed"}
SET_FILAMENT_ATTEMPTS = 3
SET_FILAMENT_RETRY_DELAY_S = 2

AMS_TYPE_BY_MODEL = {"AMS": 1, "AMS Lite": 2, "AMS 2 Pro": 3, "AMS HT": 4}

SIGNAL_STATE = f"{DOMAIN}_state"
ACTION_PREFIX = "SPOOLBRIDGE"
