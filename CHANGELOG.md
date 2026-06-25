# Changelog

This changelog was reconstructed on 2026-05-25 from the version-bump commits, project journal,
and backlog history. Dates use the project repository's commit dates.

## Unreleased

## 0.11.0 - 2026-06-25

- **Facts are now a policy, not a separate `facts:` list.** `SettingPolicy` gains a fourth value,
  `fact`: read and record the observed value with no desired value and no write-back (the others
  stay `enforce`/`audit`/`skip`). A fact is just an observation mch has no opinion about, so facts
  and settings are one resolved-policy space (Node > Group > Global). Collect a key as a fact with
  `mch set <node> <key> --policy fact` (no value); read-only keys like `ver`/`board`/`role`/
  `neighbors` accept only `fact`/`skip` and can never take a desired value. Because a key resolves
  to exactly one policy, the daemon reads it **once per cycle** and stores it in exactly one place —
  removing the latent double-poll where a key listed as both a fact and a setting was read twice.
  "Configured facts" in the report and `mch clean facts` now follow each node's resolved fact set
  instead of one global list.
- **New `mch show NODE KEY`** — the read-side complement to `mch set`/`mch unset`. It explains one
  key for one node: the desired/policy row at each scope (global, the node's group, the node), the
  resolved policy and desired value (Node > Group > Global) with which scope supplied it, and the
  value mch has observed (from the settings or fact store). Read-only; sends no mesh traffic.
- **Breaking: the flat top-level `facts:` list is retired from config.yaml.** It was the last
  desired-ish knob still living in config.yaml after the 0.10.0 sunset. The daemon now strict-loads
  and refuses to start if a `facts:` block remains (joining `nodes:`/`groups:`/`global:`). Migrate
  it like the rest: `mch import <config-with-facts>` folds a legacy `facts:` list into global
  `policy: fact` rows (a key already carried as a global desired setting keeps that row), then delete
  `facts:` from config.yaml. An absent/empty `facts:` is tolerated. After upgrading, a node collects
  no facts until its fact rows exist, so run the import before stripping the block.

## 0.10.0 - 2026-06-21

- **config.yaml desired-state sunset.** The SQLite DB is now the single source of truth for node
  inventory and desired state; config.yaml carries only daemon settings (gateway, scheduler,
  messenger, api, paths, facts, profiles). The daemon loads strict and refuses to start if config.yaml
  still contains a `nodes:`, `groups:`, or `global:` desired-state block, with an error pointing at
  `mch import` (empty leftover blocks are tolerated). Migration path: on the running daemon run
  `mch import` (now also accepts an explicit `mch import <legacy.yaml>` path and carries node
  label/group, not just settings), verify with `mch export`, strip the desired-state blocks from
  config.yaml, then restart. The fleet report drops the config-vs-DB "ghost"/"unseen" distinction —
  every DB node is a managed node.
- **`mch export`/`mch import` are now a complete fleet backup/restore.** Since the DB is the only
  copy of intent, `mch export` includes every node's identity (full pubkey), label, group, desired
  settings, and operations — even nodes with no desired settings — and emits each node's group so the
  dump re-imports cleanly. `mch import` recreates a node from its full pubkey even when the gateway/DB
  has not seen it yet (a short prefix still needs a known node and is skipped), so after a server or
  gateway replacement `mch import backup.yaml` rebuilds the fleet and the favourite mirror re-pins
  each node as its contact reappears. Retired nodes are included in the backup tagged `retired: true`;
  on import you choose `--retired keep` (restore them as retired) or `--retired skip` (omit them), and
  a write that would touch retired nodes is refused until you pick one.
- **Inventory + favourites are DB-driven.** On startup and reload the daemon reconciles the gateway's
  favourited repeaters with the DB managed set: a favourited repeater the DB doesn't know is seeded as
  a managed node, and every managed node is (re)favourited as an eviction pin. `mch set --group`
  validates the group against DB membership instead of config.groups.
- **Retire, don't delete.** New `mch retire <node>` / `mch unretire <node>`: retiring a node excludes
  it from sync and unfavourites its contact (eviction-pin removal, not contact deletion) while keeping
  its history; unretiring re-favourites it and queues it to reconverge. `mch prune` now hard-deletes
  retired nodes (the purge step after retire); `mch remove` guards a still-managed node behind
  `--force`. `mch commission` prints an importable `nodes:` doc plus `mch import` guidance instead of a
  config.yaml snippet.
- Added `name_prefix`: a string (usually an emoji) prepended to a node's desired name, settable at
  global, group, or node scope and resolving Node > Group > Global - so a whole group can be tagged
  on the map (e.g. "📡") while each node keeps its own name. The prefix is prepended verbatim with
  no separator; put a space in the prefix if you want one. It applies only when a node has a desired
  `settings.name`; a prefix with no base name does nothing. Config load now rejects any node whose
  combined `prefix+name` exceeds the 31-byte UTF-8 firmware name cap (bench-confirmed byte-based,
  not char-based, and unaffected by a set location), because the firmware would truncate on a raw
  byte boundary - corrupting multibyte emoji and producing a stored name that can never equal the
  desired value, i.e. a permanent enforcement loop. The 31-byte cap is now a shared constant reused
  by interactive commissioning.
- Sync now reaches a node on its own desired `radio` by default, instead of only the gateway home
  radio. The reach radio resolves `via_radio` (the split-mesh/bridge override) -> else the node's
  desired `radio` setting -> else the gateway home radio, each Node > Group > Global. Previously the
  gateway only switched radios when `via_radio` was set, so a node whose desired radio differed from
  home (e.g. a 915.800 node managed from a 915.075 gateway) was never probed on its own radio - it
  was reachable only via the recovery hunt, and once its observed radio matched desired the
  "radio confirmed -> skip recovery" rule disabled the hunt and the node was wrongly marked
  unreachable. With the primary probe now on the node's own radio, that skip is correct, and
  `via_radio` is needed only for genuine bridging (and is redundant when it equals the desired
  radio). Also simplified the recovery hunt's radio restore so the gateway is returned to the home
  radio exactly once, with no wasteful bounce back to home before hunting.
- Reachability probe now proactively logs in to an auth-required node that silently drops
  unauthenticated commands. A secured node (observed on v1.15.0) drops a directed command from a
  peer not in its ACL with no reply and no permission error - just a timeout - so the bare `ver`
  reachability probe timed out and the node was mislabeled unreachable, and `_send_authed` (which
  only re-authenticates on an explicit permission error) never logged in. Now, when the bare probe
  fails and a password is configured and the node is not yet authenticated this session, mch logs
  in once (entering the node ACL) and re-probes before giving up. The login is attempted only after
  the cheap bare probe fails, so a node already in the gateway's ACL keeps the no-login fast path.
  (This was the real cause of the faulkland "unreachable" investigation - not routing.)
- Added `mch probe NODE --radio FREQ,BW,SF,CR [--advert] [--login]`, a reachability-only
  investigation command. It temporarily switches the gateway radio, optionally sends a gateway
  zero-hop advert (`--advert`) and/or logs in with the node's configured password (`--login`,
  needed for auth-required nodes that drop unauthenticated commands), sends one directed `ver`,
  restores the radio, and reports the result with live progress. It does not change the contact
  route, config, secrets, or the node. (The `--login` path is what confirmed faulkland's blocker
  was authentication, not routing.)
