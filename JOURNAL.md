# Project Journal

This journal records project decisions and lessons learned without preserving private bench
details, node identities, generated private keys, local hostnames, or operator passwords.
Use `BACKLOG.md` for active todo items, wishlist items, deferred ideas, and explicit non-goals.

## 2026-06-01 - Luna 0.9.12 Deployment

- Released and deployed `0.9.12` to luna. The first production install attempt stayed on
  `0.9.11` because `/opt/meshherder` pulls from luna's local operator checkout, and that checkout
  had not yet pulled from GitHub. Updating `/home/rdm/code/meshherder` first, then pulling
  `/opt/meshherder`, installed `meshherder-0.9.12`.
- Verified `mch --version` reported `0.9.12`, `mchd` restarted and reported `active`, and the
  release-code checkout was at commit `87f647d` before this journal-only follow-up.

## 2026-06-01 - Anonymous MeshCore Repeater Probe Tool

- Confirmed from upstream MeshCore firmware that anonymous repeater requests expose owner/basic
  metadata but not firmware version. `ANON_REQ_TYPE_OWNER` returns remote clock, node name, and
  owner info; `ANON_REQ_TYPE_BASIC` returns remote clock plus feature flags; firmware version is
  only available through the logged-in/guest owner-info request path.
- Added `tools/query_node_info.py` as a standalone live-test helper rather than folding it into
  mch/mchd. It sends raw anonymous request frames through a local companion, parses owner/basic
  and optional regions responses, and keeps output provenance clear by reporting version as
  unavailable for anonymous requests.
- Added compact `--radio freq,bw,sf,cr` handling for bench and field testing, while preserving
  detailed per-field overrides such as `--coding-rate 8`.

## 2026-06-01 - Luna 0.9.11 Deployment

- Released and deployed `0.9.11` to luna at commit `91ae8b8`. The production venv installed
  `meshherder-0.9.11`, `mch --version` reported `0.9.11`, `mchd` restarted and reported
  `active`, and `/opt/meshherder` was at the expected commit.
- Read-only smoke test: `mch path qh-corbin` still reported the expected stored route through
  `out_path=2fd9`, `out_path_len=1`, `out_path_hash_mode=1`.
- No live path mutation was run during deployment. The new recovery controls were only installed
  and verified through read-only CLI behavior; `--discover`/`--advert`/`--apply` still need
  deliberate field use when a route actually needs repair.

## 2026-06-01 - Management Path Recovery Controls

- Added explicit path recovery controls to keep normal management off flood while giving operators
  a daemon-owned repair workflow. `mch path <node>` remains read-only inspection;
  `--discover` requests a MeshCore path-discovery candidate, `--advert` reads the gateway's cached
  advert path, and `--apply <path[:mode]>` writes an operator-approved replacement path to the
  gateway contact.
- Candidate discovery deliberately does not apply the result. The operator still has to inspect
  and approve the path, because a wrong path can make management reachability worse.
- The apply path records a warning log entry with the previous cached route and new applied path.
  Historical success/failure scoring remains future work; the next useful step is to store a small
  set of observed candidate paths with outcome counters rather than only logging one-off repairs.

## 2026-06-01 - Luna 0.9.10 Deployment

- Released `0.9.10` so the deployed package version reflects the live region-command behavior
  captured after the initial `0.9.9` release. The functional delta is region readback comparison
  and documentation: no extra serial live test was run on luna.

## 2026-06-01 - Admin Privilege Boundary And Region Metadata

- `mch admin` originally ran `meshcore-cli` with the same privileges as the wrapper process. That
  meant the practical workaround for polkit prompts (`sudo mch admin ...`) also made the
  interactive serial session root-owned. Fixed the boundary so systemd control remains privileged
  when needed, but `meshcore-cli` drops back to `SUDO_UID`/`SUDO_GID` plus the invoking user's
  supplementary groups.
- Region investigation result: current MeshCore docs describe `region put <name> [parent_name]`,
  `region save`, `region home`, and v1.15+ `region default`. mch already supported raw region
  setup through long-interval operations; added command metadata for `region put`/`region save`
  operations and first-class setting keys `region.home` and `region.default`.
- Mac-attached Heltec V3 / MeshCore v1.15.0 testing: `region` returned `*^ F`, `region help` and
  `region list` returned `Err - ??`, `region home` returned `home is *`, and `region default`
  returned `default scope is <null>`. A temporary unsaved `region put mch-test` returned
  `OK - (flood allowed)`, `region get mch-test` returned `mch-test F`, `region remove mch-test`
  returned `OK`, and a final `region get mch-test` returned `Err - unknown region`.
- Because `region home/default` readbacks are prose rather than bare values, `region.home` and
  `region.default` use field-aware comparison so desired `*` matches `home is *` and desired
  `<null>` matches `default scope is <null>`.
- Do not claim region enforcement is field-proven yet. The next live step is to read/write
  `region home` and `region default` on representative firmware, capture exact reply text, and
  confirm whether the v1.15+ auto-allow/auto-save behavior is consistent across deployed boards.

## 2026-06-01 - Luna 0.9.8 Deployment

- Released and deployed `0.9.8` to luna at commit `ec3d86d`. The production venv installed
  `meshherder-0.9.8`, `mch --version` reported `0.9.8`, `mchd` restarted cleanly, and
  `mch status` responded through the control socket.
- Read-only smoke test: `mch path qh-corbin` reported the expected stored route through
  `out_path=2fd9`, `out_path_len=1`, `out_path_hash_mode=1`, matching the manually recovered
  path from the qh-corbin investigation.
