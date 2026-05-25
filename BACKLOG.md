# Mesh Herder Backlog

This is the canonical working backlog for Mesh Herder. Use it for todo items, wishlist items,
deferred ideas, and explicit non-goals. Keep `JOURNAL.md` for dated decisions and lessons learned;
keep `DESIGN.md` for stable architecture notes.

## How to Use This File

- Prefer simple, boring solutions over clever or expansive ones.
- Treat "we could do this" as insufficient. New work should have a clear operator benefit,
  reasonable implementation cost, and low ongoing maintenance burden.
- Avoid fragile high-maintenance code. If a change depends on non-obvious ordering, firmware
  behavior, or safety constraints, leave a concise "why" comment or journal note so the reasoning is
  not rediscovered later.
- Keep speculative ideas in **Wishlist** or **Not Yet** until they solve a real problem.
- Apply the same filter to code review follow-ups: fix real bugs and maintainability risks first,
  and avoid turning nice-to-have ideas into required work.
- Add new work to **Inbox** first if priority or scope is unclear.
- Move committed near-term work to **Next**.
- Move broader improvements to **Later** or **Wishlist**.
- Move rejected or intentionally deferred ideas to **Won't Do / Not Yet** with the reason.
- When work lands, move it to **Done Recently** with the date.

## Inbox

### Operator Bugs

- Decide whether replacement commissioning should run an immediate sync after reboot rather
  than requiring a follow-up `mch sync <prefix>`.
- Persist "config node not visible to gateway" startup warnings in `mch logs`, not only systemd
  output.
- Separate the radio used to reach a node from a changed desired radio, so enforcing a new radio
  does not lose the route needed for the next sync. Consider known historical radio values as a
  recovery aid.

### Reporting And Commands

- Add `mch ping [NODE]` to confirm whether a node is alive on selected radio settings.
- Add `mch discover` to find nearby nodes not yet present in gateway contacts.
- Add an overall `mch health` view showing node health and required operator actions.
- Make `mch suggest <node>` show differences from current configuration by default.
- Consider latitude/longitude in suggestions so nearby nodes do not receive identical txdelay
  recommendations.

### Configuration And Commissioning Ideas

- Add operator-facing descriptions or generated YAML comments for enumerated setting values.
  For `path.hash.mode`, document modes `0` through `2`, mark `3` reserved, and warn that
  multi-byte advert hashes need firmware support across the forwarding network.
- Clarify that the public key shown in new-node commissioning is generated output, not an
  editable label or requested identity.
- Explore optional vanity generated pubkey prefixes, with an explicit cost/time warning.
- Decide whether `mch commission new` and `mch commission replace` should become explicit
  subcommands, with bare `mch commission` kept as an interactive wizard.
- Decide where field metadata should live for validating MeshCore `set` values: local command
  metadata, firmware docs-derived data, or a small hand-maintained schema.
- Decide whether a future config section should own status thresholds, for example
  `status.lost_after_days`, or whether this belongs under `scheduler`.

## Next

- Extend the normalized fleet API model for dashboard requirements as they become concrete.
  - `schema_version: 1` reporting and a loopback-only read-only HTTP transport landed
    2026-05-25; `mch status` and `mch plan` now source their display data from that fleet model.
  - Next goal: migrate mesh DM/channel summaries to the model and refine dashboard/map needs
    without independently deriving health or actions.
  - Keep command verbs short and stable:
    - `mch status [NODE]` = current state and evidence.
    - `mch plan [NODE]` = what mchd would do next.
    - `mch logs [NODE]` = history.
    - `mch suggest [NODE]` = recommendations.
    - `mch sync [NODE]` = do work now.
  - The normalized model should include identity, `last_seen`, `last_connected`, `next_sync_at`,
    facts, settings, issues, actions, and recent activity.
  - Setting classification should distinguish: `ok`, `nominal`, `drift`, `unknown`, `ignored`,
    and `blocked`. `nominal` means the value differs textually but is equivalent within expected
    firmware/computer representation tolerance.
  - Action classification should distinguish: `none`, `read`, `write`, `report`, `ignore`,
    and `blocked`.
  - `mch plan` should render only actionable/non-OK rows, grouped as planned writes,
    audit-only differences, needs observation, and blocked work.
  - Keep YAML as the source of truth for now, but make this model JSON-ready so a future web UI,
    OpenAPI schema, and mesh summaries do not scrape CLI text.

