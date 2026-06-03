# Mesh Herder Backlog

This is the working backlog for unfinished work and open decisions. Keep released feature history
in `CHANGELOG.md`, dated investigation notes in `JOURNAL.md`, and stable behavior in `DESIGN.md`.

## Working Rules

- Put operator-impacting bugs and safety issues ahead of feature ideas.
- Keep an item here only when its operator benefit or unresolved decision is clear.
- Keep speculative ideas short until they are selected for implementation.
- Move completed work to **Done Recently**, then remove it once it is represented in release notes.

## Next

### Operation Safety And Reachability

- **Proactively log in to auth-required nodes that silently drop unauthenticated commands.**
  (Confirmed root cause of the faulkland "unreachable" saga - see JOURNAL 2026-06-03 RESOLVED.)
  faulkland (v1.15.0) drops a directed command from a peer not in its ACL with no reply and no
  permission error, so the bare-`ver` reachability probe times out and the node is mislabeled
  unreachable. `_send_authed` only re-authenticates on an *explicit* permission error, which never
  arrives. Fix: when a node has a password configured and is not `auth_cached`, attempt login
  before/instead of relying on the bare probe, so the gateway enters the node ACL at least once.
  `mch probe --login` already exercises the gateway side. Decide where this slots into the
  reachability-probe/sync flow and how it interacts with the offline/skip decision.
- Consider an anonymous request (`SEND_ANON_REQ`, owner/basic) as the reachability/presence probe
  instead of (or before) the bare `ver`. It returns clock/name/flags **without login or ACL
  membership**, so it cleanly separates "node present but gateway not in its ACL" from "truly
  unreachable" - the exact ambiguity that misdiagnosed faulkland - and yields the remote clock for
  free. `tools/query_node_info.py` already implements the wire format; it needs lifting into the
  Gateway class. Note firmware exposes no anonymous version, so `ver` as a *fact* still needs
  login/guest. Pairs with the proactive-login item above.
- Fix the 0.9.35 "radio confirmed -> skip recovery" optimization for nodes on a non-home radio
  without `via_radio`. The skip assumes the just-failed probe was sent on the node's radio, so a
  failure must be a path issue. But with no `via_radio`, the probe goes out on the gateway *home*
  radio, which cannot reach a node on a different frequency/bandwidth at all (e.g. faulkland on
  915.800 vs home 915.075). Once such a node's observed radio matches desired, the skip fires, the
  hunt is never run, and the node is wrongly marked unreachable - a steady-state strand. Either
  require `via_radio` for off-home-radio nodes (and document it), or make the skip aware of whether
  the failed probe actually used the node's desired radio (home == desired, or via_radio == desired)
  before concluding "path, not radio".
- Make daemon startup tolerate a `get_contacts` failure instead of wedging. After the 0.9.34
  timeout bound, a `get_contacts` timeout at work-queue startup (`queue.py`) now raises out of the
  work-queue task and leaves the daemon half-alive: control socket up, work queue dead, every
  command hanging the full client timeout. It should retry/back off, or fail loudly and exit so
  systemd restarts cleanly. (Observed 2026-06-03 when a serial lockup made `get_contacts` time out
  on restart; on a healthy gateway `get_contacts` is <1s, so this is about resilience, not the
  contact-list size.)
- Store a small set of candidate management paths per node with success/failure counts or
  percentages. Normal sync should try the current stored path first, then fall back to a known
  strong recent path before using flood as a bounded recovery signal. The recovery command is
  currently `ver`, but should be explicit/configurable if a better low-cost probe emerges.
- Build a per-node reliability/fragility view from collected sync evidence: rolling command
  success/failure and timeout rates first, then assess path length/history and optional physical
  distance as explanatory signals. Use it to inform scheduling/backoff only after the evidence
  model is visible and reviewable.
- Allow an operator-requested `commission` or `exec` to preempt scheduled sync at a safe
  between-command boundary rather than repeatedly returning `busy`.