- Deployment note: root on luna cannot fetch the private GitHub remote by SSH, and unauthenticated
  HTTPS also fails. `/opt/meshherder` now uses `/home/rdm/code/meshherder` as its `origin`, so the
  operator checkout pulls from GitHub and the production checkout can use `sudo git pull --ff-only`
  locally without root network credentials.

## 2026-06-01 - Path Inspection And Admin Shell

- Added a read-only `mch path <node>` command after qh-corbin and Acacia showed that route state
  needs to be inspectable before adding any path mutation workflow. The command only reports the
  daemon's cached gateway contact metadata; it does not probe, discover, sync, or change routes.
- The path output deliberately calls out flood and missing gateway contacts as `path repair needed`
  because normal management should not treat flood as a healthy steady-state route. Stored and
  direct routes are shown as cached evidence, not proof that the node is currently reachable.
- Added `mch admin` for deliberate serial administration. It stops `mchd`, launches
  `meshcore-cli` using `gateway.device` and `gateway.baud` from config, then restarts `mchd` unless
  `--no-restart` is requested. This releases the serial port at the process level; merely pausing
  the scheduler is not sufficient while `mchd` still owns the gateway connection.
- Kept only one command name, `admin`, rather than carrying a `connect` alias. Project CLI
  vocabulary should stay narrow unless a second name has clear operator value.

## 2026-06-01 - Acacia Flood-Route Partial Diagnosis

- Acacia (`4d0385b9d942`) was staying `partial` because its last successful management pass was
  over a flood-routed cached contact, not a stored repeater path. The node answered enough commands
  to collect `ver`/`neighbors` and accept two enforced writes, but timed out on enforced reads for
  `advert.interval`, `agc.reset.interval`, and `multi.acks`; the first two remain unobserved in
  `mch status acacia`, so mch cannot prove the desired state is applied.
- The live gateway evidence matches the older route note: Acacia's cached management path was
  `flood`, while field knowledge says the useful route is via repeaters rather than flood. A manual
  `mch exec acacia ver` on 2026-06-01 also timed out, so the current flood route is not a reliable
  management path.
- Fix: mch now treats an already flood-routed contact the same way qh-corbin taught us to treat
  flood recovery. It sends at most reachability probes, records `path repair needed`, leaves a
  reachable flood-routed node `partial`, and defers facts/settings/writes until the gateway has a
  stored path. This avoids normalizing partial convergence and writes over flood.

## 2026-06-01 - qh-corbin Route Recovery And Flood Boundary

- qh-corbin (`e2601d591517`) was not dead: `last_seen` continued to update from adverts, traces
  to `e260` and via `2fd9,e260` returned good SNR, and cached facts/settings showed the repeater
  was otherwise healthy. The failure was management reachability.
- Luna's gateway had qh-corbin stored as flood-routed (`out_path_len=-1`, empty `out_path`).
  `disc_path e2601d591517` timed out and left the contact on flood. Manual path testing showed
  qh-woldhuis's two-byte path hash `2fd9` was a viable first hop: after
  `change_path e2601d591517 2fd9:1`, a non-secret test using the configured password logged in
  and `ver` succeeded.
- Subsequent `mch sync qh-corbin` runs over `out_path=2fd9` confirmed the mesh is lossy but
  manageable: two passes dropped one read each, and a final pass completed with `facts=2`,
  `settings=12`, `drift=0`, and `read-failed=0`. qh-corbin ended `configured` with a fresh
  `last_connected`.
- Design rule from this incident: flood is useful for controlled path recovery/diagnosis, not as
  ordinary management transport. Once a useful stored path is known, mch should prefer that path,
  tolerate isolated command loss with bounded retries, and avoid full-sync or write retries over
  flood. A failed stored path should trigger a small recovery signal, then an explicit
  path-learning workflow rather than hammering the mesh.

## 2026-05-31 - Luna `/opt/meshherder` Checkout Restored

- Incident: `mchd` on luna was restart-looping with `error loading config: unknown config key
  'api'` even though the deployed config was valid for current meshherder.
- Cause: `/opt/meshherder` had become an old non-git source copy whose `pyproject.toml` declared
  `version = "0.2.0"`. Running the documented `sudo /opt/meshherder/.venv/bin/pip install .`
  from that directory reinstalled the old package into the live venv. The old parser did not know
  the newer `api:` config block.
- Recovery: mirrored the clean checkout from `/home/rdm/code/meshherder` back into
  `/opt/meshherder` while preserving `/opt/meshherder/.venv`, reinstalled from `/opt/meshherder`,
  recopied the systemd unit, reloaded systemd, and restarted `mchd`.
- Verified final state on luna: `/opt/meshherder` is a real `main...origin/main` checkout at
  `9a776f2`, `mch --version` reports `0.9.0`, `mchd` is active, the control socket works, and
  `mch status` returns fleet status.
- Documentation guardrail added: the upgrade flow now runs `git status --short --branch` before
  `pip install .` and warns not to install from a non-git `/opt/meshherder` tree.

## 2026-05-31 - Documentation Split for Install Flow

- Moved install, service setup, udev, and upgrade instructions out of `README.md` into
  `INSTALL.md`.
- Rationale: the README was mixing project overview, install procedure, configuration reference,
  command reference, API examples, logs, and upgrades. A separate install guide is easier for
  operators following a Raspberry Pi/server setup and gives a cleaner source file for a later
  GitHub Pages documentation pass.
- The README now keeps a short install pointer and its docs table links to `INSTALL.md`.

## 2026-05-31 - Reachability Probe Before Full Sync (0.9.0)

