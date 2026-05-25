# Changelog

This changelog was reconstructed on 2026-05-25 from the version-bump commits, project journal,
and backlog history. Dates use the project repository's commit dates.

## 0.7.1 - 2026-05-25

- Added `mch sync -v|--verbose <node>` to stream progress while a foreground sync waits for
  facts, settings, writes, and operations.
- Persisted fact and setting read failures in `mch logs`, so partial node syncs can be diagnosed
  without reading the systemd journal.
- Documented that `TRIES` counts enforced write attempts, not failed reads.

## 0.7.0 - 2026-05-25

- Added the normalized fleet/node report model with `schema_version: 1`, node health, settings
  status/action classifications, issues, and plan data.
- Switched terminal `status` and `plan` rendering to source data from the shared daemon report
  model.
- Added the optional loopback-only, read-only HTTP JSON API for future dashboard work:
  `/health`, `/status`, `/status/<node>`, `/issues`, `/plan`, and `/logs`.
- Removed the draft `/api/v1/...` and `/healthz` route forms while the API is still in active
  development.

## 0.6.1 - 2026-05-25

- Removed padded relative-age formatting from activity output so node logs read naturally.

## 0.6.0 - 2026-05-25

- Improved `mch logs` identity display with labels/pubkey prefixes and added relative time to
  activity rendering.

## 0.5.2 - 2026-05-25

- Made YAML tab-indentation parse failures report an actionable correction.

## 0.5.1 - 2026-05-25

- Improved config/secrets hot reload to detect atomic or timestamp-preserving file replacements.

## 0.5.0 - 2026-05-25

- Made generated commissioning config snippets valid for current label/group/name handling.

## 0.4.1 - 2026-05-25

- Clarified status output and forced-sync partial-result reporting.

## 0.4.0 - 2026-05-25

- Ensured interrupted node syncs persist a terminal partial state instead of appearing complete.

## 0.3.1 - 2026-05-25

- Limited setting update-attempt counters to enforced settings.

## 0.3.0 - 2026-05-25

- Added persisted enforcement update-attempt counting and exposed it as `TRIES` in status and
  drift summaries.

## 0.2.3 - 2026-05-24

- Added a curated command registry for read/write forms, validation metadata, comparison rules,
  safety flags, and slow-command timeouts.

## 0.2.2 - 2026-05-24

- Added field-aware numeric comparison, including radio tuple float-roundoff tolerance and
  latitude/longitude tolerance.
- Improved status display of values that differ textually but compare as nominal.

## 0.2.1 - 2026-05-24

- Added sync lookup by node label.
- Reworked settings output with policy/drift columns and visible placeholders for unobserved
  facts/settings.

## 0.2.0 - 2026-05-23

- Added private-key identity validation warnings for configuration/secrets mismatches.
- Improved new and replacement commissioning recovery after partially completed attempts.

## 0.1.2 - 2026-05-23

- Increased commissioning timeouts for slow `set prv.key` and `set radio` writes to 180 seconds,
  based on live device testing.

## 0.1.1 - 2026-05-23

- Added bench investigation support and captured the slow private-key-write failure analysis that
  led to the commissioning timeout fix.

## 0.1.0 - 2026-05-17

- Established the initial project version during early development of the daemon, CLI,
  configuration model, SQLite observed-state store, scheduler, messenger, and gateway support.