- Make daemon shutdown cancel or bound in-flight gateway calls cleanly. During the 0.9.29 luna
  deploy, `systemctl restart mchd` remained in `deactivating` after a config reload hit
  `get_contacts` timeout and required systemd SIGKILL before the new daemon could start.
  Partially addressed (Unreleased): `Gateway.get_contacts` is now bounded by a timeout, which
  removes the specific unbounded await that caused the observed hang. Residual hardening still
  open: (a) the SIGHUP-triggered `_reload_config` runs as a detached task that `_shutdown` never
  tracks/cancels (daemon.py); (b) other gateway round-trips (e.g. `send_command` paths) rely on
  per-call timeouts rather than shutdown-driven cancellation. Decide whether `_shutdown` should
  cancel/await the reload task and bound the remaining gateway calls.
- Investigate safe remediation for fast node clocks. Live Wold testing showed `clock sync` can
  return `ERR: clock cannot go backwards`, so the ordinary clock-sync operation only fixes slow
  clocks. Any fallback such as reboot/clkreboot must be board/firmware tested before automation.

### Configuration And Radio Semantics

- Extend `mch adopt` beyond the initial DB/contact seed: retain the discovery evidence used for
  adoption, expose adopted-radio/path caveats in status, and make follow-up path/radio repair
  steps explicit. (Partially done, Unreleased: adopt now installs a direct zero-hop route and runs
  a directed-`ver` confirm probe on the discovered radio. Still open: *persisting* the discovered
  reach-radio so scheduled sync can reach the node without a config `via_radio` - deferred until the
  routability keystone is confirmed on a real node, see ROADMAP.)
- Store discovered/adopted management radios in the DB. Active discovery on a non-home radio proves
  the node answered there, but config-only `via_radio` is too clumsy for ad hoc recovery and
  adoption; the DB should retain the radio evidence with source and timestamp.
- Live-test region management on representative firmware before recommending it for ordinary
  enforcement: `region put`, `region save`, `region home`, and `region default`; Heltec V3
  v1.15.0 has been checked, but the same read-back text and v1.15+ auto-allow/auto-save behavior
  still need confirmation across deployed boards.
- Keep desired node radio distinct from the radio used to reach it. Replacement commissioning
  currently consumes `via_radio` as the value to push with `set radio`; this becomes ambiguous
  if the node is being moved to a new radio.
- Decide whether `via_radio.cr` should be optional. A CR difference alone does not prevent
  reception, but commissioning currently needs a complete `freq,bw,sf,cr` transmit setting.
- Add `mch probe NODE --radio freq,bw,sf,cr` for reachability-only investigation. (Done,
  Unreleased: `mch probe NODE --radio ... [--advert]` switches temporarily, optionally sends a
  gateway zero-hop advert first, sends a directed `ver`, restores the radio, and reports the result.
  Still open: *recording* successful radios as known-good evidence - that is the known-good radio
  history item below.)
- Store known-good management radios in the DB with source, success/failure counts, and
  `last_success_at` / `last_failure_at`; use them as radio-recovery candidates after configured
  radios. Add an expiry or decay policy so stale bench/test evidence ages out naturally.
- Consider observed-radio recovery candidates from successful DB observations, especially `radio`
  setting reads, while excluding factory/commissioning defaults unless they succeeded for a
  managed node during a normal/probe path.
- Consider nearby bandwidth variants for radio recovery only after known-good radio history is in
  place; SF variants are already speculative enough for the current bounded hunt.
- Decide the node-name-prefix design. If `name_prefix` is added it should use Node > Group >
  Global precedence and respect the advertised-name byte limits: 32 bytes without location and
  24 with location. Do not add a suffix because truncation can create an enforcement loop.
- Expand field-aware validation and operator-facing setting descriptions where registry metadata
  is incomplete, including enumerated settings such as `path.hash.mode`.