- Problem: an unreachable node still cost a timeout on every declared fact and every enforced
  setting, multiplying slow mesh waits across a node that simply was not answering this cycle.
- Fix: `_process_node` now runs `_probe_reachable()` (bare `ver`, up to `_PROBE_ATTEMPTS=2`)
  before collecting facts/settings. Two consecutive misses mark the node `OFFLINE` and skip the
  rest of the pass; the skip is written to node activity and the journal. The radio-restore
  `finally` still runs, so a temporary `via_radio` switch is always unwound.
- Single-miss retry rationale: luna `0.8.4` testing showed a node time out on `ver` in one sync
  and answer in the next, so one missing reply must be treated as inconclusive. This is the
  bounded retry called for in the backlog "operation safety and reachability" item.
- The probe reuses its `ver` reply as the `ver` fact (matched via `read_command_for_key`), so when
  `ver` is already a configured fact the probe adds no extra mesh traffic. If `ver` is not a
  configured fact, the probe is one extra cheap read - an acceptable cost for skipping a full
  dead-node pass.
- `last_connected` moved to after a successful probe (was set optimistically at processing start),
  so it now reflects an actual exchange this cycle. `last_seen` still updates at the start since it
  means "we attempted/observed it" and is also driven passively by adverts.
- Randomized read order: both `_collect_facts` and `_process_settings` now `random.shuffle` their
  read keys each cycle so a marginal link does not keep losing the same trailing observations.
  Writes remain ordered; only independent reads are shuffled.
- Process note: implementation was written before the tests this time (not TDD). The existing
  cycle tests needed updating anyway because the probe prepends one `ver` send to every sync;
  three new probe tests were added (skip+offline, single-miss retry, ver-reuse). 568 tests pass.
- Open decision recorded for the user: skipped nodes are marked `OFFLINE`. If a skip should
  instead preserve the prior state, that is a one-line change.

### Luna live verification (0.9.0)

