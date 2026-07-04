# Recommendations Part 2 — CLI UX and workflow

> ## Progress (updated 2026-07-04)
>
> All gates green throughout (`pytest` 970 passing, `ruff`, `mypy src`). Each slice on its own
> branch, merged `--no-ff` to `main`.
>
> - **Slice 1 (operator-first UX) — DONE.** Help-text rewrite with a banned-jargon guard test;
>   `mch help selectors`/`concepts`; top-level `mch status`/`mch sync`; exit codes documented +
>   tested; shell-completion docs; daemon-down hint fixed. **Deferred: S1.3** (drop the `fleet status`
>   single-match magic switch) — it requires a *released* version carrying the `mch node show` hint
>   first, and everything is still Unreleased. Cut the pending release (or get an explicit OK to break
>   early), then remove the switch.
> - **Slice 2 (grammar unification) — DONE.** `node prune [NODE]` absorbs `node remove` (now a hidden
>   deprecated alias, kept as the `--force` escape hatch); `node path --apply` previews by default;
>   scope-flag and mutation conventions codified in `mch help concepts` + DESIGN.md. (No release was
>   cut first, so `remove` was kept as an alias rather than deleted — revisit when a release ships.)
> - **Slice 3 (`--json` + firmware fleet views) — DONE.** `--json` on the read commands (report model
>   verbatim, API-parity pinned by a real-socket contract test); fleet-wide firmware currency (VER
>   `!` marker + advisory footer, never a fault); dead `ghosts`/`unseen_nodes` categories removed.
> - **Slice 4 (`mch node add` pipeline) — DESIGN DRAFTED, awaiting operator review.**
>   `docs/onboarding-pipeline.md` is Deliverable 1; no code until it is approved. It surfaces one
>   decision for the operator: whether the interactive wizard may write `secrets.yaml` itself.

A follow-up review of Mesh Herder at **v0.20.1 + unreleased sweep work** (commit `d22ffd3`,
2026-07-04), written — like part 1 (`RECOMMENDATIONS.md`) — as a work plan another AI model or
human can execute item by item.

**Context from the operator (rdm, 2026-07-04):** the focus is a solid CLI tool for managing
MeshCore repeater nodes — think meshmonitor.org but CLI-only. No web UI until the CLI and API are
down pat. Current complaint: *"The UI is very confusing and the workflow feels clunky."*

**Diagnosis.** The part-1 structural redesign worked: the five nouns
(`daemon`/`fleet`/`node`/`config`/`gateway`) are the right shape and commands now live under
sensible groups. The remaining confusion is not command placement; it is three specific things:

1. **Help text speaks implementer, not operator** — "Record a desired setting in the DB (the
   source of truth)", "policy-only override with --policy", "Explain one KEY for one NODE" — and
   the core vocabulary (desired vs observed, policy, scope, the NODE selector) is taught nowhere
   in the CLI. Several docstrings also render with mangled wrapping (click re-wraps prose).
2. **The daily loop has friction** — the highest-frequency command is three words
   (`mch fleet status`), it still carries the magic single-match detail switch, and there is no
   `--json`, so nothing is scriptable.
3. **Onboarding is fragmented** — `node new` / `node adopt` / `node discover` / `node probe` /
   `gateway contacts` plus hand-editing `secrets.yaml` and raw `--radio FREQ,BW,SF,CR` tuples.
   ROADMAP Direction 1 diagnosed this in June; it is still open.

This document sequences the fix as four slices. Slices 1–3 are mostly items already deferred to
`BACKLOG.md` ("From the 2026-07-04 RECOMMENDATIONS sweep") — the value added here is ordering,
pre-made decisions, and file anchors. Slice 4 is the big feature.

## How to use this document (instructions for the executing agent)

- **Read `CLAUDE.md` first** and follow it: TDD (failing test first, confirm it fails for the
  expected reason), small verified slices, preserve debug logging and comments, record rationale
  in `JOURNAL.md`/`DESIGN.md`.