- Investigate `get pwrmgt.support` across deployed boards and firmware. Decide whether it should
  become a collected fact, a capability annotation for powersaving behavior, or just a diagnostic
  command documented for manual investigation.
- Decide whether future command-registry metadata should remain Python-defined or move to a
  versioned data file while preserving fallback handling for unknown firmware commands.
- Decide where configurable stale/lost thresholds belong (`status` versus `scheduler`).

### CLI And Reporting

- Move toward DB-primary fleet inventory. Config should describe desired policy/defaults and remain
  exportable as a backup/import format, while DB owns discovered full pubkeys, labels, mesh names,
  observed radios, known paths, facts, and scheduling state.
- Add `mch export config` and related backup commands once DB-primary inventory exists. Exports
  should produce reviewable YAML, avoid lossy shorthand such as literal `...`, and distinguish
  inventory from desired policy.
- Decide how secrets fit a DB-primary model. Passwords and private keys probably stay in
  `secrets.yaml` or move to a protected/encrypted secrets store rather than ordinary DB tables;
  export/import must preserve replacement private keys without accidentally exposing them.
- Standardize pubkey display and input conventions: accept labels, unique prefixes, and full
  pubkeys; store full pubkeys in DB and gateway-contact operations; prefer 12-character display
  prefixes in tables; keep shorter prefixes acceptable in config only when unique.
- Allow `facts:` to be configured at global, group, and node scope with Node > Group > Global
  precedence. Current behavior is one fleet-wide fact list, so stale-fact cleanup can scope which
  nodes are cleaned but not yet compute different desired fact sets per group/node.
- Decide whether anonymous repeater probing should stay as a standalone `tools/` helper or grow
  into a supported `mch` command. If it becomes supported, add an optional guest-login path for
  firmware version via `REQ_TYPE_GET_OWNER_INFO` while keeping anonymous results clearly marked.
- Decide whether live sync progress should include timestamps now that it is always displayed for
  foreground node sync.
- Explain partial status more directly when enforced reads timed out after other reads succeeded;
  `TRIES` counts writes, so a failed read correctly does not increment it.
- Add an operator-controlled way to hide stale cached facts that are no longer configured, such
  as a misspelled `facts:` key (`powersavings`) that remains visible in node status after it is
  removed from configuration. Preserve observation history by default.
- Add an explicit stale-fact cleanup command for operators who want to delete unconfigured cached
  fact rows from the local database after reviewing them.
- Consider an opt-in Markdown/clipboard-friendly rendering mode for sharing reports, while
  retaining aligned terminal tables as the default.
- Persist "configured node not visible to gateway" warnings in `mch logs`, not only the journal.
- Make `mch suggest <node>` show differences from current configuration and consider geographic
  separation when suggesting settings for nearby nodes.

## Later

- Add management path diagnostics and recovery controls: store/display the selected path, retain
  bounded successful-path history, and provide an explicit path-reset operation.
- Split the fleet API summary response from full node detail before building a frequently polled
  dashboard or map.
- Deduplicate mesh reads when one key is requested both as a fact and a managed setting.
- Decide whether replacement commissioning should set the declared mesh name immediately and
  optionally perform a follow-up sync after reboot.
- Improve commissioning for location-aware nodes, including `lat`/`lon` prompts and correct
  32-byte versus 24-byte advertised-name limits; consider an operator-configured home-radius
  warning without rejecting intentional out-of-area deployments.
- Clarify generated-public-key wording and wizard sentinel values.
- Support replacing an archived secrets-only node and decide whether group defaults should
  populate new commissioning values, including treatment of guest passwords.
- Make profile updates explicit and validated instead of implicitly depending on remotely fetched
  recommendation data.
- Improve remote profile cache placement and validate downloaded content before replacing a
  known-good cached profile.
