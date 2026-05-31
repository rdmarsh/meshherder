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

- Extend the reachability probe (shipped 0.9.0) with controlled stored-path/flood fallback before
  writes when the probe succeeds but the node is on a marginal route. The probe currently only
  gates the full pass; it does not yet try alternate routing.
- Build a per-node reliability/fragility view from collected sync evidence: rolling command
  success/failure and timeout rates first, then assess path length/history and optional physical
  distance as explanatory signals. Use it to inform scheduling/backoff only after the evidence
  model is visible and reviewable.
- Allow an operator-requested `commission` or `exec` to preempt scheduled sync at a safe
  between-command boundary rather than repeatedly returning `busy`.

### Configuration And Radio Semantics

- Keep desired node radio distinct from the radio used to reach it. Replacement commissioning
  currently consumes `via_radio` as the value to push with `set radio`; this becomes ambiguous
  if the node is being moved to a new radio.
- Decide whether `via_radio.cr` should be optional. A CR difference alone does not prevent
  reception, but commissioning currently needs a complete `freq,bw,sf,cr` transmit setting.
- Decide the node-name-prefix design. If `name_prefix` is added it should use Node > Group >
  Global precedence and respect the advertised-name byte limits: 32 bytes without location and
  24 with location. Do not add a suffix because truncation can create an enforcement loop.
- Expand field-aware validation and operator-facing setting descriptions where registry metadata
  is incomplete, including enumerated settings such as `path.hash.mode`.
- Decide whether future command-registry metadata should remain Python-defined or move to a
  versioned data file while preserving fallback handling for unknown firmware commands.
- Decide where configurable stale/lost thresholds belong (`status` versus `scheduler`).

### CLI And Reporting

- Decide whether live sync progress should include timestamps now that it is always displayed for
  foreground node sync.
- Explain partial status more directly when enforced reads timed out after other reads succeeded;
  `TRIES` counts writes, so a failed read correctly does not increment it.
- Add an operator-controlled cleanup for stale cached facts that are no longer configured, such
  as a misspelled `facts:` key (`powersavings`) that remains visible in node status after it is
  removed from configuration. Preserve observation history by default; make deletion explicit.
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
- Confirm version-specific `region allowf` behavior before adding any region automation.

## Won't Do / Not Yet

- Do not auto-edit `config.yaml` or `secrets.yaml` from `mchd`; they remain operator-owned source
  files until a reviewed, audited configuration-write workflow exists.
- Do not store passwords or node private keys in the observational SQLite database without an
  explicit secret-storage and recovery design.
- Do not add a first-class reset command until reset becomes a normal operator workflow.
- Do not place dangerous identity/reset operations in ordinary desired-state enforcement.
- Do not turn mesh DM control into a general messaging client or enable writes by default.
- Do not add vanity key generation until ordinary commissioning and replacement are stable.

## Done Recently

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