- Add a curated MeshCore command registry for commands mch actually uses.
  - Goal: one small hand-maintained table defining command shape and metadata for known fields,
    replacing scattered conditionals such as `_BARE_COMMAND_KEYS`, fact bare-command tuples,
    raw exec safety lists, comparison rules, CLI radio validation, and commissioning timeouts.
  - Future direction: move the command data out of `src/mch/command_registry.py` into a versioned
    `command_registry.json`; keep Python as the loader, validator, and fallback behavior layer.
    That lets new firmware commands be added or corrected by editing data instead of changing
    application code.
  - Do not model the entire MeshCore CLI. Unknown settings must keep working with the default
    `get <key>` / `set <key> <value>` behavior, exact comparison, and no validation.
  - Suggested `CommandSpec` fields: `key`, `kind`, `read`, `write`, `writable`, `value_type`,
    `validator`, `comparator`, `write_timeout_seconds`, `reboot_required`, `min_firmware`,
    `serial_only`, `dangerous`, `confirm`, `fire_and_forget`, and `notes`.
  - Use `write_timeout_seconds` rather than a vague `slow_write` flag. Current known extended
    timeout examples: `set prv.key` and `set radio` need 180s in commissioning.
  - Initial known settings/facts should cover current examples and profile output: `name`,
    `owner.info`, `radio`, `powersaving`, `lat`, `lon`, `txdelay`, `direct.txdelay`,
    `flood.advert.interval`, `advert.interval`, `path.hash.mode`, `loop.detect`, `multi.acks`,
    `agc.reset.interval`, `ver`, `board`, `role`, `clock`, `neighbors`, and `public.key`.
  - Initial validators should cover `on_off`, integer/float, radio tuple, latitude/longitude,
    SF 7-12, CR 5-8, and later string byte limits like node-name length.
  - Radio tuple validation should require `freq,bw,sf,cr`; SF and CR are bounded integer choices,
    bandwidth starts with known common values but remains easy to expand, and frequency keeps the
    current broad firmware-supported MHz range until regional profile limits exist.
  - Known invalid desired values should appear as blocked in `mch plan` and fail `mch validate`
    with clear messages. Unknown keys should be logged as metadata candidates when firmware
    returns obvious command errors, without spamming logs.
  - Hardware discovery tests to add near the start of registry work:
    - Mac v1.15.0 repeater finding: `tempradio <freq,bw,sf,cr>,<minutes>` replies
      `OK - temp params for N mins`; `get radio` still reports the persistent value; bare `radio`
      returns `Unknown command`; `set radio <same freq,bw,sf,cr>` replies `OK - reboot to apply`.
      Still test on Luna-managed firmware before removing `needs_hardware_test`.
      Not yet confirmed: whether `tempradio <target>,<minutes>` followed by
      `set radio <same target>` avoids an immediate reboot because runtime radio is already on the
      target tuple. That needs an over-mesh test or another way to observe the active radio after
      the temporary interval expires; serial-only `get radio` only showed the persistent value.
      Heltec V3 v1.15.0 same-value test: current persistent radio was
      `915.7999877,250,10,5`; `tempradio 915.800,250,10,5,5` replied
      `OK - temp params for 5 mins`; `set radio 915.800,250,10,5` still replied
      `OK - reboot to apply`; `get radio` still returned `915.7999877,250,10,5`.
      This confirms command replies and doc consistency, but not whether an over-mesh target radio
      change could avoid downtime when temp radio already matches the desired tuple.
      Community/Discord reports conflict with the docs/reply: operators report that
      `tempradio <target>,<minutes>` followed by `set radio <same target>` persists the radio and
      does not require reboot in practice. Treat this as unresolved until an over-mesh active-radio
      test confirms or disproves it.
    - Test `reboot` after `set radio` versus `tempradio`/`radio` so the registry can distinguish
      `reboot_required` from `apply_command`.
      Mac v1.15.0 finding: `reboot` caused the serial CLI to fail with `Device not configured`;
      the device reappeared and answered `ver` after roughly 5 seconds. It did not correct a wrong
      2024 clock. After `clock sync`, plain `reboot` returned the clock to the wrong 2024 baseline.
      Heltec V3 v1.15.0 finding: `clock sync` followed by plain `reboot` preserved the synced
      2026 clock across reboot.
    - Test `clkreboot` to document whether it replies before rebooting, should be treated as
      fire-and-forget, and how long mch should wait before reconnect/rediscovery.
      Mac v1.15.0 finding: `clkreboot` caused the serial CLI to fail with `Device not configured`;
      the device reappeared and answered `ver` after roughly 5 seconds. On the raw serial path it
      did not correct a wrong 2024 clock; `clock sync` did correct it and replied `OK - clock set`;
      `clock sync` followed by `clkreboot` still returned the clock to the wrong 2024 baseline.
      Still test whether mesh-side `clkreboot` behaves differently before preferring it over
      `clock sync` plus `reboot`, but do not assume it is better on raw serial repeaters.
      Community reports suggest behavior may be hardware-specific: nRF nodes often reset clock on
      reboot, ESP32 nodes often do not, and `clkreboot` on V4/ESP32 may be a recovery step that
      lets `clock sync` work afterwards rather than setting/preserving time itself. Test a V3/V4
      separately before making `clkreboot` preferred behavior.
      Heltec V3 v1.15.0 finding: `clock sync` followed by `clkreboot` reset the clock back to the
      wrong 2024 baseline, while `clock sync` plus plain `reboot` preserved it. Do not prefer
      `clkreboot` for V3 based on current evidence.
      Luna mesh-side note: `mch exec 81d6bf1950a0 clock` timed out with no response in this
      session, so mesh-side `clkreboot` was not tested.
    - Test `region allowf` on old/new firmware if available; older firmware reportedly needed it
      because added regions started disabled, while newer firmware adds regions enabled.
      Mac v1.15.0 finding: `region` returned `*^ F`; `region list` and `region help` returned
      `Err - ??`.
    - Luna note: after stopping `mchd`, `/usr/local/bin/meshcli -s /dev/ttyACM0 -r help` returned
      normal help text, but simple reads such as `get radio` produced garbled/empty output in this
      session. Do not run write tests there until the serial reply path is reliable.

