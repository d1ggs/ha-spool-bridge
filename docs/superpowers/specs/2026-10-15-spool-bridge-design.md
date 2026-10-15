# Spool Bridge — design

Date: 2026-10-15
Status: draft, awaiting review

## 1. Goal

Third-party filament on a Bambu Lab X2D should behave like Bambu RFID filament:

1. **Scan, load** — scan a label on the spool with an iPhone, put the spool into any AMS slot. No further input.
2. **Auto identify** — the printer tray gets the right slicer profile and colour, so Bambu Studio picks the correct profile.
3. **Levels in Studio** — the spool appears in Bambu Studio's Filament Manager in its slot, with grams remaining.
4. **Tracking** — grams are deducted after every print, including cancelled ones.

Non-goal: buying into Bambu filament. Spoolman is the master inventory so the data stays self-hosted and portable.

### Success criteria

- Scan + load a third-party spool → within 1 minute Studio shows the correct profile on the tray and the spool, with grams, in the Filament Manager slot.
- After a finished print, Spoolman and the Filament Manager show the spool reduced by the job's per-slot grams, exactly once.
- After a cancelled print, the deduction equals planned grams × progress at stop.
- HA restart, Spoolman downtime or cloud outage during or after a print loses no deduction and creates no duplicate.

### Scope

| In | Out |
|---|---|
| One X2D (dual nozzle), 2× AMS 2 Pro, 2 external spool holders | Any other printer on the account (e.g. the A1 mini) — its jobs and spools are ignored |
| iPhone with the HA Companion app (QR and NFC) | Android-specific features |
| Bambu RFID and third-party spools | Spool weighing hardware (manual weighing in Spoolman is supported) |

## 2. Architecture

```
 iPhone (scan QR/NFC) ──tag_scanned──► HA
                                        │
 ┌─────────────┐  REST + websocket  ┌───┴──────────────────────────┐
 │  Spoolman   │◄──────────────────►│  Spool Bridge (this repo)    │
 │  (master)   │                    │  HA custom integration       │
 └─────────────┘                    │   • scan → load assignment   │
                                    │   • printer sync ────────────┼─► bambu_lab.set_filament
                                    │   • cloud sync ──────────────┼─► Bambu cloud filament library
                                    │   • usage tracking ◄─────────┼── Bambu cloud job history
                                    │                              │   + ha-bambulab status/progress
                                    └──────────────────────────────┘
 Bambu Studio ◄── reads cloud library (profile, slot, grams) ──────┘
```

| Component | Role |
|---|---|
| Spoolman (HA add-on `fabio-garavini/hassio-addons`, official upstream image) | Master inventory: spools, weights, slot assignment |
| **Spool Bridge** | All logic in this spec. The **only** writer to the Bambu cloud library |
| ha-bambulab (existing, LAN mode) | Tray contents, print status and progress; `set_filament` action |
| ha-bambu-filaments | **Uninstalled** once the bridge runs (two cloud writers would conflict). Its MIT-licensed cloud login/client code is reused with attribution |

Rejected alternatives (from audits on 2026-10-15): SpoolmanSync (unauthenticated LAN port with HA-admin power; time-based usage estimate with an open over-count bug on X2D), HaspelSync (needs a USB stick in the X2D, second MQTT client, missed/duplicate bookings, separate assignment store).

### Ownership rule

Data flows **out of Spoolman** to the printer and the cloud. The only inbound flow is per-job grams from the cloud job history. **Spoolman always wins** a conflict.

| Data | Master | Mirrors |
|---|---|---|
| Spool identity, weights | Spoolman | Cloud library |
| Spool ↔ slot assignment | Spoolman | Cloud slot binding, printer tray profile |
| Grams used per job | Cloud job history (read-only) | Deducted in Spoolman |

## 3. Data model (Spoolman extra fields)

Created by the bridge on setup if missing. Extra field values are JSON-encoded strings (Spoolman convention).

| Entity | Field | Meaning |
|---|---|---|
| filament | `bambu_filament_id` | Bambu slicer profile id: catalogue (`GFL03`) or user Studio preset (`P…`). Empty → material default (§5.1) |
| spool | `bambu_cloud_id` | Cloud library spool id (int) |
| spool | `bambu_rfid` | Tray UUID (32 hex) for Bambu RFID spools |
| spool | `bambu_slot` | Current slot, e.g. `ams:1:2` (AMS index 1, slot index 2, both 0-based) or `ext:0` / `ext:1`; empty when not loaded |

Spoolman's built-in `location` is also set to a readable label (`X2D AMS 2 · slot 3`) for its UI; `bambu_slot` is authoritative.

The bridge never writes extra fields owned by other tools (`active_tray`, `tag`). Writes to `extra` are read-modify-write of the bridge's own keys only, re-reading the spool immediately before writing.

## 4. Scan → load flow

### Labels