- Add concurrency coverage for scheduled sync, manual sync, `exec`, and commissioning ownership.
- Add compact per-sync outcome counters for contacts, writes attempted, and settings changed.
- Add named DB migrations, typed MeshCore contact data, and smaller ownership-focused modules as
  persistence and queue behavior grow.
- Reduce duplicated status/issue assembly and continue converging terminology on `mch status`
  rather than legacy node-detail command wording.
- Polish `mch validate` output ordering and revisit whether `mch remove` and `mch prune` should
  converge on one retired-node command vocabulary.

## Wishlist

- `mch ping`, `mch discover`, and an overall `mch health` command.
- Read-only dashboard/map based on the existing local API once reporting payloads are suitably
  compact.
- Group-aware commissioning defaults and safer recovery of an interrupted generated private key.
- Adopt an already deployed node into configuration without destructive recommissioning.
- Dynamic `txdelay`/`direct.txdelay` recommendations based on observed neighbour count.
- Consider a managed `owner.info` prefix only if there is a demonstrated use case; avoid suffix
  semantics that could reintroduce truncation/convergence ambiguity.
- Additional focused mesh messenger controls and notification workflows.
- DM-triggered sync and channel/room reporting using the existing authorization model.
- Mapping-style `nodes:` YAML, alternate config/secrets locations, and configurable label/name
  formats only after current file workflows become a demonstrated problem.
- Firmware-age reporting, Bluetooth transport, and a Docker image after core operations settle.
- Derived desired settings from observed facts, kept separate from ordinary static settings.
- OpenAPI and authenticated write-operation design only when a real UI/client requires it.
- Deliberate config export/import and protected secret backup workflow before any managed
  configuration store or browser-based configuration editing.

## Research / Live-Test