- Add explicit profile update command.
  - One-day scope: add `mch profiles update` (or `mch update-profiles`) to fetch
    `profiles_url` or the default `https://meshherder.com/profiles.yaml`, validate
    `version: 1`, and write the cache used by recommendations/commission radio presets.
  - Keep commissioning offline-friendly: do not prompt to fetch profiles during
    `mch commission`.
  - Output should summarize what changed, especially profile count and radio preset count.

- Auto-preempt a scheduled sync when an operator requests `commission`/`exec`.
  - Today an interactive `mch commission` against a node whose scheduled sync just kicked
    off fails with `busy: scheduled sync <prefix>`, and the operator has to mash Enter to
    retry until the sync finishes. This is confusing and feels broken even though it's the
    safety lock working as designed.
  - Operator commands should preempt at the next safe boundary (sync ops are between-command
    interruptible). Implementation idea: a "preempt requested" flag the sync loop checks
    between commands; on set, the loop yields after the current command completes and
    releases the operation lock so commission can take over. Never interrupt mid-command —
    leaving a node mid-write is the failure mode we're avoiding.

- Consider mapping-style YAML for `nodes:` in `config.yaml`.
  - Today nodes are a YAML list of mappings; secrets is already a mapping keyed by prefix.
    Operator suggested matching secrets' shape:
    ```yaml
    nodes:
      81d6bf19:
        label: 81d6bf1950a0
        group: default
    ```
  - Pros: shorter, consistent with secrets, YAML key uniqueness enforces no duplicate
    `pubkey_prefix`.
  - Cons: needs a migration for every existing operator config; loses any meaning of
    `nodes:` order (though most parsers preserve insertion order in practice); the loader,
    validation messages, and examples all need updating.
  - Decision deferred — capture as a possible future config shape, not committed.