- OFFLINE-skip path confirmed on real hardware, both ways:
  - Forced: `mch sync qh-test` (bench node, `via_radio` 915.800/250/SF11) ran the two probes and
    printed `node unreachable - skipping sync this cycle` instead of timing out on every fact and
    setting. Reproduced across ~6 forced runs over qh-test and qhtest02 (SF10).
  - Scheduled: the daemon's own cycle logged `node ... did not answer 2 reachability probes -
    skipping sync this cycle` for qh-test and qhtest01a with no action from me - normal operation.
- Probe limitation observed and worth keeping: `🦷 QH Paterson` (field node, gateway home freq,
  cached path `flood`) did NOT skip. It answered the probe, then the full settings pass logged
  `read-failed:10` (`no_event_received`) and finished `facts:0 ... read-failed:10`. The probe is a
  single point-in-time check: a node that answers `ver` once and then goes silent still incurs the
  per-read timeouts. The probe reduces the dead-node case, it does not eliminate the
  flaky-mid-pass case. The backlog stored-path/flood-fallback and per-node reliability items are
  the deeper fix.
- SUCCESS path (probe answers, `ver` reply reused, shuffled reads) was NOT cleanly observed live:
  no node both answered the probe and completed a read this session (bench nodes down/out of
  range; field nodes flood-routed and dropping directed reads). It stays covered by unit tests
  (`test_probe_ver_reply_reused_as_fact_no_second_read` + shuffle assertions). Do not claim the
  reuse/shuffle path is live-verified until a node actually completes a probed sync.
- Unrelated pre-existing issue seen during the restart: config-reload-during-shutdown raised
  `GatewayError: get_contacts failed: timeout waiting for contacts` from `_watch_config_files` ->
  `_reload_config` -> `refresh_config_state`. Not caused by the probe; noting it as a separate
  shutdown-race cleanup candidate.
- Process honesty note: an earlier attempt to record live evidence (commit message "Record 0.9.0
  luna live verification") was cancelled mid-batch by an unrelated ssh failure, so no wrong entry
  ever landed; a follow-up "correction" commit referenced a prior wrong entry that did not exist.
  This block is the single accurate record. Lesson: do not write live-evidence notes before the
  live run actually completes.

## 2026-05-26 - Fact Name Validation

- Field status output exposed a typo configured as a fact: `powersavings` repeatedly returned
  `??: powersavings`, while the intended readable setting is `powersaving`.
- `mch validate` now warns for fact names that are not registered readable facts/settings and
  suggests a close match where available. The check is intentionally non-fatal: MeshCore
  firmware may add useful read keys before mch metadata is updated.
- Registered readable settings such as `powersaving`, `lat`, and `lon` remain valid facts when an
  operator wants observation without desired-state enforcement.

## 2026-05-26 - Management Route Visibility Without More Mesh Traffic

- After luna showed intermittent command success across consecutive foreground syncs, the next
  step is observation rather than an automatic unreachable-node shortcut.
- Foreground node sync now prints the effective management radio and the cached gateway contact
  route mode (`direct`, `flood`, or stored path metadata) before reading from the node. The daemon
  log records the same context.
- This uses the contact and active radio information already held by the gateway. It sends no
  added mesh traffic and does not claim that a cached route currently reaches the node.
- Clarified the testing boundary: the USB-attached node on luna is a same-room bench target, so
  deliberate verification traffic is expected there. Conserving radio traffic applies to
  unattended syncs against remote, low-powered field nodes.
- The safe testing distinction is also frequency-specific: current luna configuration has
  test-labelled nodes on `via_radio: 915.800,...`; avoid discretionary test traffic on the
  `915.075` field mesh channel.
- Captured a future design direction: accumulate per-node reliability evidence from command
  success/timeouts and partial runs, with path length/history and optional `lat`/`lon` distance as
  contextual factors. Distance alone must not be treated as proof of fragility.

## 2026-05-26 - Luna Manual Sync Activity Testing

- Reconfirmed the original design brief: mch is intended to bring unreliable, low-bandwidth,
  low-powered MeshCore nodes gradually into desired state, similar to a constrained-network
  configuration manager. It must preserve stale-but-useful evidence, expose recurring versus
  intermittent failure patterns, and keep unnecessary mesh traffic low.
- Luna testing of `0.8.3` confirmed that foreground `mch sync <node>` streams normal slow mesh
  work by default and that Ctrl-C returns control promptly during an in-progress read.
- The activity history exposed a missing audit boundary: it showed per-command timeouts and
  successful writes, but did not show which entries belonged to an operator-triggered sync or
  that a later foreground sync was deliberately aborted.
- Manual full-cycle and single-node syncs now record start, completion, failure, and abort entries
  in activity history. Scheduled cycles remain distinguishable by not emitting manual-run markers.
- The same slow-network assumption applies to writes: ordinary desired-state enforcement now
  consumes command-registry timeout metadata, so `set radio` receives the calibrated 180-second
  timeout previously applied only during commissioning.
- Added a project workflow rule requiring operational constants and safety decisions to retain
  their rationale and supporting evidence in the journal during investigation and in design notes
  once stable. The 180-second timeout is the immediate example: it remains traceable to live
  commissioning evidence rather than becoming an unexplained constant.
- Luna testing of `0.8.4` confirmed the new activity boundaries: a completed sync with four
  enforced setting-read failures logged `manual sync completed: partial result`; an interrupted
  foreground sync logged `sync interrupted before completion` followed by `manual sync aborted`;
  and `mch sync` logged the fleet-level `manual sync started: full cycle` marker.
- In consecutive foreground syncs to the same node, `ver` timed out in one run and responded in
  the next. A future read-only reachability gate must therefore treat one missing reply as
  inconclusive and use a small bounded retry/fallback strategy before skipping the remainder of a
  sync.

## 2026-05-26 - Slow Mesh Operations And Cancellation

- Treat slow MeshCore command replies as ordinary operating conditions, not an exceptional verbose
  diagnostic case. A foreground `mch sync <node>` now always streams per-step progress; the
  optional `--verbose` mode is removed.
- Client disconnect during a foreground sync must cancel the daemon-side sync even while it is
  awaiting a slow mesh reply. This releases the gateway operation lock and runs cleanup rather
  than allowing an invisible sync to continue after Ctrl-C.
- Daemon shutdown now cancels and awaits all active gateway-owning operations, including manual
  and foreground work as well as scheduled work, before the gateway is disconnected. A sync that
  temporarily changed radio settings unwinds through radio restoration first.

## 2026-05-26 - Backlog Triage And Coding Rate Semantics

- Cleaned the backlog so it contains unfinished work and open decisions rather than repeating
  release history or retaining raw terminal transcripts. Shipped behavior remains recorded in
  `CHANGELOG.md`; stable radio semantics are recorded in `DESIGN.md`.
- Added a project workflow rule: do not delete or over-compress untriaged backlog material until
  each distinct item has been classified and preserved in backlog, journal/design, changelog, or
  an explicit rejection.
- Corrected an implementation overreach found during review: coding rate must be shown when a
  complete radio configuration is transmitted or restored, but a CR-only difference is not a
  reason to switch the gateway for reachability. Frequency, bandwidth, and spreading factor
  determine whether the switch is needed.
- Kept `via_radio.cr` required for now because replacement commissioning currently consumes
  `via_radio` as a complete `set radio freq,bw,sf,cr` value. Making CR optional requires a clear
  source for replacement transmit configuration.

## Preserved Hardware-Test Notes

- Heltec V3 on v1.15-era firmware returned `OK - reboot to apply` for `set radio`, including
  after a temporary-radio command targeting the same values. `get radio` continued to report the
  persistent value with its firmware float representation. This confirms the documented reply,
  but does not resolve whether over-mesh communications can continue without reboot while runtime
  temporary settings match the newly persisted target.
- Reports that `tempradio` followed by `set radio` persists without immediate reboot remain
  unresolved until tested by observing over-mesh active-radio behavior.
- Bench tests found hardware-dependent clock behavior: plain reboot preserved a synced clock on
  Heltec V3, while `clkreboot` reset it; on a tested Seeed/nRF-style repeater, reboot paths did
  not preserve a corrected clock. This is why normal commissioning should not switch from
  `reboot` to `clkreboot` without device-specific evidence.
- Region command behavior remains version-specific: newer v1.15-era testing indicated explicit
  `region allowf` should not be assumed necessary. Any region automation still requires testing
  against supported firmware variants.

## 2026-05-25 - Fleet Report API Foundation

- Released as `0.7.0`; exposing a new HTTP API and central reporting contract is a minor-version
  capability change, not an invisible patch to `0.6.1`.
- Added a single normalized read-only fleet/node report model with `schema_version: 1`.
- The model classifies node health, setting status (`ok`, `nominal`, `drift`, `unknown`,
  `ignored`) and next action (`none`, `read`, `write`, `report`, `ignore`), retaining collected
  facts such as `lat` and `lon` for later map presentation.
- Existing status, plan, node-detail, and issues RPC payloads are compatibility projections from
  the model; display-only CLI `status` and `plan` now request the fleet model directly.
- Added an optional HTTP JSON transport for read-only local consumers. It binds only to loopback
  addresses until authentication is designed and includes no write endpoints.
- Revised the public HTTP route vocabulary to match the existing read-only CLI commands:
  `/health`, `/status`, `/status/<node>`, `/issues`, `/plan`, and `/logs`. Removed the draft
  `/api/v1/...` and `/healthz` paths because the API is still under active development.
- Field use exposed the next API refinement: `/status` should become a small, pollable
  dashboard/map summary, including health and coordinates, while `/status/<node>` supplies
  clickable full details and `/logs` remains separately refreshed.
- Retained the YAML/secrets/observed-DB ownership boundary for future UI work. API-driven clients
  do not require storing credentials or private identities in SQLite; future configuration
  editing must provide authentication, reviewable diffs, auditing, and explicit protected
  import/export handling.
- Clarified that collected settings are factual observations but remain distinct from facts in
  the model because they also carry desired values, policy, drift classification, and actions.
- Identified avoidable mesh load when the same key is configured as both a fact and a setting:
  the current sync path reads it in both passes. Future work should read once per cycle and fan
  that observation out to both semantic views.
- Captured the future messenger direction: explicit send/receive configuration, channel
  selection, and operator-reviewed pending sender requests without confusing DM authorization
  with managed-node login permissions.

## 2026-05-25 - Verbose Forced Sync And Operational Logs

- Released as `0.7.1`: `mch sync --verbose <node>` streams per-command progress while a
  foreground sync waits on slow or unreachable mesh responses.
- Fact and setting read failures are now persisted as node activity so `mch logs <node>` explains
  a partial sync without requiring access to `journalctl`. The journal remains the daemon/service
  diagnostic stream; systemd process state is intentionally not mixed into mesh status.
- `TRIES` means enforced write attempts. A setting read timeout never reaches comparison/write
  and correctly does not increment that value.
- The observed `mch contacts acai` miss was not a case-sensitivity defect: contact-name matching
  already ignores case, but `acai` is not a contiguous substring of `Acacia`.
- Kept a full messaging inbox outside project scope; messenger work remains focused on operational
  commands, notifications, and explicit authorization review.
- Added `CHANGELOG.md`, reconstructed from identifiable version-bump commits back to `0.1.0`, so
  release-facing history is kept separately from design investigation notes.
- Confirmed management radio behavior while diagnosing field timeouts: mchd uses the gateway's
  current contact route by default and only changes gateway radio for nodes configured with
  `via_radio:`. Automatic probing of historical radios is not implemented.
- Found a follow-up bug from the same trace: ordinary setting enforcement does not yet consume
  the command registry's slow-write timeout for `set radio`, even though commissioning does.
- Corrected terminology after operator feedback and review of the upstream MeshCore FAQ:
  MeshCore hops/paths are repeater routing concepts; changing the gateway's radio tuple is a
  temporary radio switch, not a hop. Runtime/log wording and documentation should use that
  distinction. Released this correction as `0.7.2`.
- Released `0.8.0`: replaced ambiguous `via:` with `via_radio:` and allowed a group's
  `via_radio:` to supply the reachability radio for member nodes unless overridden per node.
- After deploying `0.8.0`, read live contact routing fields from `luna`. Both `qh-paterson`
  (`cde6cd06be51`) and `acacia` (`4d0385b9d942`) reported no stored path with
  `out_path_len=-1`, meaning they were already flood-routed when management attempts failed.
  `qh-test`, `qhtest01`, and `qhtest02` reported empty `out_path` with `out_path_len=0`,
  representing direct/zero-hop routing. A stored-path reset would not by itself recover the two
  field failures; radio reachability, node availability, or broader flood delivery remains to be
  investigated.
- Released `0.8.1`: `global.via_radio` now supplies a fleet-wide reachability fallback, after
  node and group overrides. Recorded the operator naming convention that `LABEL` is local while
  `MESH NAME` is the device-advertised name, and planned bounded successful-path history because
  field knowledge contained working repeater paths no longer present in the gateway contact data.
- Deploying `0.8.1` exposed a shutdown problem: systemd stopped `mchd` during a sync after the
  gateway switched to `915.800/250/SF11`, but the process remained in command timeouts until
  systemd's 90-second stop deadline. The restarted daemon detected that radio and restored its
  configured home tuple `915.075/125/SF9`; prompt cancellation and restore still need fixing.
- Operator-reported known working routes were Acacia Gardens via `e260,5f6c,68be` and QH
  Paterson via `e260,5f6c`, despite both current gateway contact entries being flood-routed.
  This supports bounded successful-path history rather than treating the current contact path as
  the only useful recovery candidate.

## 2026-05-24 - Status view improvements and passive last_seen

**`clock` bare-command fix**: `_collect_facts()` was sending `get clock` instead of bare
`clock`, causing nodes to return `??: clock`. Added `"clock"` to the bare-command tuple
alongside `ver`, `board`, and `neighbors`.

**last_seen vs last_connected now distinct**: `last_seen` updates passively via an
`ADVERTISEMENT` event subscription set up at daemon startup — any time a managed node
broadcasts an advert on the mesh, its `last_seen` is updated immediately without waiting
for the next sync. `last_connected` still only updates when the work queue successfully
exchanges commands with the node. Both fields now show as full datetime + relative time
in the node detail view (`2026-05-24 00:22:00 (  2m ago)`).

**Settings deception fixed**: `mch status <node>` detail view now merges desired config
settings (from both node and group config) with DB observations. New settings added to
config show immediately with their desired values and `-` for observed — clearly pending
rather than invisible. Previously, only settings that had been synced at least once
appeared, making `state: configured` look complete when new config had never been applied.

**Settings table column widths**: now computed from content display width using
`_wlen()` / `_wpad()` (via `unicodedata.east_asian_width`), so emoji-containing values
(e.g. `🦷 Quakers Hill`) no longer push subsequent columns out of alignment. The drift
indicator column header `~` is only shown when at least one row is actually drifted.

**Radio debug logging**: added `log.debug("set_radio: ...")` before every
`mc.commands.set_radio()` call in `switch_radio_temporarily`, `restore_radio`, and `set_radio`
in `gateway.py`. Visible at DEBUG log level without cluttering production INFO logs.

## 2026-05-23 - Clock sync, settings vs operations, and powersaving fix

**Clock sync mechanism confirmed**: `send_cmd` (meshcore_py) embeds `int(time.time())` from the
host in every command packet. When a managed node receives `clock sync` over the mesh, it can use
that embedded timestamp to set its clock — so time accuracy comes from the Mac's NTP clock, not
the gateway node's own clock. The `clock sync` operation in `config.yaml` (every 6 hours) is the
correct approach.

**Settings vs operations**: Settings use `get <key>` / `set <key> <value>` with drift detection;
operations are fire-and-forget scheduled commands. Region commands (`region put`, `region save`)
must be operations because there is no `get region` read-back. `clock sync` and `reboot` are
genuine operations. See DESIGN.md for the full explanation.

**powersaving bug found and fixed**: `powersaving` as a setting was sending `get powersaving`
(read) and `set powersaving on` (write) — both wrong. The firmware CLI uses bare `powersaving`
to read (returns "on"/"off") and `powersaving on` / `powersaving off` to write. Fixed via
`_BARE_COMMAND_KEYS` in `queue.py`; the set is extensible as more non-standard keys are found.

**clkreboot open question**: commissioning ends with bare `reboot`. `clkreboot` would sync the
clock before rebooting. Unknown whether MeshCore persists time to flash across a reboot. Needs
live bench test — see BACKLOG Research section.

## 2026-05-23 - Bench Investigation of the set prv.key Timeout

After shipping the new-mode fallback, the next live commission failed at the same
point: `password walter76` replied OK, `set prv.key <real key>` timed out with no
reply after 30s. The fallback recovered the auth on retry, but each retry hit the
exact same wall — the wizard was now stuck in a loop where the fallback worked
correctly but couldn't get past `set prv.key`.

Built `scripts/bench_session_invalidation.py` to test hypotheses against the
USB-attached bricked node from luna's gateway over the mesh, with mchd stopped so
the script owned the serial port. Every step prints `[SEND]` / `[REPLY]` /
`[NO REPLY]` so the bench tape is unambiguous. The script never sends `reboot`,
so any staged radio/key changes never apply.

### Run 1: hypothesis "password change invalidates the admin session"

Phases: factory login → `set name PHASE_A` → `password <new>` → `set name PHASE_B`
(no re-login) → re-login → `set name PHASE_C`. Result: A, B, and C all replied
`OK`. The session is **not** invalidated by `password <new>`. Hypothesis refuted.

### Run 2: hypothesis "`set prv.key` is the slow command"

Refocused Phases: factory login → `set name PHASE_A` (control) → `set prv.key
<junk>` (90s timeout) → `password <new>` → re-login → `set prv.key <junk>` (90s).

Results:
- Phase A: `set name PHASE_A` → `OK` (instant).
- Phase B: `set prv.key 1111...2222...` → **`'Error, bad key'`** (instant).
- Phase C: same junk key after password change → **`'Error, bad key'`** (instant).

Two key inferences:
- The firmware *validates the scalar clamping* on the private key. Our junk had
  bit 0 set in the first byte, violating the standard curve25519 `&= 248` clamp,
  so the firmware rejected it instantly with `'Error, bad key'`. `src/mch/meshkey.py`
  produces properly clamped keys, so real wizard-generated keys pass validation.
- A *validated* `set prv.key` doesn't just respond fast and lazily defer work.
  The processing the firmware does for a valid key (probably curve25519 scalar
  multiplication on the ESP32 to derive the new public key, plus flash writes)
  takes long enough that the response misses the 30s timeout in
  `gateway.send_command` (`src/mch/gateway.py`, line 353).

Side observation from Run 2: the post-password re-login failed
(`login failed for bbcde403cef3: None`), but Phase C still got an admin reply
without it. Consistent with the existing model in `CLAUDE.md`: after first login
the gateway pubkey is on the node's ACL and subsequent commands work without
re-authentication.

### Conclusion and next step

The bug is not session-related. The bug is that the default 30s timeout is too
short for `set prv.key` (and almost certainly `set radio`, which failed the same
way on 2026-05-22). Next: extend the bench with Phase D that sends a *real*
generated private key with a long timeout so we can measure the actual response
time, then ship a calibrated per-command timeout in commissioning.

### Run 3: Phase D with a real generated key (180s timeout)

Sent a fresh `generate_private_key()` value with a 180s timeout. The firmware
replied within the window with:
`OK, reboot to apply! New pubkey: <hex>`. The pubkey matched the one derived
locally from the same private key, so `src/mch/meshkey.py` produces the format
the firmware expects and our `derive_public_key` agrees with the firmware's
own derivation. The response was real, not lost — the 30s default timeout was
just too short for the firmware to finish its work (curve25519 scalar mult to
derive the new public key + flash write to persist the staged identity).

### Fix

Per-command timeout in `_send_commission_command`: `set prv.key` and `set radio`
get 180s; everything else keeps the 30s default. Targeted and reversible.

### Reference: firmware source for `set prv.key`

Handler at `src/helpers/CommonCLI.cpp` (MeshCore main, ~line 710):

```cpp
} else if (memcmp(config, "prv.key ", 8) == 0) {
  uint8_t prv_key[PRV_KEY_SIZE];
  bool success = mesh::Utils::fromHex(prv_key, PRV_KEY_SIZE, &config[8]);
  if (success && mesh::LocalIdentity::validatePrivateKey(prv_key)) {
    mesh::LocalIdentity new_id;
    new_id.readFrom(prv_key, PRV_KEY_SIZE);
    _callbacks->saveIdentity(new_id);
    strcpy(reply, "OK, reboot to apply! New pubkey: ");
    mesh::Utils::toHex(&reply[33], new_id.pub_key, PUB_KEY_SIZE);
  } else {
    strcpy(reply, "Error, bad key");
  }
}
```

`validatePrivateKey` is what rejects malformed input fast; `readFrom` does the
expensive public-key derivation; `saveIdentity` writes to flash. Then the reply
goes out — the reply is always sent, it just takes time. There is no remote-vs-
serial gate on `set prv.key` (only `get prv.key` is serial-only).

### Live confirmation (0.1.2)

After deploying 0.1.2 with the 180s per-command timeout, `mch commission` on
luna completed end-to-end against the bricked bench node:

- `setting name`, `setting guest password`, `setting password` → all fast.
- `setting generated private key` → completed within 180s (previously timed out at 30s).
- `setting radio` → completed within 180s.
- `reboot` → fire-and-forget.
- `verifying identity (2/3)` → new pubkey visible after the second post-reboot scan.

Direct USB check after reboot: live pubkey, radio, and name all matched the
wizard's snippet. The bricked node is now fully commissioned. The "set prv.key
times out" failure mode is closed for v1.15-era firmware.

### Live confirmation: `commission replace` (same session)

To validate `_login_for_replace`, the just-commissioned `81d6bf19` was erased
via USB (`meshcore-cli -r -s /dev/cu.usbserial-0001 erase`, "Erase filesystem
(serial only)") and re-discovered as a factory-fresh node with a new pubkey
`c26d49f6d122...`. The operator pasted the original `81d6bf19` config/secrets
snippet into luna's config files, reloaded mchd, and ran `mch commission` →
replace → selected `81d6bf19`. The wizard hit the same 180s flow for the
slow commands; direct USB check after reboot confirmed the original pubkey
restored from the secrets `prv_key`.

### Side observations from the live runs

- **Wizard's `group: default` was rejected by hot-reload.** Luna's `config.yaml`
  defines `QH` only. Tracked in `BACKLOG.md` — wizard should read live config
  and offer existing groups.
- **`mch validate` defaults to `~/config.yaml` instead of the mchd path.** Real
  bug — caught the file divergence here. Tracked.
- **`busy: scheduled sync` UX during commission** — scheduled sync of the just-
  added node 81d6bf19 grabbed the operation lock; commission had to retry
  several times before the sync window closed. Tracked.
- **Duplicate label spotted via `mch status`** — `2fd9c7bbc93d qh-wold1`
  (configured) and `1fd9c7bb qh-wold1` (ghost) share the same label. Loader
  doesn't flag this. Tracked.
- **Post-replace name regression to "Heltec Repeater".** Replace mode does not
  send `set name`; on identity change the firmware appears to fall back to a
  hardware-default name. Tracked — decision pending whether replace should
  always push `set name <label>`.

## 2026-05-23 - New-Mode Commission Fallback to User Password

- After shipping the replace-mode fallback (entry below), a live `commission new` run on the
  same bricked node hit the same class of bug at `set prv.key`: the password change just
  before it appears to invalidate the admin session on v1.15-era firmware, and the next
  command times out waiting for a response.
- Verified by raw serial (`meshcore-cli -r -s /dev/cu.usbserial-0001 get freq` etc.) that
  the node was still on factory frequency and factory pubkey after the timeout — i.e. the
  state-changing radio/key writes had not been applied (because they only take effect on
  reboot, which never happened). Only the admin password had actually changed.
- Extended the fallback to new mode the same way as replace: factory password first, then
  the user-supplied admin password from the wizard form. The CLI generates `prv_key` once
  before the retry loop (`cli.py:1129`) and resends the same value, so the originally
  feared "operator loses the daemon-generated prv_key" failure mode is mitigated for the
  common in-session retry case. It can still bite if the wizard itself is killed mid-run.
- The operator's framing is the right one: the mesh is fragile by nature, so partial-state
  recovery is the *purpose* of the tool, not an edge case. Future work should keep this
  bias: prefer idempotent commands and fallback strategies over assuming a clean baseline.

### Bench observation (v1.15.0-dee3e26 on Heltec V3)

- Symptom: `password <new>` returns OK, the very next CLI command sent over the mesh times
  out with no response.
- Reproduced on both `set radio` (replace mode, 2026-05-22) and `set prv.key` (new mode,
  2026-05-23). Same firmware build.
- Hypothesis: the password change closes the current admin session in some firmware paths;
  the next command runs without admin auth and is dropped silently.
- Mitigation in current code: keep password/identity changes late in the command list and
  rely on idempotent retry-with-fallback rather than ordering tricks. Direct serial reset
  is the bench escape hatch when both fallbacks are exhausted.

## 2026-05-23 - Replacement Commission Retry After Partial Failure

- On 2026-05-22 a replacement commission on Heltec V3 / MeshCore v1.15 changed the admin
  password and private key, then timed out waiting for `set radio`. The next attempt could
  not log in because the factory password was already gone.
- The fix is stateless: on factory-login failure in replace mode, try the managed password
  from `secrets.yaml`. The mutation commands (`set guest.password`, `password`,
  `set prv.key`, `set radio`) are idempotent for the same target values, so re-sending the
  whole sequence is safe.
- Login is now per-mode, not shared in `handle_commission`. `_commission_new` keeps the
  factory-only path; `_commission_replace` uses `_login_for_replace` which announces the
  fallback via the progress callback so the operator sees what happened.

## 2026-05-21 - Commissioning Identity Model

- `mch commission new` should not keep a factory-generated node identity.
- Mesh Herder should generate a MeshCore-compatible private key locally, derive the public key,
  send `set prv.key <generated-key>` during commissioning, and print:
  - `config.yaml`: derived `pubkey_prefix`.
  - `secrets.yaml`: generated `prv_key`.
- This avoids later USB-only private-key retrieval and makes replacement mode viable.
- Vanity public-key generation is parked until normal commissioning and replacement are stable.

## 2026-05-21 - Commissioning Command Ordering

- Live tests on Heltec V3 / MeshCore v1.15-era firmware showed that session-invalidating changes
  should be sent late in the commissioning flow.
- Commissioning now treats CLI-looking error responses as failures and redacts passwords/private
  keys from errors.
- `set radio` returns a normal response indicating that reboot is needed; it should not be
  fire-and-forget.
- The final `reboot` remains fire-and-forget.

## 2026-05-21 - Commissioning Verification

- `set prv.key` can appear to succeed before the new identity is visible over the mesh.
- `commission new` verifies the expected generated public key after reboot/radio change before
  returning success.
- Discovery verification needs retry logic because the rebooted node may not be visible
  immediately and another node on the mesh can answer first.

## 2026-05-22 - Fresh Flash Commission Success

- Fresh-flash commissioning was live-tested end-to-end on Heltec V3 / MeshCore v1.15-era firmware.
- The successful flow:
  - Discover factory node.
  - Login with factory credentials.
  - Set normal node settings.
  - Set admin password and generated private key late.
  - Set target radio, reboot, and verify generated identity after retry.
- The resulting snippets are printed for operator review rather than written by the daemon.

## 2026-05-22 - Post-Commission Documentation Decisions

- `config.yaml` and `secrets.yaml` remain operator-managed source-of-truth files.
- `mch commission new` prints paste-ready snippets instead of editing files automatically.
- A separate `mch reset` command is not needed yet; bench reflash commands can stay as
  development documentation.
- Prefer `mch status <node>` for node detail views. Treat `mch node <node>` wording as legacy.

## 2026-05-22 - Commission Wizard UX Notes

- `mch commission` works, but the copy needs a pass before wider use.
- Candidate CLI shape:
  - Keep bare `mch commission` as the interactive wizard.
  - Add explicit subcommands later: `mch commission new` and `mch commission replace`.
- Suggested wording:
  - "New node" -> "Commission fresh node".
  - "Replace node" -> "Restore existing identity".
  - Use explicit sentinel prompts such as `Group label [- for none]`.
  - After successful commissioning, say: add snippets, restart/reload `mchd`, then run
    `mch status <prefix>` to inspect or `mch sync <prefix>` to force convergence.

## 2026-05-22 - Config Reload Direction

- YAML remains desired state.
- SQLite remains observed/cache/history state.
- `mchd` detects YAML file changes and reloads desired state without an operator restart.
- On valid reload, the daemon updates the in-memory config, messenger settings, gateway contacts,
  and DB label/group metadata.
- On invalid YAML or validation failure, the daemon logs the error and keeps the last valid config.

## 2026-05-22 - Name Display After Sync

- `LABEL` is local-only desired metadata from `config.yaml`.
- `MESH NAME` is observed MeshCore state from the node/contact store.
- A config edit alone should not change the observed mesh name; a successful sync must apply
  `settings.name.desired`.

## 2026-05-22 - MeshCore CLI References

- Keep these references handy when validating command semantics:
  - https://docs.meshcore.io/cli_commands/
  - https://github.com/meshcore-dev/MeshCore/wiki/Repeater-&-Room-Server-CLI-Reference/83961784b86ef23a55ca7cf621bd9381efa9f535
- Important units/details confirmed from the wiki reference:
  - `set advert.interval {minutes}`
  - `set flood.advert.interval {hours}`
  - `set radio {freq},{bw},{sf},{cr}` requires reboot to apply
  - `region put` and `region save` are raw region-management commands, not `set <key> <value>`
    settings
  - For MeshCore v1.15.0 and newer, `region allowf` is no longer required after `region put`
- After successful `set name`, the DB node name is updated immediately to avoid stale display.

## 2026-05-22 - Desired vs Known Safety Model

- YAML is desired state.
- SQLite is known state: observations, command results, history, and schedule.
- The default policy is `audit`, not `enforce`; writes require explicit `policy: enforce` or
  `default_policy: enforce`.
- `mch plan` provides a read-only desired-vs-known view without touching the mesh.

## 2026-05-22 - Config File Write Access Removed

- `mchd` does not write `config.yaml` or `secrets.yaml`.
- Commissioning returns paste-ready snippets only.
- The systemd unit grants write access to runtime/database paths, not the operator-owned config
  directory.

## 2026-05-22 - Plan Command

- Added `mch plan` as a read-only desired-vs-known view.
- It uses the daemon's loaded config/secrets and known DB values.
- Output groups:
  - Planned writes: explicit/enherited `enforce`, known drift, concrete next `set` command.
  - Audit drift: known drift under `audit`, explicitly no write.
  - Needs observation: desired values where the DB has no known value yet.