- Validate (and probably correct) the `txdelay` vs neighbour-count direction in `profiles.yaml`.
  The current presets use more neighbours -> *lower* txdelay (tower 20+ -> 0.3, mobile 0 -> 2.0).
  The MeshCore community discussion (https://github.com/meshcore-dev/MeshCore/discussions/2053)
  leans the *opposite* way: txdelay scales the random flood-retransmit backoff
  (`delay = random(0, 5*t+1)`, `t = airtime * txdelay`), so denser areas need *more* backoff to
  cut collisions, giving more neighbours -> *higher* txdelay (their table: 0->~0.05, 5->1.5,
  12->3.6, via `base + neighbours * 0.3`). This means profiles.yaml may be backwards. The thread
  is itself inconclusive: a simulator recommended txdelay=0 everywhere, but real-world testing
  showed zero delay made error rates worse. Required result: a defensible direction and formula
  validated against real delivery/collision behaviour before mch recommends txdelay confidently.
  Until then, treat `mch suggest` txdelay output as provisional. Note the CLI caps txdelay at 2.0
  (firmware 1.14.1+ may accept higher).
- Confirm optional-CR behavior for `via_radio`.
  - Current behavior: CR is displayed when a complete radio setting is transmitted/restored, but
    CR alone does not trigger a reachability switch.
  - Required test/design result: identify the CR source for replacement commissioning if
    reachability configuration is allowed to omit it.
- Test whether `tempradio <target>,<minutes>` followed by `set radio <target>` can persist a
  radio move without an immediate reboot over the mesh. Serial tests observed the documented
  `OK - reboot to apply` reply but cannot establish active over-mesh behavior.
- Keep `reboot` as the normal commissioning final step unless hardware evidence changes:
  `clkreboot` reset a corrected clock on tested Seeed/nRF-style and Heltec V3 nodes, while plain
  `reboot` preserved it on the Heltec V3.
- Confirm boolean setting vocabulary on representative firmware; configuration currently
  normalizes YAML boolean-style `powersaving` values to `on`/`off`.
- Confirm version-specific `region allowf` behavior before recommending legacy region workflows.

## Won't Do / Not Yet

- Do not auto-edit `config.yaml` or `secrets.yaml` from `mchd`; they remain operator-owned source
  files until a reviewed, audited configuration-write workflow exists.
- Do not store passwords or node private keys in the observational SQLite database without an
  explicit secret-storage and recovery design.
- Do not add a first-class reset command until reset becomes a normal operator workflow.
- Do not place dangerous identity/reset operations in ordinary desired-state enforcement.
- Do not turn mesh DM control into a general messaging client or enable writes by default.
- Do not add vanity key generation until ordinary commissioning and replacement are stable.
- Do not attempt to onboard a node that is both unknown to the gateway (no mesh contact / no route)
  and out of direct radio range. With no contact, no route, and no way for local discovery to
  reach it, there is nothing to work with. Onboarding is scoped to nodes that are either already
  routable via mesh contacts or in direct radio range (bench) so a route can be established.

## Done Recently

- 2026-06-01: Added explicit `mch path` recovery controls: `--discover` and `--advert` learn
  candidate paths without applying them, while `--apply <path[:mode]>` applies an
  operator-confirmed replacement path to the gateway contact and records the change in logs.
- 2026-06-01: Added `mch path <node>` for read-only cached management route inspection, including
  raw `out_path` metadata and explicit flood/missing `path repair needed` status.
- 2026-06-01: Added `mch admin` to stop `mchd`, run `meshcore-cli` against the configured gateway
  serial device/baud, and restart `mchd` after the admin session; avoided adding an alias to keep
  CLI vocabulary narrow.
- 2026-05-31: Added controlled flood-route fallback for enforced setting writes after a cached
  stored-path write failure: mch now probes with flood-routed `ver` before retrying the write via
  flood, and skips the retry if the fallback probe also fails.
- 2026-05-31: Released `0.9.0`. Added a reachability probe (bare `ver`, two attempts) before each
  node's full fact/setting pass: two misses skip the pass and mark the node offline rather than
  timing out on every read, while a single miss is retried. The probe reply is reused as the
  `ver` fact, and independent fact/setting reads are now issued in randomized order each cycle.
- 2026-05-26: Added non-fatal `mch validate` warnings and close-match suggestions for unknown
  `facts:` entries while keeping unregistered firmware reads available for deliberate use.
- 2026-05-26: Added foreground sync visibility for effective management radio and cached
  direct/flood/stored contact route, using already available gateway state without sending an
  extra mesh command.
- 2026-05-26: Applied command-registry write timeout metadata during ordinary enforcement, so
  slow `set radio` desired-state writes use the calibrated extended timeout already used during
  commissioning.
- 2026-05-26: Added activity boundaries for operator-triggered syncs: manual full cycles and
  single-node syncs record start, completion, failure, or abort so command results can be grouped
  with the manual run that caused them.
- 2026-05-26: Made foreground node sync progress unconditional and cancelled active
  gateway-owning work during shutdown or foreground client disconnect, so slow mesh waits no
  longer conceal work or strand a temporary gateway radio.
- 2026-05-26: Cleaned this backlog down to active work and open decisions; release history and
  lengthy field observations remain available in `CHANGELOG.md`, `JOURNAL.md`, and git history.
- 2026-05-26: Documented Node > Group > Global shared-setting precedence for ungrouped nodes in
  `examples/config.example.yaml`; `global.via_radio` behavior was already implemented.
- 2026-05-26: Added coding rate to human-facing gateway radio switch/restore output while
  preserving CR-independent reachability behavior, and added stop/restart wait feedback.
- 2026-05-25: Released `0.8.1`, adding `global.via_radio` as a fleet-wide fallback beneath node
  and group reachability-radio overrides.
- 2026-05-25: Released `0.8.0`, replacing per-node `via:` with explicit `via_radio:` and adding
  group-level inheritance for sync and replacement commissioning.
- 2026-05-25: Released `0.7.2`, correcting terminology so gateway radio switching is not called a
  MeshCore hop or path.