- Add latitude/longitude to the `commission new` wizard.
  - Prompt for `lat` and `lon` during the wizard with default `0,0` (treated as blank /
    unset). Persist into the generated `config.yaml` snippet only if the operator entered
    non-zero values, or emit `lat: 0` / `lon: 0` as explicit placeholders — choice TBD.
  - When implemented, also write the values to the node via `set lat` / `set lon` during
    commissioning (these don't require reboot per the MeshCore CLI reference).
  - Add optional home-location proximity warnings, using an operator-friendly centre plus radius
    rather than a manually calculated bounding box, for example:
    ```yaml
    location:
      center: -33.73,150.90
      warn_outside_km: 20
    ```
    Warn when entered or observed coordinates lie outside that radius. Keep ordinary geographic
    bounds (`lat` -90..90, `lon` -180..180) as hard validation; do not reject intentional
    out-of-area deployments. A raw bounding-box form can be considered later for unusual
    non-circular deployment areas.
  - **Name length interacts with location**: per the MeshCore CLI reference, a node's
    advertised name is capped at **32 bytes when no location is set, dropping to 24 bytes
    once `lat`/`lon` are non-zero** (the 8 bytes go to the lat+lon floats in the advert).
    The wizard currently caps at 31 bytes regardless. After adding lat/lon prompts, the
    name truncation logic in `cli.py:_commission_new_wizard` must account for whether
    location is being set: cap at 24 if yes, 32 if no. Also surface this in the prompt
    text so operators understand why the name they typed got rejected.

- `mch adopt <node-id>`: bring an existing in-the-wild node under management.
  - Use case: a repeater is already deployed and operational on the mesh, possibly
    commissioned by someone else or by a previous tool. Operator wants to start managing
    it with mch without re-flashing, re-commissioning, or hand-transcribing settings.
  - Flow (sketch):
    1. Operator runs `mch adopt <prefix>` or picks from a list of currently-discovered
       repeaters that are NOT in `config.yaml`.
    2. mchd queries the node via the existing CLI: `ver`, `board`, `get name`,
       `get public.key`, `get radio`, `get lat`, `get lon`, `get role`, `get owner.info`,
       any other observable fields.
    3. Operator is prompted for the admin password (the only thing mchd can't read).
    4. mchd verifies it can authenticate, then prints paste-ready `config.yaml` and
       `secrets.yaml` snippets matching the *observed* state, with the gateway's home
       radio used for `via` if the node is on the same freq, or the observed radio
       otherwise.
    5. Notably, `prv_key` cannot be obtained over the mesh (`get prv.key` is serial-only),
       so the secrets entry will have `password` only, no `prv_key`. That node will not
       be eligible for `commission replace` until the operator either gets serial access
       to read the key, or accepts a future re-key (which loses the current identity).
  - Distinct from `commission new` (factory hardware → managed) and `commission replace`
    (factory hardware → restore known identity). Adopt is "already-managed-by-someone-else
    hardware → under-mch-control without touching anything destructive".

- Investigate post-replace "Heltec Repeater" name regression.
  - Observed 2026-05-23: after a successful `commission replace` of `81d6bf19`, the node
    came up named `Heltec Repeater` instead of the label-derived name. Replace mode does
    not send `set name` (only `set guest.password`, `password`, `set prv.key`, `set radio`,
    `reboot`) — so this is the firmware picking a default name when identity changes.
  - Two paths to consider: (a) add `set name <label>` to the replace command sequence as
    a normal idempotent push; (b) leave the wizard alone and rely on declared
    `settings.name.desired` to converge on the next sync. Path (a) gives an immediate
    consistent name without requiring a settings declaration; path (b) keeps replace mode
    strictly about identity. Pick after deciding the policy.

- Configurable default label/name format in the commission wizard.
  - Today the wizard defaults label and name to the firmware's 12-hex-char convention
    (first 12 hex of the derived pubkey), e.g. `81d6bf1950a0`. Operators have asked for a
    project-identifying default such as `mch_81d6bf19`.
  - Add a `commission.default_label_format` (and maybe `default_name_format`) string in
    `config.yaml` with placeholders like `{prefix8}` / `{prefix12}`.
  - Keep the firmware-style default as a fallback if not configured.

- Allow resurrecting an archived node from secrets-only.
  - Use case: operator pruned a node months ago (removed from `config.yaml`, `mch prune`d
    the DB), but kept the `secrets.yaml` entry as an archive. Currently `commission replace`
    only lists prefixes that are in BOTH `config.nodes` and `config.secrets`.
  - Extend `list_replace` to also surface secrets-only entries with an `(archived)` tag,
    so the operator can pick one and the wizard prints a fresh `config.yaml` snippet
    alongside the usual replacement flow.
  - Behaviour after restore should be the same as today's replace: the operator pastes
    the snippet and `mch sync <prefix>` converges normal settings.

- Group-aware defaults for `commission new`.
  - Today the wizard pulls radio defaults from the gateway node's home radio settings. It
    should also honour `config.yaml` group settings: when the operator picks group
    `default` (or any named group), pre-fill radio/region/other applicable fields from that
    group. If no group match, fall back to a `default` group if present, then to the
    gateway's radio.
  - Same pattern for `secrets.yaml`: if a `default:` entry is present, use it as the base
    for the new node before applying form values.
  - Decide where guest password lives. It is not really a secret (any user holds it), so
    it could live alongside other group settings in `config.yaml` rather than in
    `secrets.yaml`. The admin password and `prv_key` are the only true secrets.
  - Keep the form override behaviour: the operator's explicit input always wins.

- Persist daemon-generated `prv_key` earlier in `commission new`.
  - Today the CLI wizard generates `prv_key` once before the retry loop (`cli.py:1129`)
    and passes the same value on every RPC, so in-session retries keep the same identity.
  - If the wizard itself is killed (Ctrl-C, terminal closed, ssh dropout) mid-run, the
    `prv_key` is lost while the node may still be holding it. The node is then bricked
    until manual factory reset.
  - Options to consider: write the generated key to a short-lived state file before the
    first RPC, or have the daemon record commissioning attempts in the DB. Either path
    must redact key material in logs and clean up the record on successful commission.

- Finish retired/stale node lifecycle handling.
  - `mch prune` already lists and can delete DB ghosts.
  - Add stale/lost status for nodes not seen after a configurable threshold, with compact display
    such as `>99d ago`.

- Improve commissioning prompt semantics.
  - Keep Enter for accepting defaults.
  - Replace or clarify `-` sentinel values for "none" or "clear".
  - Ensure generated snippets never persist placeholder sentinel values.
  - Update success text to mention both inspection and action:
    `mch status <prefix>` and `mch sync <prefix>`.

- Add field-aware validation before sending values to nodes.
  - Reject non-numeric values for numeric fields.
  - Reject out-of-range numbers.
  - Reject malformed `set` payloads before they reach the mesh.
  - Validate radio tuples specifically: `freq,bw,sf,cr`; `bw` should be one of the firmware's
    accepted LoRa bandwidths (commonly 62.5, 125, 250, maybe board/firmware-dependent extras),
    `sf` should be 7-12, and `cr` should be 5-8.
  - Validate `lat` and `lon` ranges.
  - Apply the same rules to config/secrets validation where relevant.

- Add concurrency tests around scheduler/manual sync/exec/commission ownership.
  - Assert only one high-level gateway operation can own the radio/session at a time.
  - Cover scheduled sync, manual full sync, forced node sync, raw exec, and commissioning.

- Add lightweight per-sync outcome counters.
  - Track how many nodes were contacted in a cycle.
  - Track how many update commands were sent.
  - Track how many values were actually changed.
  - Keep this simple: useful status/log output and DB history, not a full metrics subsystem.

## Later

- Split reachability radio from desired node radio.
  - `NodeConfig.via` currently describes both how the gateway reaches a node and what radio config
    replacement commissioning should push.
  - Keep `via` for gateway/routing reachability.
  - Add a distinct desired node radio model before bridge-mediated management gets more complex.

- Add named database migrations.
  - Replace one-off `ALTER TABLE` behavior with ordered migrations and a schema version table.
  - Make upgrades deterministic and easier to reason about.

- Improve URL profile cache behavior.
  - Avoid writing to `/var/lib/meshherder/profiles.yaml` unconditionally in development/tests.
  - Derive the cache path from config or DB directory.
  - Validate downloaded YAML before replacing a known-good cache.

- Break up large modules.
  - `cli.py`: split rendering and commissioning wizard concerns.
  - `queue.py`: split scheduling, node processing, facts/settings, auth, and operations.

- Reduce duplicated issue/status assembly.
  - Extract shared issue-report construction for drift, bad-state, ghost, lost, and open-node
    reporting.
  - Test it directly.

- Add typed MeshCore contact dictionaries.
  - Introduce a `TypedDict` for the contact fields this project uses, including `public_key`,
    `adv_name`, `type`, `flags`, and radio path fields.

- Support derived desired values from observed state.
  - Concrete example: if uptime is less than 48 hours, set zero-hop advert interval to 3 hours;
    otherwise turn it off.
  - More generally, allow settings to depend on facts such as uptime, role, or other observed data.
  - Keep this separate from ordinary static desired-state enforcement.

- Continue `mch status <node>` convergence.
  - Treat `mch node <node>` wording as legacy.
  - Remove or redirect older docs/code references over time.

- Polish `mch validate` output ordering.
  - The current behavior is useful, but the display is a little awkward: the node/group count sits
    between config and secrets status, and it mentions secrets before confirming the secrets file.
  - Revisit after core validation behavior settles.

- Revisit retired-node command naming.
  - `mch remove <node>` works, but `mch prune <node>` might be a better fit as the single-node form
    of `mch prune`.
  - If changed, keep `mch remove` as an alias or compatibility path until the CLI settles.

## Wishlist

- **`_ago` leading-space cosmetic**: `_ago` right-justifies to 3 chars (`{n:>3}`), so
  single-digit values get 2 leading spaces and 2-digit get 1 — visible in `_ago_with_ts`
  as `(  5m ago)` vs `( 44m ago)`. Options: drop padding or use fixed 2-digit format.

- **`auto` computed values for txdelay/direct.txdelay**: allow `txdelay: "auto"` and
  `direct.txdelay: "auto"` in config. mchd resolves `"auto"` at sync time using the same
  formula already in `recommend.py`, keyed off the node's observed `neighbors` fact —
  more neighbors → lower txdelay → node gets right-of-way. Most settings from `mch suggest`
  are static and can just be pasted into config directly; `txdelay`/`direct.txdelay` are the
  only ones that are genuinely dynamic (they change as the mesh grows). The formula lives in
  `recommend.py` so it stays testable and consistent between `mch suggest` output and actual
  enforcement. Needs design: what desired value is displayed in `mch plan` and `mch status`
  when the value is `auto` (show the resolved value? show `auto (1.0)`?); what happens when
  `neighbors` fact is not yet known (fall back to the unknown-neighbors default of 2.0?).
  Note on direction: more neighbors → lower txdelay (right-of-way for busier nodes). Some
  operators argue the opposite — small nodes go first so leaf nodes aren't starved — but the
  chosen logic prioritises throughput: a high-neighbour hub carries more mesh traffic, so
  stalling it is more costly than stalling a leaf.

- **Name / owner.info group-level prefix and suffix**: add `name_prefix`, `name_suffix`
  (and matching `owner.info_prefix` / `owner.info_suffix`) to group config. The daemon
  assembles the full desired value at sync time, so per-node config stays clean — e.g.
  `name_prefix: "🦷 "` at group level, `name: "Quakers Hill Corb"` per node. Drift
  detection compares the assembled string against the observed value.

- Support alternate config/secrets locations end-to-end.
  - Let `mch validate` check a working copy in a different location, such as a home directory,
    before copying it into production.
  - Let `mchd` read config and secrets from operator-chosen paths instead of assuming `/etc/meshherder`.
  - Keep the current production layout as the default, but stop hardcoding it where flexibility
    helps safer edits.

- DM-triggered sync for already-managed nodes, using the same authorization and operation-lock
  model as existing CLI-triggered sync.
- Channel or room-server reporting for fleet status.
- Web UI with node map, using stored lat/lon facts.
- Firmware age reporting from collected `ver` facts.
- Docker image once packaging and runtime paths settle.
- Bluetooth connection support through `meshcore_py.create_ble`.
- Optional vanity public-key generation after normal commissioning and replacement are reliable.

## Research / Live-Test

- Keep `reboot` as the commissioning final step unless future firmware evidence says otherwise.
  - Official MeshCore CLI docs describe `clkreboot` as "reset the clock and reboot", not as
    "sync clock and reboot".
  - Bench Seeed/nRF-style repeater: `clock sync` corrected time, but both `reboot` and `clkreboot`
    returned the clock to the wrong 2024 baseline.
  - Heltec V3 v1.15.0: `clock sync` plus plain `reboot` preserved the synced clock, while
    `clock sync` plus `clkreboot` reset the clock to the wrong 2024 baseline.
  - Current conclusion: do not replace commissioning `reboot` with `clkreboot`. If a future
    ESP32/V4 recovery case needs `clkreboot`, model that as hardware-specific remediation before a
    later `clock sync`, not as the normal reboot path.

- Investigate what boolean vocabulary MeshCore firmware actually accepts for boolean-style settings.
  - Currently mchd coerces YAML booleans (`on`/`off`/`yes`/`no`/`true`/`false` and case variants)
    to `"on"`/`"off"` at config load time, because PyYAML parses all of them as Python `True`/`False`
    and we've lost the original spelling.
  - Unknown: does `set powersaving true` work the same as `set powersaving on`? Does `1`/`0`?
    If the firmware is flexible, `"on"`/`"off"` is still the safest canonical form, but knowing
    what it accepts would confirm the coercion is correct and whether `true`/`false` or `1`/`0`
    are silently equivalent.
  - Test with bench node: send `set powersaving true`, `set powersaving 1`, `set powersaving yes`
    and check `get powersaving` response to see what the firmware stores and echoes back.
  - Also worth checking: are there any boolean-style settings beyond `powersaving` where `on`/`off`
    is wrong and the firmware expects a different vocabulary?
  - If firmware accepts multiple forms: no code change needed, current coercion stays correct.
    If firmware only accepts `on`/`off`: document this explicitly in CLAUDE.md.

- Test `tempradio` followed by `set radio` as a possible no-reboot commissioning radio switch.
  - Current confirmed behavior on Heltec V3 / MeshCore v1.15-era firmware: `set radio` returns
    `OK - reboot to apply`; commissioning should wait for that response and then send one reboot.
  - Do not change commissioning behavior until current firmware has been live-tested.

## Won't Do / Not Yet

- Do not auto-edit `config.yaml` or `secrets.yaml` from `mchd`.
  - Config and secrets are operator-managed source-of-truth files.
  - Commissioning should print paste-ready snippets only.

- Do not add a first-class `mch reset` command yet.
  - Bench reset/reflash can stay as documentation with explicit `esptool` commands.
  - Reset automation can be reconsidered only if it becomes a normal operator workflow.

- Do not broaden DM control into writes by default.
  - Current DM commands should remain read-only/status-oriented unless a clear operator need
    justifies write actions.

- Do not put dangerous identity/radio/reset commands into ordinary desired-state enforcement.
  - Keep those in deliberate commissioning or replacement flows.

- Do not implement vanity public-key generation until generated-key commissioning and replacement
  are both stable.

## Done Recently

- 2026-05-25: Added the normalized read-only fleet report contract (`schema_version: 1`), routed
  status/plan/issue interpretation through it, switched terminal status/plan views to consume the
  fleet API model, and added an optional loopback-only HTTP JSON API for a future dashboard/map.
- 2026-05-25: `mch logs` identifies node entries by configured label and pubkey prefix, and
  log timestamps now include relative ages in fleet and node-detail activity output.
- 2026-05-25: YAML tab-indentation parse failures in config and secrets now report the
  actionable fix: replace indentation tabs with spaces.
- 2026-05-25: Config/secrets hot reload now detects timestamp-preserving replacements and
  atomic file swaps, rather than relying only on modification time and file size.
- 2026-05-25: `mch commission new` now sanitizes local labels while retaining human-friendly
  mesh names, only emits configured groups, and includes the chosen mesh name as an enforced
  setting in generated config snippets. `mch plan` also shortens unobserved-value reasons to
  `not observed yet`.
- 2026-05-25: Setting enforcement now persists an update-attempt count for the current desired
  value and displays it as `TRIES` in node detail and drift summaries, exposing values repeatedly
  failing to converge.
- 2026-05-24: Duplicate node labels in `config.yaml` are rejected during config load, so
  `mch validate` fails before ambiguous local labels can reach status/commissioning flows.
  Covered by `test_duplicate_labels_raise`.
- 2026-05-24: Added field-aware close-enough comparison for numeric settings. `radio` now parses
  `freq,bw,sf,cr` and tolerates firmware float roundoff on frequency/bandwidth while keeping SF/CR
  exact; `lat`/`lon` compare within roughly three decimal places. Status still shows the observed
  value and marks rows within expected tolerance with `*`, but audit/enforce no longer treats them
  as drift.
- 2026-05-24: Settings table reworked: POLICY first column, DRIFT last column showing
  `???` (not yet observed) / `~~~` (drifted) replacing the old left-side `~` indicator.
  Facts section shows `???` for any `config.facts` key not yet collected. Drifted settings
  table in `mch status` now uses content-based column widths (no more truncation on long
  values like `radio`).
- 2026-05-23: Fixed bare `mch validate` to check `/etc/meshherder/config.yaml`, matching the
  shipped daemon config path, instead of preferring a `config.yaml` in the current working
  directory. The command now prints the selected config path before validation; use
  `--config <path>` for working-copy validation.
- 2026-05-23: `mch validate` warns when a `secrets.yaml` `prv_key` derives a public key
  that doesn't match the `pubkey_prefix` it is filed under — catches typos and stale
  secrets entries early.
- 2026-05-23: Live-tested `commission replace` end-to-end against the bench node.
  Erased via USB → discovered as factory → `_login_for_replace` path → restored the
  `81d6bf19` identity → reboot → live pubkey/radio matched the original on USB check.
- 2026-05-23: Raised commission timeout to 180s for `set prv.key` and `set radio`. These
  derive a public key and write to flash before replying on v1.15-era firmware, so the 30s
  default was too short. Bench-measured and live-confirmed end-to-end on Heltec V3.
- 2026-05-23: `commission new` falls back to the user-supplied admin password when factory
  login fails. Recovers from a previous in-session attempt that already changed the admin
  password before a later command (e.g. `set prv.key`) timed out.
- 2026-05-23: Replacement commissioning now falls back to the managed password from
  `secrets.yaml` when factory login fails. Recovers from a previous partial commission
  that changed the admin password before timing out on a later command.
- 2026-05-23: Added a static `mchd(8)` man page for daemon operation and troubleshooting.
- 2026-05-23: Added a static `mch(1)` man page and README install instructions.
- 2026-05-23: Added `mch remove <node>` for deleting one node's local DB state, with `--force` for
  nodes still present in config.
- 2026-05-23: Added `mch validate` to check config/secrets locally without contacting `mchd`.
- 2026-05-22: Added `mch plan` as a read-only desired-vs-known view.
- 2026-05-22: Removed config-file write access from the daemon/systemd model.
- 2026-05-22: Implemented config/secrets hot reload with last-known-good behavior.
- 2026-05-22: Live-tested fresh-flash commissioning with generated private key and identity
  verification.
- 2026-05-22: Fixed commissioning command ordering for `set radio` plus one fire-and-forget
  reboot.
- 2026-05-22: Added `mch prune` for DB nodes no longer present in `config.yaml`.
- 2026-05-22: Normalized config, secrets, and messenger pubkey prefixes to lowercase.
- 2026-05-22: Validated `secrets.yaml` prefixes like config node prefixes.
- 2026-05-22: Made background manual sync failures observable.
- 2026-05-22: Made `mch suggest` the single suggested-settings command.
- 2026-05-22: Added CI for `pytest`, `ruff check .`, and `mypy src`.
- 2026-05-21: Fixed `MCHD_SOCKET` environment variable naming.
- 2026-05-21: Fixed secret lookup to match secrets prefixes against the full node pubkey.
- 2026-05-21: Tightened command safety matching for raw `mch exec`.
- 2026-05-21: Made the default setting policy `audit`.