- `mch adopt` now installs a direct (zero-hop) route on the new gateway contact and sends one
  directed `ver` on the discovered radio to confirm the route carries a command, restoring the
  gateway radio afterwards. The confirm probe result is shown in the command output and recorded in
  `mch logs`. (Note: the faulkland investigation later showed the original directed-command failure
  was an authentication problem, not routing - see the reachability-probe login change above; the
  direct-route install remains useful for genuinely unrouted nodes.)

## 0.9.35 - 2026-06-03

- Skip config-derived radio recovery when a node's radio is already confirmed (its observed
  `radio` matches desired). The node is on the radio we expect, so a reachability failure is a
  path/availability problem, not a radio one - hunting nearby spreading-factor variants is wasted
  mesh traffic. The node is marked unreachable instead, and the skip is logged.

## 0.9.34 - 2026-06-03

- Bounded the gateway `get_contacts` serial round-trip with a timeout (default 30s). An unbounded
  await here could hang config reload and leave `systemctl restart mchd` stuck in `deactivating`
  until SIGKILL (observed during the 0.9.29 luna deploy). A locked-up gateway now raises a clean
  `GatewayError` instead of blocking the daemon indefinitely.

## 0.9.33 - 2026-06-02

- Added discovery result annotations (`STATUS` and `LABEL`), `mch discover --unknown-only`, and
  `mch adopt PUBKEY --radio ...` for explicitly adopting an already-running discovered repeater
  into the local DB and gateway contacts.

## 0.9.32 - 2026-06-02

