# Changelog

This changelog was reconstructed on 2026-05-25 from the version-bump commits, project journal,
and backlog history. Dates use the project repository's commit dates.

## 0.8.6 - 2026-05-26

- Warn in `mch validate` when `facts:` contains an unrecognised read key, with spelling
  suggestions such as `powersavings` to `powersaving`, while still permitting new firmware keys.
- Add a luna manual QA worksheet covering no-traffic smoke checks, service lifecycle, test-radio
  sync evidence, cancellation, stale facts, and optional commissioning.

## 0.8.5 - 2026-05-26

- Show the effective management radio and cached direct/flood/stored contact route during a
  foreground node sync without adding mesh traffic.

## 0.8.4 - 2026-05-26

- Record operator-triggered full-cycle and single-node sync starts, completions, failures, and
  aborts in recent activity logs.
- Apply command-registry timeouts to ordinary enforced writes, including the extended timeout for
  slow `set radio` updates over the mesh.

## 0.8.3 - 2026-05-26

- Make foreground `mch sync <node>` always stream progress and remove its optional verbose flag.
- Cancel active gateway-owning work during daemon shutdown, restoring a temporary radio before
  disconnecting the gateway.
- Cancel a foreground node sync when its CLI client disconnects instead of leaving it holding the
  operation lock through slow mesh timeouts.

## 0.8.2 - 2026-05-26

- Show coding rate in human-facing radio switch/restore output without treating a CR-only
  difference as a different reachability channel.
- Document global/group/node setting precedence for ungrouped nodes in the example config.
- Print a wait notice before `mch stop` or `mch restart` blocks while current sync work exits.

## 0.8.1 - 2026-05-25

- Added `global.via_radio` as an optional fleet-wide reachability-radio fallback beneath node and
  group overrides.

## 0.8.0 - 2026-05-25

- Replaced per-node `via:` with `via_radio:` to identify the temporary radio tuple used to reach
  a node without confusing it with a MeshCore routing path.
- Added group-level `via_radio:` inheritance, with per-node override, for normal sync and
  replacement commissioning.

## 0.7.2 - 2026-05-25

- Corrected terminology in progress/log messages and documentation: temporarily switching the
  gateway radio tuple for management is not a MeshCore routing hop/path.

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
