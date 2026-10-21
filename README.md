# Spool Bridge

Home Assistant integration that makes [Spoolman](https://github.com/Donkie/Spoolman) the master inventory for third-party filament on a Bambu Lab X2D, and keeps the printer and Bambu Studio in step with it.

- **Scan, load.** Scan a spool's QR label or NFC tag with the Home Assistant iPhone app, put the spool into any AMS slot. The slot that goes from empty to loaded within 5 minutes gets the spool.
- **Profile on the printer.** The tray gets the spool's slicer profile and colour (via ha-bambulab), so Bambu Studio picks the right profile.
- **Levels in Studio.** Every active Spoolman spool is mirrored into the Bambu cloud filament library, with its remaining grams, so Studio's Filament Manager shows it. Spools without RFID are bound to their AMS slot by the bridge; Bambu RFID spools are placed by the printer itself (the cloud refuses manual binds for them and updates them once a print uses the spool).
- **Usage.** After each print the per-slot grams from the Bambu cloud job history are deducted in Spoolman, exactly once. Cancelled prints are booked by the progress reached.
- **AMS auto-refill.** If a tray runs dry mid-print and the printer carries on from another tray with the same material and colour, the bridge notices (the tray goes empty while the print keeps running) and splits that slot's grams by the print progress at that moment: the spool that ran out is booked up to there and left at 0 g, the backup spool gets the rest. You get one notification, since the split is an estimate: weigh the backup spool to correct it. Two runouts chained in one print are not split further.
- **Bambu RFID spools** are picked up automatically when loaded.

Spoolman always wins: edit spools in Spoolman, not in Studio's Filament Manager.

## Requirements

- Home Assistant 2026.8 or newer, [HACS](https://hacs.xyz)
- [ha-bambulab](https://github.com/greghesp/ha-bambulab) with the printer set up (LAN mode is fine)
- Spoolman reachable from Home Assistant (e.g. the [fabio-garavini Spoolman add-on](https://github.com/fabio-garavini/hassio-addons))
- A Bambu cloud account with the printer bound to it (email + password login; Apple/Google sign-in accounts need a password set in Bambu Handy → Account security)
- Home Assistant iOS app 2026.5.0 or newer for scanning

Scope: one printer (built and tested on an X2D with two AMS 2 Pro). External spool holders are tracked but not bound in the cloud library.

## Install

1. HACS → three dots → Custom repositories → `https://github.com/d1ggs/ha-spool-bridge`, category Integration. Install, restart Home Assistant.
2. Settings → Devices & services → Add integration → **Spool Bridge**: Spoolman URL, printer, phone, then sign in to Bambu (or reuse a Bambu Filaments login).
3. The last step shows a one-time migration: Bambu RFID spools in the cloud library are imported into Spoolman, all other cloud spools are deleted. Leave the box unticked to skip it.

## Labels

- **QR:** in Spoolman, Settings → General → Base URL = `https://www.home-assistant.io/tag`, then print labels with "Use HTTP URL". Scanning one yields the tag `spool/show/<id>`.
- **NFC:** in the Home Assistant app, write a tag with the id `spool-<id>`.

Each Spoolman filament can carry a `bambu_filament_id` (e.g. `GFL03`, or a Studio user preset id). Without it a generic profile for the material is used.

### Shorter filament names (optional)

Names imported from the Spoolman database can be long enough to overflow a label, e.g. `Panchroma™ Matte (Formerly PolyTerra™) Earth Brown`. Under Settings → Devices & services → Spool Bridge → Configure you can add rewrite rules, one per line:

```
™|®
\s*\(Formerly[^)]*\)
```

Each line is `pattern => replacement`, or just `pattern` to delete what it matches (Python regular expressions; spaces are collapsed afterwards). The example turns that name into `Panchroma Matte Earth Brown`.

- Off by default; leave the field empty to keep names as imported.
- Only filaments imported from the database are renamed. Filaments you or the bridge created are left alone.
- Rules run at every sync: right after saving them, after any spool change, and every 15 minutes. The new names also reach the Bambu cloud library.
- A rule that would change its own output again (e.g. `Matte => Matte Finish`) is skipped, so names can't grow on every sync.

## Services

| Service | Fields |
|---|---|
| `spool_bridge.assign` | `spool_id`, `slot` (`ams:<unit>:<slot>` or `ext:<n>`, 0-based) |
| `spool_bridge.unassign` | `slot` |
| `spool_bridge.reconcile` | — |

## How it talks to Bambu

Unofficial cloud API, reverse-engineered by the community; it may break. Slot bindings use the same call Bambu Studio's filament manager makes (`/my/filament/v2/slot-mappings/sync`).

## Credits

- Bambu cloud login and filament client adapted from [ha-bambu-filaments](https://github.com/Timmes123/ha-bambu-filaments) (MIT, see `NOTICE`).
- Slot-mapping request shape documented by [open-bamboo-networking](https://github.com/ClusterM/open-bamboo-networking) and the [Bambu Studio](https://github.com/bambulab/BambuStudio) source.

## License

MIT
