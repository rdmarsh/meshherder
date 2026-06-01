# Changelog

This changelog was reconstructed on 2026-05-25 from the version-bump commits, project journal,
and backlog history. Dates use the project repository's commit dates.

## Unreleased

- Refined lossy-link handling after qh-corbin live testing: stored-path reads now get a small
  per-node retry budget, while failed stored-path probes get only one flood-routed `ver` check as
  a path-recovery signal. mchd does not continue a full sync or retry writes over flood after a
  stored path fails.
- Recorded qh-corbin's operational recovery: the node was alive and traceable, but the gateway
  contact had fallen back to flood. Manually restoring the stored path through qh-woldhuis
  (`out_path=2fd9`, two-byte hash mode) allowed management syncs to complete.

## 0.9.6 - 2026-06-01

- Align the relative-age field in `mch logs` by left-padding the age text inside the parentheses.

## 0.9.5 - 2026-06-01

- Give config-only "not visible to gateway" log entries structured display identity, so they
  render in `mch logs` as `[prefix] [label]` without embedding the prefix and label in prose.

## 0.9.4 - 2026-06-01

- Render node identities in log output as `[pubkey/label]` and align the level and node columns
  across each displayed log set.

## 0.9.3 - 2026-06-01

- Render absolute timestamps in `mch logs` and node recent activity using the operator's local
  timezone while preserving UTC-based relative ages.

## 0.9.2 - 2026-06-01

- Renamed the node-detail settings result column to `STATUS` and replaced symbolic drift markers
  with colored text statuses: `ok`, `nominal`, `not observed`, and `drifted`.

## 0.9.1 - 2026-05-31

- Enforced setting writes that fail over a cached stored path now perform one cheap flood-routed
  `ver` probe before retrying the write via flood. If the fallback probe also fails, the write is
  not retried, avoiding duplicate writes to a node that is not answering.
- Split development, production, service, udev, and upgrade instructions into `INSTALL.md`, leaving
  the README focused on overview, configuration, and command reference.
- Renamed the commissioning wizard command from `mch commission` to `mch new`. The wizard now uses
  consistent `q` cancel wording and shows selected group/radio choices first when editing.
- Improved `mch validate` diagnostics for operator config mistakes: duplicate YAML keys now
  report line numbers, and unknown node-group references are reported together with close-match
  hints.
- When daemon-backed CLI commands cannot connect to `mchd`, `mch` now checks the default config
  file and reports whether config validation is blocking daemon startup.

## 0.9.0 - 2026-05-31

- Added a low-cost reachability probe before each node's full sync pass: a bare `ver` read is
  sent first (up to two attempts), and if the node does not answer, the rest of the fact and
  setting pass is skipped instead of timing out on every read. A single missed probe is retried
  because a marginal mesh link can drop one reply and answer the next, so two consecutive misses
  are required before the node is marked offline and the cycle skipped. The skip is recorded in
  node activity (`mch logs`) and the daemon journal.
- The probe's `ver` reply is reused as the `ver` fact, so the reachability check costs no extra
  mesh traffic when `ver` is already a collected fact.
- `last_connected` now advances only after the probe confirms the node is answering this cycle,
  rather than optimistically at the start of processing.
- Independent read-only fact and setting reads are now issued in a randomized order each cycle, so
  a marginal connection does not repeatedly lose the same trailing observations.
- Live-verified on luna: both forced and scheduled syncs of unresponsive nodes now skip after two
  probes instead of timing out on every read.

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
