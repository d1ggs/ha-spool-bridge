# Spool Bridge

Home Assistant integration that makes [Spoolman](https://github.com/Donkie/Spoolman) the master inventory for the filament on a Bambu Lab X2D, and keeps the printer (and, optionally, [Bambuddy](#bambuddy-optional)) in step with it. Everything runs on your LAN: no Bambu cloud account.

- **Scan, load.** Scan a spool's QR label or NFC tag with the Home Assistant iPhone app, put the spool into any AMS slot. The slot that goes from empty to loaded within 5 minutes gets the spool.
- **Profile on the printer.** The tray gets the spool's slicer profile and colour (via ha-bambulab), so Bambu Studio picks the right profile.
- **Usage.** After each print the per-slot grams the printer reports (read locally through ha-bambulab) are deducted in Spoolman, exactly once. Cancelled prints are booked by the progress reached.
- **AMS auto-refill.** If a tray runs dry mid-print and the printer carries on from another tray with the same material and colour, the bridge notices (the tray goes empty while the print keeps running) and splits that slot's grams by the print progress at that moment: the spool that ran out is booked up to there and left at 0 g, the backup spool gets the rest. You get one notification, since the split is an estimate: weigh the backup spool to correct it. Two runouts chained in one print are not split further.
- **Bambu RFID spools** are picked up automatically when loaded.

Spoolman always wins: edit spools in Spoolman (or in Bambuddy, which edits Spoolman).

## Requirements

- Home Assistant 2026.8 or newer, [HACS](https://hacs.xyz)
- [ha-bambulab](https://github.com/greghesp/ha-bambulab) with the printer set up
- The printer in **LAN-only + Developer mode**: firmware that signs MQTT commands refuses tray profiles from anything else (the bridge then tells you what to set by hand)
- Spoolman reachable from Home Assistant (e.g. the [fabio-garavini Spoolman add-on](https://github.com/fabio-garavini/hassio-addons))
- Home Assistant iOS app 2026.5.0 or newer for scanning

Scope: one printer (built and tested on an X2D with two AMS 2 Pro).

## Install

1. HACS → three dots → Custom repositories → `https://github.com/d1ggs/ha-spool-bridge`, category Integration. Install, restart Home Assistant.
2. Settings → Devices & services → Add integration → **Spool Bridge**: Spoolman URL, printer, phone.

## Labels

- **QR:** in Spoolman, Settings → General → Base URL = `https://www.home-assistant.io/tag`. Then set the label's **QR code link** to **URL**: in the label designer (Labels → select the QR code element), or in the Print Labels dialog, depending on the Spoolman build. The preview should read `https://www.home-assistant.io/tag/spool/show/{id}`; the default `WEB+SPOOLMAN:S-{id}` only works with Spoolman's own scanner. Scan with the iPhone Camera app; the link opens the Home Assistant app, which reports the tag `spool/show/<id>`.
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
- Rules run right after saving them, after any spool change, and every 15 minutes.
- A rule that would change its own output again (e.g. `Matte => Matte Finish`) is skipped, so names can't grow on every sync.

## Bambuddy (optional)

[Bambuddy](https://github.com/maziggy/bambuddy) can show and use the Spoolman inventory (its Spoolman mode). The bridge keeps the two in step, so you can assign spools from either side and Spoolman stays the single inventory.

- **Slots, both ways.** A spool the bridge assigns (scan → load, the `assign` service) appears on Bambuddy's AMS slot within seconds. A spool you assign or remove in Bambuddy (its slot dialog, its QR labels) is recorded in Spoolman within 30 seconds, so the print's usage lands on it. Bambu RFID spools are linked through Spoolman's `tag` field instead, which the bridge fills in.
- **Usage is booked once, by the bridge.** Bambuddy deducts usage itself and has no setting to stop it; on an X2D it usually can't read the print file at print start, so its numbers would be partial. Bambuddy therefore reaches Spoolman through the bridge's gateway at `http://127.0.0.1:7913`, which passes everything through (spools, edits, labels) and answers Bambuddy's usage deductions without applying them.
- **Profiles match.** Bambuddy sends the slot's profile to the printer when a spool is assigned there; the bridge records the profile it uses on the spool (`bambu_slicer_filament`), so both send the same. A preset you pick for a spool in Bambuddy is used by the bridge too. While a print runs, the bridge holds back catch-up assignments for the trays that print uses.
- **Complete print archives.** With a USB stick in the X2D and *Store sent files on external storage* on, the printer copies each Bambu Studio print onto the stick, but only after Bambuddy has looked for it at print start. The bridge asks Bambuddy again 1 and 3 minutes in, which fills in the print's archive.
- **Tidy slot table.** Bambuddy rows that point at deleted spools, or at another spool on an RFID tray, are removed (Spoolman reuses spool ids, so such a row could otherwise name the wrong spool).

Setup:

1. Bambuddy → Settings → API Keys: create a key with **Read status** and **Manage inventory**, limited to the printer.
2. Home Assistant → Spool Bridge → Configure: Bambuddy URL (`http://127.0.0.1:8000` for the Bambuddy add-on) and the key; change the gateway port (default 7913) only if something else uses it. The bridge checks the URL and key and restarts.
3. Bambuddy → Settings → Spoolman: enable it with the URL `http://127.0.0.1:7913` (or your gateway port) and AMS sync **manual**.

The gateway listens on the Home Assistant host's loopback only, so only apps with host networking (like the Bambuddy add-on) can reach it.

## Services

| Service | Fields |
|---|---|
| `spool_bridge.assign` | `spool_id`, `slot` (`ams:<unit>:<slot>` or `ext:<n>`, 0-based) |
| `spool_bridge.unassign` | `slot` |
| `spool_bridge.reconcile` | — |

## Credits

- Slot detection, profiles and usage build on [ha-bambulab](https://github.com/greghesp/ha-bambulab)'s sensors and services.

## License

MIT