- Hid the low-level discovery request tag from `mch discover` output, leaving only operator-useful
  prefix, full public key, node type, and SNR columns.
- Improved scheduled-sync busy messages to prefer configured node labels when DB labels are
  missing, so errors identify the node more clearly.
- Added backlog notes for active-discovery adoption, DB-primary inventory, config export, radio
  memory, and protected secret storage.

## 0.9.31 - 2026-06-02

- Added `mch discover --radio FREQ,BW,SF,CR`, a bounded active MeshCore discovery command that
  temporarily switches the gateway radio, sends a discovery request, prints discovered public keys
  and signal metadata, and restores the gateway radio without DB/contact/config writes.
- Expanded `mch path` documentation and help with field meanings and the path repair flow.

## 0.9.30 - 2026-06-02

- Added cached management path context to `mch status NODE`, including stored path metadata and
  `path repair needed` marking for flood-routed or missing gateway contacts.

## 0.9.29 - 2026-06-02

- Made radio recovery SF bounds explicit as SF5 through SF12, so nearby SF fallback never emits
  invalid SF4/SF13 while allowing valid SF5/SF6 candidates.

## 0.9.28 - 2026-06-02

- Removed `profiles.yaml` presets from automatic radio recovery; fallback now stays within radios
  declared in `config.yaml`.
- Radio recovery now tries the exact effective configured radio before nearby SF variants.
- Radio hunts now restore the gateway's home radio once at the end of an exhausted hunt instead of
  restoring between each failed candidate.
- Added backlog notes for `mch probe`, known-good radio history with expiry, observed-radio
  recovery candidates, and deferred nearby bandwidth variants.

## 0.9.27 - 2026-06-02

- When a flood-routed contact does not answer on the gateway's current radio, try the same
  config-derived radio recovery candidates before marking it unreachable. This lets nodes whose
  desired `radio` differs from the gateway radio be found on their declared channel.

## 0.9.26 - 2026-06-02

- Treat MeshCore operation replies beginning with `ERR:` as operation failures, even when the
  gateway delivered the reply as command text. This correctly reports cases such as
  `ERR: clock cannot go backwards`.

## 0.9.25 - 2026-06-02

- Fixed recurring operations that previously failed, such as `clock sync`, so they are retried
  after their configured `every_hours` retry window instead of staying failed forever.
- After a successful `clock sync`, re-read the `clock` fact so `mch status NODE` shows the
  corrected clock in the same sync.
- Added `get pwrmgt.support` to the backlog for board/firmware capability investigation.

## 0.9.24 - 2026-06-02

- Fixed the canonical `fleet_node` detail projection used by `mch status NODE` to preserve
  `next_sync_at`, so the approximate `next sync` line renders the scheduler timestamp.

## 0.9.23 - 2026-06-02

- Fixed the legacy node-detail status payload to include `next_sync_at`, so the new
  approximate `next sync` line in `mch status NODE` uses real scheduler state.

## 0.9.22 - 2026-06-02

- Added an approximate `next sync` line to `mch status NODE`, rendering the scheduler's stored
  `next_sync_at` as `in ~...`, `due now`, or `unscheduled`.

## 0.9.21 - 2026-06-02

- Shortened actionable clock offset notes in `mch status NODE` from `clock fast by ...` /
  `clock slow by ...` to `fast by ...` / `slow by ...`.

## 0.9.20 - 2026-06-02

- Changed clock freshness notes in `mch status NODE` to use `within ±1m` for tolerated offsets
  and `clock fast by ...` / `clock slow by ...` for actionable drift.

## 0.9.19 - 2026-06-02

- Changed near-zero clock drift display in `mch status NODE` from `drift <1m` to
  `within +-1m`, so clocks inside the useful tolerance are not presented as drift.

## 0.9.18 - 2026-06-02

- Added `operations:` support on `global:` and `groups:` with Node > Group > Global precedence.
  This supports group-level recurring commands such as `clock sync` without duplicating the
  operation on every node.

## 0.9.17 - 2026-06-02

- Added per-fact observation timestamps to node reports and `mch status NODE`, including a parsed
  clock-drift note for MeshCore `clock` fact values.
- Added `mch clean facts` for dry-run-first removal of cached facts that are no longer configured,
  with node, group, or fleet scope plus `--fact <key>` narrowing for cases such as misspelled
  `powersavings`.
- Recorded the follow-up design note to support future global/group/node fact configuration
  precedence; current fact configuration remains fleet-wide.

## 0.9.16 - 2026-06-01