A label (QR) or NFC sticker encodes the HA tag URL `https://www.home-assistant.io/tag/spool-<spoolman_spool_id>`. The iPhone camera (QR) or background NFC read shows a notification; tapping it opens the HA app, which fires `tag_scanned` with `tag_id = spool-<id>`. Requires HA iOS app ≥ 2026.5.0 (CVE-2026-66061).

Label generation: use Spoolman's label designer if it can encode this URL; otherwise the bridge provides a `spool_bridge.print_labels` action producing a printable page (decided during implementation).

### Assignment

1. `tag_scanned` with prefix `spool-` → look up the Spoolman spool. Unknown or archived → notify "unknown spool", stop. Other tags are ignored.
2. Arm a **5-minute load window** for that spool and notify the scanning device: "<filament>, <grams> g — load it into any slot within 5 min."
3. The first X2D tray (AMS or external) whose ha-bambulab sensor transitions **empty → loaded** inside the window receives the spool.
4. On assignment: write `bambu_slot` + `location` in Spoolman → printer sync (§5.1) → cloud sync (§5.2) → notify "<filament> → AMS 2 slot 3 ✓".

| Case | Behaviour |
|---|---|
| Window expires with no transition | Notification offers "Pick slot…" actionable buttons |
| Two trays transition in the window | Ambiguous → ask with buttons, never guess |
| Spool scanned while assigned elsewhere | Move: clear old slot, assign new |
| A new scan while a window is armed | Replaces the armed spool |
| Tray transitions loaded → empty | Clear `bambu_slot`/`location` of the spool in that slot; cloud unbind (§5.2) |
| Tray with a valid RFID (non-zero `tray_uuid`) becomes loaded | RFID path (§4.1); no scan needed, an armed window is not consumed |

### 4.1 Bambu RFID spools

On a loaded tray with a valid `tray_uuid`: find the Spoolman spool with that `bambu_rfid`. If none, create it (vendor Bambu Lab, filament from tray data and the Bambu catalogue, total weight from `tray_weight`, remaining from AMS %). Then assign as above, except printer sync is skipped (the printer already has the RFID profile).

## 5. Sync rules

### 5.1 Printer sync

On assignment of a non-RFID spool: call `bambu_lab.set_filament` on the tray entity with:

| Field | Source |
|---|---|
| `tray_info_idx` | filament `bambu_filament_id`, else material default (PLA→`GFL99`, PETG→`GFG99`, ABS→`GFB99`, ASA→`GFB98`, TPU→`GFU99`, PLA-CF→`GFL98`; IDs checked against the live catalogue at build time, table extended as needed) |
| `tray_color` | filament `color_hex` + `FF` alpha |
| `tray_type` | filament `material` |
| `nozzle_temp_min/max` | filament `settings_extruder_temp` ± 10 °C, else profile defaults |

### 5.2 Cloud sync (Spoolman → Bambu cloud library)

Endpoints (base `https://api.bambulab.com/v1/design-user-service/my/filament/v2`), verified live 2026-10-15 unless noted:

| Purpose | Call | Evidence |
|---|---|---|
| List | `GET ?offset=&limit=` | live |
| Create | `POST` (returns `{}`; re-list to find the id) | live |
| Update fields / weight | `PUT` body `{id, filamentName, …}`; **position fields are silently ignored** | live |
| Bind spool to slot | `POST /slot-mappings/sync` `{devId, mappings:[{amsId, amsSn, amsType, rfid, slotId, spoolId}]}` → `results[].code` 0 = ok | live; shape from Studio source (`fila_manager/wgtFilaManagerCloudSync.cpp`) and open-bamboo-networking MITM notes |
| Unbind | same call with `spoolId: 0, rfid: ""` | returned code 4 when the slot was physically occupied — hypothesis: only accepted on an empty slot; **verify** |
| Register RFID spool | `POST /ams/sync` | used by ha-bambu-filaments (verified by its author) |
| Delete | `DELETE /batch` `{ids:[…]}` | live |

Field mapping:

| Spoolman | Cloud |
|---|---|
| vendor name / material / filament name | `filamentVendor` / `filamentType` / `filamentName` |
| filament `color_hex` | `color` (`#RRGGBB`), `colors` on update |
| filament `weight` / spool `remaining_weight` | `totalNetWeight` / `netWeight` |
| `bambu_filament_id` (or default) | `filamentId` |
| `bambu_slot` | slot binding (AMS serial from ha-bambulab device registry; `amsType` 3 for AMS 2 Pro) |
| spool archived | cloud spool deleted, `bambu_cloud_id` cleared |

- **Scope:** every non-archived Spoolman spool is mirrored, loaded or not.
- **Linking:** by `bambu_cloud_id`; RFID spools by `bambu_rfid` ↔ cloud `RFID` (the cloud creates those itself via `/ams/sync`; the bridge registers them if missing).
- **Triggers:** Spoolman websocket change events (debounced 5 s) + full reconciliation every 15 min.
- **Conflicts:** reconciliation overwrites any cloud value that differs from Spoolman. Known sources: Studio pushing AMS % for RFID spools while open (observed 2026-10-15: 210 → 140 g); edits made in Studio's Filament Manager. Overwrites are logged at info level. Rule for the user: edit in Spoolman only.
- **Throttle:** at most 1 cloud write per spool per 10 s; whole-library reconciliation is diff-based.