- **Branch policy** (operator preference): every slice below is a branch + PR. Within a slice,
  keep commits small and gates green.
- **Gates** for every PR: `pytest` (916 passing at baseline), `ruff check .`, `mypy src` — all
  clean.
- **Check `BACKLOG.md` before starting an item.** Most slice-1/2/3 items exist there in shorter
  form. When you pick one up, move/merge it here-to-BACKLOG-to-done in one place; do not track
  progress in two files, and do not silently drop items from either.
- **Do not send mesh traffic** for slices 1–3; everything is verifiable with unit tests and
  `--help` output. Slice 4 needs live verification on the luna bench node only, per CLAUDE.md
  (test frequency 915.800, never the field frequency 915.075).
- **Decisions below are made, not open.** Where part 1 said "pick one", this document picks.
  If a decision proves wrong during implementation, stop and ask the operator; do not silently
  choose differently.
- **Do each slice in order.** Slice 2 gates new command surface (every command added before it
  copies today's inconsistencies); slice 3's JSON shape must be frozen after the dead-category
  removal (S3.1), not before.

---

## Slice 1 — Operator-first UX pass (P1, smallest, do first)

Directly attacks "the UI is very confusing". One PR. No behaviour changes except S1.3 and the
two tiny additions (S1.4, S1.7).

### S1.1 Help-text rewrite, every command

Rules for the pass:

- First line = what the operator gets, action-first, no internals. Banned words in help output:
  "DB", "source of truth", "sunset", "eviction", "policy-only override", "projection".
  (They remain fine in DESIGN.md and code comments.)
- Worked rewrites (do all commands, but these are the acceptance examples):
  - `config set` (`src/mch/cli.py:2113`): "Record a desired setting in the DB (the source of
    truth)" → "Set a desired value for a node, a group, or the whole fleet." Explain `--policy`
    values in operator terms (enforce = write it, audit = report drift only, fact = just collect
    the current value, skip = ignore this key).
  - `config show` (`cli.py:2239`): "Explain one KEY for one NODE" → "Show why a setting has the
    value it does: each scope's entry, which one wins, and what the node last reported."
  - `node prune` (`cli.py:1549`) / `node remove` (`cli.py:1759`): reword now, converge in S2.3.