- Made `mch status NODE` recent activity use the same timestamp, relative-age, and level layout as
  `mch logs`, while omitting redundant node identity columns in the single-node detail view.
- Added explicit activity/progress messages when temporary gateway radio switches are restored, so
  node history shows both the switch away and the switch back.

## 0.9.15 - 2026-06-01

- Fixed the CLI RPC reader to report a clean error when `mchd` closes the control socket before
  sending a final response, such as during a daemon restart in the middle of foreground `mch sync`.
  This replaces an operator-facing `JSONDecodeError` traceback with a short daemon-disconnected
  message.

## 0.9.14 - 2026-06-01

- Fixed `tools/query_node_info.py --radio` to save the attached gateway's current radio before
  switching and restore it afterward, including normal exception, Ctrl-C, and SIGTERM paths.
- Added focused tests for anonymous-probe gateway radio restoration behavior.

## 0.9.13 - 2026-06-01

- Added conservative radio recovery for nodes with configured radio context: after normal
  reachability probes fail, mchd tries up to three config-derived radio candidates. Curated
  `profiles.yaml` radio presets are tried first, followed by nearby spreading-factor variants
  of the node's effective `via_radio` or desired `radio`; each attempt is logged and the gateway
  radio is restored afterward.
- Added structured `setting_history` persistence for successful enforced writes, recording the
  previous observed value, new value, command, and timestamp. This gives operators a rollback
  breadcrumb for radio changes and other enforced settings on hard-to-access nodes.

## 0.9.12 - 2026-06-01

- Added standalone MeshCore anonymous repeater probing tooling under `tools/`. The
  `query_node_info.py` helper sends anonymous owner, basic, and optional regions requests,
  supports compact `--radio freq,bw,sf,cr` test-radio selection, and reports explicitly that
  firmware version is unavailable through the anonymous request path.

## 0.9.11 - 2026-06-01

- Added explicit `mch path` recovery controls: `--discover` asks the mesh for a fresh path
  candidate, `--advert` shows the gateway's cached advert path, and `--apply <path[:mode]>`
  applies an operator-approved replacement path to the gateway contact.

## 0.9.10 - 2026-06-01

- Captured live region-command behavior from a Mac-attached Heltec V3 on MeshCore v1.15.0:
  `region home` replies with `home is *`, `region default` replies with
  `default scope is <null>`, and `region put <name>` creates flood-allowed regions by default.
- Added field-aware comparison for `region.home` and `region.default` so their prose readbacks
  compare against desired bare values without reporting false drift.
- Clarified `mch exec` versus `mch admin` in the CLI reference: `exec` is a single remote command
  through `mchd` over the mesh, while `admin` is a local serial `meshcore-cli` session after
  stopping `mchd`.

## 0.9.9 - 2026-06-01

- Fixed `mch admin` privilege handling: when the wrapper is run via `sudo`, systemd control can
  remain privileged but the launched `meshcore-cli` process drops back to the invoking user and
  groups instead of running the interactive serial session as root.
- Added command metadata for MeshCore region operations (`region put`, `region save`) and
  first-class setting keys for `region.home` and `region.default`; live verification is still
  required before recommending region enforcement broadly.

## 0.9.8 - 2026-06-01

- Added `mch path <node>` to show the daemon's cached gateway management route for one node
  without sending mesh traffic. The command reports direct, stored, flood, missing, or unknown
  route state, raw `out_path` metadata, and flags flood/missing routes as needing path repair.
- Added `mch admin`, which stops `mchd`, launches `meshcore-cli` with the configured gateway
  serial device and baud, then restarts `mchd` when the admin session exits. `--no-restart`,
  `--config`, `--cli`, and passthrough `meshcore-cli` arguments are supported.

## 0.9.7 - 2026-06-01

- Refined lossy-link handling after qh-corbin live testing: stored-path reads now get a small
  per-node retry budget, while failed stored-path probes get only one flood-routed `ver` check as
  a path-recovery signal. mchd does not continue a full sync or retry writes over flood after a
  stored path fails.
- Recorded qh-corbin's operational recovery: the node was alive and traceable, but the gateway
  contact had fallen back to flood. Manually restoring the stored path through qh-woldhuis
  (`out_path=2fd9`, two-byte hash mode) allowed management syncs to complete.
- Split issue reporting terminology: DB-only retired nodes are now `ghosts`, while configured
  nodes that have never been seen are reported separately as `unseen_nodes`. The read-only fleet
  report schema is now version 2.

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