### 5.3 One-time migration

Run once from the config flow, after showing a dry-run list and getting explicit confirmation:

- Cloud spools with `createType = ams` (Bambu RFID) → imported into Spoolman (vendor, filament, weights, `bambu_rfid`, `bambu_cloud_id`, current slot).
- Cloud spools with `createType = manual` → **deleted from the cloud**.

## 6. Usage tracking

1. **Print start** (ha-bambulab print status → `running`): persist a snapshot `{job_start_time, slot → spool_id}` for the X2D.
2. **Print progress**: persist the last known progress % while running/paused (survives restart).
3. **Print end** (status → `finish` or `failed`): fetch `GET /v1/user-service/my/tasks?limit=20`, select the X2D job (by `deviceId`) whose `startTime` matches the snapshot (±5 min). Retry with back-off for up to 10 min if not yet present.
4. For each `amsDetailMapping` entry: map the flat `ams` index to a slot (`0–15` → AMS `ams // 4`, slot `ams % 4`; external holders: **verify on X2D** — the observed `255` came from the A1 mini) → spool from the **snapshot** → grams = `weight` × factor.
   - `status = 2` (finished): factor 1.
   - `status = 3` (failed/cancelled): factor = persisted progress / 100; if unknown → no deduction, notify.
5. Deduct via Spoolman `PUT /api/v1/spool/{id}/use {use_weight}`. The cloud mirror follows via §5.2.

Startup catch-up: on integration start, process every X2D job that ended after the last processed job (snapshot-less jobs use the current assignment and are flagged in the notification).

### Ledger (exactly-once)

Persisted in HA storage, one record per `(job_id, ams_index)`: `pending → applied | skipped`, with grams, spool id, and Spoolman `used_weight` before the write.

- Before a retry after an uncertain write (timeout/connection reset), re-read the spool: if `used_weight ≈ before + grams` (±0.5 g) mark `applied` without writing.
- A slot with no assigned spool → `skipped` + notification (no fallback matching).
- Records older than 90 days are pruned.

## 7. Error handling and observability

| Failure | Behaviour |
|---|---|
| Bambu token expired (~90 days) / 401 | HA reauth flow; cloud sync and job fetch pause; ledger entries stay `pending` |
| Cloudflare block / 5xx / network | Exponential back-off (max 15 min); queued |
| Spoolman unreachable | Queue persisted in HA storage, replayed in order |
| `set_filament` fails | Retry 3×, then notify |
| Anything stuck > 1 h | Phone notification |

Entities: `sensor.spool_bridge_pending_jobs`, `sensor.spool_bridge_last_sync`, `sensor.spool_bridge_last_error`. Diagnostics download redacts token, email, serials, RFIDs, cloud ids.

## 8. Security

- No listening ports. Cloud token in the HA config entry (same as any cloud integration). Password never stored.
- Spoolman is reached via the Supervisor internal network only (`http://<addon-hostname>:8000`).
- Accepted risk: the Spoolman add-on exposes port 7912 on the LAN without authentication (Spoolman has no auth); impact limited to the filament inventory.
- Public repository: fixtures and docs must not contain real serials, spool ids, RFIDs, tokens or email. Fixtures are anonymised by a script before commit.

## 9. Testing

| Layer | Approach |
|---|---|
| Pure logic: slot index mapping, usage factors, ledger state machine, diffing, field mapping | pytest unit tests, fixtures from anonymised live captures (2026-10-15) |
| Spoolman client | Integration tests against the official Spoolman Docker image (pinned version) |
| Cloud client | Recorded-response tests; no live calls in CI |
| HA integration (config flow, tag events, tray transitions, services) | `pytest-homeassistant-custom-component` |
| End to end | Manual checklist on the X2D: scan → load → Studio profile + Filament Manager → print → grams → cancel a print → unload → HA restart mid-print |

## 10. Delivery

- Public GitHub repository, HACS custom repository (integration category), domain `spool_bridge`.
- CI: hassfest, HACS validation, pytest.
- Releases tagged with semver; installed versions pinned in HACS.

## 11. To verify during implementation

1. ha-bambulab reports empty → loaded for a third-party spool on the AMS 2 Pro (and the external holders).
2. Cloud unbind is accepted when the slot is physically empty.
3. How the X2D's external holders appear in `amsDetailMapping`.
4. `set_filament` on the X2D results in Studio selecting the profile (including user presets).
5. Whether Spoolman's label designer can encode the HA tag URL.
6. Spoolman websocket event format for spool changes.
7. Material default profile IDs (§5.1) against the live Bambu catalogue.