- **Fix docstring rendering.** Click re-wraps paragraphs and currently mangles the aligned
  key/value explanations (see `mch config set --help`: "KEY:  a writable setting … key       
  (e.g. ver"). Use click's `\b` escape for blocks that must not be re-wrapped, or restructure as
  short paragraphs. Verify every command by eyeballing `--help` output, not just reading source.
- **Test:** extend the docs-drift guard (part-1 M5, `tests/`) with a test asserting no banned
  jargon words appear in any command's `--help` output (invoke via `CliRunner`). This makes the
  rewrite regression-proof.

### S1.2 `mch help concepts` — teach the vocabulary once

Add a `mch help [TOPIC]` group (or a single `mch concepts` command if click makes topic routing
awkward — either is fine, document the choice) with two topics:

- `selectors`: the canonical NODE grammar — label | unique pubkey prefix | mesh-name substring —
  written once. Every command's help then says "NODE: see `mch help selectors`" instead of
  restating a variant.
- `concepts`: desired vs observed values; policy (enforce/audit/fact/skip); scope and precedence
  (node > group > global); what a sync cycle does; where state lives (one paragraph, operator
  wording: "mch keeps its inventory and desired settings in a local database;
  `mch config export` is your backup").

Reference text sources: DESIGN.md already explains all of this for developers; compress, don't
invent.

### S1.3 Drop the fleet-status magic detail switch

`fleet status QUERY` with a single match silently switches to the full node-detail view
(`cli.py:609`, switch at `cli.py:625` — the code comment already says it is being retired).
`mch node show` and the hint shipped in the unreleased sweep.

- **Dependency:** part 1 required one *released* version carrying the hint before the switch is
  dropped. The post-sunset release has not been cut yet (top of BACKLOG "Sunset follow-ups").
  So: **cut that pending release first** (it is overdue anyway), then remove the switch in this
  PR targeting the next release. If the operator prefers not to wait, get an explicit OK to
  break early — alpha software, but ask.
- After removal: `fleet status QUERY` always renders the (filtered) table; single match appends
  the existing hint line pointing at `mch node show`.

### S1.4 Top-level `mch status` and `mch sync` shortcuts

Additive aliases for the two highest-frequency commands, forwarding to `fleet status` /
`fleet sync` with identical arguments. Visible in `mch --help` (listed after the groups),
one-line help: "Shortcut for `mch fleet status`." Nothing else gets a top-level alias — the noun
tree stays the model; these two exist because they are the daily loop.

### S1.5 Document exit codes

`mch fleet issues` **already** exits 2 when issues are present (`cli.py:719`) — this item is
documentation, not implementation. Write an "Exit codes" section (README + `mch help concepts`):
0 = ok, 1 = error, 2 = issues present (`fleet issues` only). Decision: `fleet status` keeps
exit 0 always — it is a viewing command; `fleet issues` is the cron/alerting surface. Add a test
pinning each documented code.

### S1.6 Document shell completion

Click ships completion free. Add an INSTALL.md section:
`eval "$(_MCH_COMPLETE=bash_source mch)"` (+ zsh/fish variants). Verify the env-var name against
the installed click major version before documenting.

### S1.7 Fix the daemon-down hint

`_check_default_config_when_daemon_unavailable` (`cli.py:247`) still prints
"is mchd running?  try: mchd --help". The useful hints now exist: suggest
`mch daemon status` and `mch daemon logs` instead. (Part-1 C5 leftover.)

---

## Slice 2 — Grammar unification (P1, gates all new command surface)

One PR. Breaking changes here are acceptable without aliases **only** because the release in
S1.3 has just been cut and CHANGELOG carries loud deprecation notes — otherwise alias per part-1
rules.

### S2.1 Scope grammar — decision made

Keep **both** flags, with a codified rule (this resolves part 1 principle 3's "pick one"):

- `--group NAME` / `--global` = **scope of a desired setting** (`config set`/`unset`). "Set this
  at global scope" reads correctly; "set --all" does not.
- `--all` = **apply an action fleet-wide** (`node clean facts|settings`, `cli.py:1683,1720`, and
  any future fleet-wide action).

Work: audit every command for conformance, state the rule in `mch help concepts` and DESIGN.md.
No renames expected; the deliverable is the written rule plus conformance.

### S2.2 One mutation convention

Target: **mutating commands preview by default and execute with `--yes`**; interactive prompts
only for wizards (`node new`) and typed-confirmation guards (`node exec` destructive commands —
keep as-is, it is a confirm not a preview).

Audit list: `node prune`, `node clean`, `config import` already conform. `node remove` prompts
interactively — resolved by S2.3. `node path --apply` prompts unless `--yes` — convert to
preview-by-default + `--yes`. Re-audit the whole tree after these; document the convention in
`mch help concepts`.

### S2.3 Converge `node remove` into `node prune`

`node prune [NODE]` becomes the single delete-local-state command: no argument = list/purge
retired nodes (current behaviour), `NODE` argument = prune that one retired node.
`node remove` is deleted; its help pointed users at `retire` anyway. Guard: pruning a
non-retired node is refused with "retire it first: mch node retire NODE". CHANGELOG deprecation
note. (BACKLOG already asks for this convergence.)

---

## Slice 3 — `--json` and the scripting surface (P1/P2)

This is the CLI's answer to meshmonitor: glanceability is the table, alerting is
`fleet issues` + exit 2 in cron, and integration is `--json`. It is also the "get the API down
pat" step — CLI JSON and HTTP API must be the same model.

### S3.1 Remove the dead `ghosts`/`unseen_nodes` issue categories — FIRST

Already specced in BACKLOG ("CLI And Reporting", last item): post-sunset `report.py` always sets
`configured=True` and `unseen=[]`, so these sections can never populate. Remove the categories
from `report.py`, the render paths (`_render_issue_sections`, `_filter_ghosts`,
`_filter_unseen_nodes` in `cli.py`), the summary fields, and their tests.
**Must land before S3.2** so the frozen JSON shape does not carry dead fields.

### S3.2 `--json` on read commands

Add `--json` to: `fleet status`, `fleet issues`, `fleet plan`, `fleet logs`, `node show`,
`config show`.

- **Shape = the report model.** The HTTP API (`src/mch/api.py:91`) already serves
  `build_fleet_report()` / `build_node_report()` (`src/mch/report.py`) directly. The CLI must
  emit the same shapes — ideally the daemon response verbatim after `_check`, bypassing the
  `_status_view`/`_node_detail_view` shims (`cli.py:538,561`). If the socket response and the
  API response differ for the same data, that difference is a bug to fix, not to paper over.
- `fleet issues --json` keeps exit code 2 (machine-readable output, machine-readable code).
- **Contract test** (part-1 M4 remainder): one round-trip test over a real Unix socket asserting
  `mch fleet status --json` output equals the API `/status` body for the same stub DB. This
  pins CLI/API parity permanently.
- Document `--json` in README with one worked `jq` example (e.g. nodes not seen in 24 h).

### S3.3 Fleet-wide firmware-currency surfaces

Already fully specced in BACKLOG ("Fleet-wide firmware-currency surfaces") — the VER-column
marker in `fleet status` and the advisory "outdated firmware" entry in `fleet issues`. Implement
per that spec (advisory, never a bad-state; reuse `command_catalog.firmware_currency()`). Do it
after S3.2 so the JSON shape includes it from the start of its life.

---

## Slice 4 — `mch node add`: one onboarding pipeline (P2, design first)

The "workflow feels clunky" fix, and the largest piece. ROADMAP Direction 1 and the BACKLOG
Phase 7 item own the requirements; this section only fixes the process:

1. **Deliverable 1 is a design doc** (`docs/` or a DESIGN.md section), reviewed by the operator
   before any code. It must cover: the auth-first ordering (ROADMAP "real difficulty axis"),
   how `node new` / `node adopt` become modes or entry points of `node add`, radio selection via
   the vendored `networks.yaml` presets (no raw `FREQ,BW,SF,CR` prompts; keep the tuple as an
   escape hatch), and the wizard writing `secrets.yaml` itself instead of printing snippets.
2. Implementation lands in slices, each keeping the old commands working as aliases until the
   pipeline covers their mode.
3. Live verification on the luna bench node per CLAUDE.md rules; record observations in
   JOURNAL.md.

The C2 module split of `cli.py` (3,175 lines) has no operator value on its own — fold the
commissioning-related extraction into this slice when the wizard is rewritten anyway; leave the
rest of C2 opportunistic.

---

## Explicitly not now (so nobody "helpfully" adds them)

- **Web UI / map** — operator decision: not until CLI + API are down pat.
- **Telemetry history / trend graphs** (the meshmonitor-graphs feature) — real feature with real
  storage decisions (`node_facts` overwrites; only `setting_history` is append-only). It should
  be the first *consumer* of the stable `--json`/API layer, after slice 3, not part of it.
- **Remaining C2 splits** (`queue.py`, `daemon._dispatch`) — dedicated mechanical PR, whenever.
- **New alerting transports** (mesh broadcast, room server) — ROADMAP Direction 4 territory;
  exit-code-2-in-cron is the alerting story for now.

## Definition of done, per slice

- All gates green (`pytest`, `ruff check .`, `mypy src`).
- CHANGELOG entry; deprecations called out loudly.
- The corresponding BACKLOG items moved to "Done Recently" (not duplicated).
- Help output eyeballed (`--help` for every touched command) — rendering bugs do not show up in
  source review.
- No mesh traffic sent except slice-4 bench verification on luna.
