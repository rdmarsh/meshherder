# Recommendations Part 3 — output UX and complexity pay-down

A follow-up review of Mesh Herder at **v0.22.0** (commit `88b40bc`, 2026-07-05), written — like
parts 1 and 2 — as a work plan another AI model or human can execute item by item.

**Context from the operator (rdm, 2026-07-05):** a live session transcript from luna (14 nodes,
12 shown unreachable) with two explicit asks:

1. `mch config show corbin` should show everything by default instead of erroring with
   `Missing argument 'KEY'`.
2. A general UI review, plus "review anything else that you think needs doing — it's become
   pretty complex."

**Evidence:** in addition to the transcript, a full read-only UI survey was captured on luna
(2026-07-05) as `mch-ui-survey.txt`. The survey and its generator script (`tools/ui-survey.sh`)
are deliberately **untracked and gitignored** — both contain real fleet node names/pubkeys.
They exist in the working tree on the operator's machines; every survey finding below quotes
the relevant evidence inline, so the file is corroboration, not required reading.

**Baseline at review commit:** `pytest` 960 passed, `ruff check .` clean, `mypy src` clean.
Nothing here is a breakage fix; it is output UX and structural debt.

**Diagnosis.** Part 2's structural work landed: the nouns are right, the vocabulary help exists,
`--json` covers scripting. What the live transcript shows is that the *output* now lags the
command surface, in four ways:

1. **`mch config show` answers only one question** (one key, one node) and cannot answer the
   two the operator actually reaches for: "what is configured on this node?" and "what did I
   set globally / on this group?". There is no view of desired state at a scope at all —
   `config export` is a backup, not a view.
2. **The high-volume views don't scale to the real mesh.** With a realistic fleet (12 of 14
   nodes in problem states) the `mch status` footer's "problem states" table repeats the STATE
   and LAST SEEN columns of the main table row-for-row, and the full drift table dwarfs
   everything — `mch fleet issues` already exists as the dedicated issues view. Worse, the
   survey shows `mch gateway contacts` printing a ~10-line adopt recipe for each of the **337**
   repeaters the gateway can see: ~3,400 lines where a table belongs.
3. **Stale observations are shown without enough honesty to avoid contradiction.** corbin's
   facts table says the clock is "slow by 51m" (observation from 2 days ago) while recent
   activity shows `clock sync` failing three days running with `ERR: clock cannot go backwards`
   — which means the clock is now *fast*. The fact is only refreshed after a *successful* sync,
   so the display can never catch up. Similarly, "unreachable" rows carry no "as of when we
   last tried", and nothing in the CLI teaches what the states mean.
4. **Internal complexity has outgrown the file layout.** `cli.py` is 3,356 lines, `queue.py`
   2,468, `daemon._dispatch` is a ~700-line if-chain, and the report model reaches the terminal
   through two stacked adapter layers plus three dead RPC verbs left over from the HTTP-API
   retirement. All of this was deferred from parts 1 and 2 (item C2); with the operator now
   naming complexity as the problem, it stops being deferrable.

This document sequences the fix as five slices. Slice 1 is the direct request; do it first.

## How to use this document (instructions for the executing agent)

- **Read `CLAUDE.md` first** and follow it: TDD (failing test first, confirm it fails for the
  expected reason), small verified slices, preserve debug logging and comments, record rationale
  in `JOURNAL.md`/`DESIGN.md`.
- **Branch policy** (operator preference): each slice is a branch + PR merged `--no-ff`;
  within a slice keep commits small and gates green.
- **Gates** for every PR: `pytest` (960 passing at baseline), `ruff check .`, `mypy src`.
- **No mesh traffic needed** for any slice except S3.1's live confirmation, which uses the luna
  bench node on the test frequency `915.800` only (never the field frequency `915.075`).
- **Check `BACKLOG.md` before starting an item.** Overlaps are flagged inline below
  (clock remediation, the C2 splits, "explain partial status" and stale-fact items). When you
  pick one up, resolve it in one place — do not track progress in two files.
- **Decisions below are made, not open.** If a decision proves wrong during implementation,
  stop and ask the operator; do not silently choose differently.
- **RECOMMENDATIONS-2 slice 4** (the `mch node add` onboarding pipeline,
  `docs/onboarding-pipeline.md`) is still awaiting operator review. It is *not* superseded by
  this document; do not start it from here.

## Ground rules for this sweep (operator-stated, not negotiable)

**1. Alpha software — no backwards compatibility, ever.** The operator has said this
repeatedly: there are no external consumers, and compat shims are part of the complexity being
paid down. When you change any CLI/RPC/JSON surface, **delete the old path in the same
change** — no deprecation aliases, no legacy adapters, no dual payload shapes, no transition
periods. Migrate tests to the new shape; never keep a production fallback because a test feeds
an old shape. (This hardens part 1's "alpha software" rule from a preference into a rule.)

**2. JSON-first.** The architecture the project already committed to (part 2 slice 3 +
`report.py`) is the rule for *all* read surface from now on. Every read command must:

  (a) get its data from the daemon as **one documented JSON shape** (the report model or a
      sibling shape in `report.py`);
  (b) render its terminal output as a **pure function of exactly that shape** — no extra RPC
      fields the JSON doesn't carry, no renderer-side lookups;
  (c) offer `--json` emitting that shape **verbatim**.

Query and presentation stay split *inside* the one `mch` binary: `mch … --json | jq` is the
unix seam for anything bespoke. Do **not** split into separate query/render tools or add a
templating layer — the daemon owns mesh+DB, the CLI presents the model, jq composes.
S4.1/S4.4/S4.5 below bring the existing stragglers under this rule.

---

## Slice 1 — `mch config show` grows up (the direct request)

The `config` noun has a complete write grammar (`set`/`unset` at node/group/global scope) but a
one-key read. Complete the read side to mirror it.

### S1.1 Make KEY optional: `mch config show NODE` lists every key

**Behavior.** With no KEY, show one row per key that resolves for the node — the same key set
`mch node show` renders (configured keys plus observed-only leftovers), but oriented around
*resolution* rather than node health:

```
settings for corbin (e2601d591517), group 'deployed':

  KEY                    POLICY   DESIRED               FROM    OBSERVED              AGE
  ---                    ------   -------               ----    --------              ---
  advert.interval        enforce  238                   global  238                   2d
  lat                    enforce  -33.732               node    -33.7319984           2d
  name                   enforce  🦷 Quakers Hill Corb  node    🦷 Quakers Hill Corb  2d
  owner.info             enforce  🦷 rdm                global  🦷 rdm                3h
  ...

(`mch config show corbin KEY` traces one key through node/group/global)
```

The **FROM** column (which scope supplied the winning desired value) is the reason this view
exists alongside `mch node show`: `node show` answers "how is the node doing", `config show`
answers "what did I ask for and where did each value come from". Rows with no desired value
(observed-only history) show `-` in DESIRED/FROM.

**Directives must be visible.** Survey evidence (`mch config show corbin name`): the scope
trace shows the stored row `desired='Quakers Hill Corb'` while the resolved value is
`'🦷 Quakers Hill Corb'` — the group's `name_prefix` directive was applied with nothing on
screen explaining the transformation. Wherever a directive (`name_prefix`, `default_policy`,
`via_radio`) changes a resolved value or policy, the view must say so, e.g.
`desired='🦷 Quakers Hill Corb'  (node value + name_prefix '🦷 ' from group 'deployed')` in
the single-key trace, and a `*` marker with a footnote in the all-keys table.

**Implementation.**
- Daemon: make `key` optional in the `show_setting` RPC (`daemon.py:434
  _handle_show_setting`). With no key, iterate
  `all_setting_keys(node_cfg, group_cfg, global_cfg) | known-settings keys` and return a list of
  per-key resolutions (reuse the existing per-key logic; the `desired_source` loop already
  computes FROM). Keep the single-key response shape unchanged for the existing call.
- CLI: `@click.argument("key", required=False, default=None)` on `show_setting`
  (`cli.py:2407-2412`); render the table with the `_wlen`/`_wpad` helpers (emoji-safe) and
  `--json` emitting the list verbatim.
- Tests first: CLI table render, daemon list resolution (node beats group beats global), JSON
  shape, and the no-arg case in the socket contract test.

### S1.2 Scope listing: `mch config show --group NAME` and `mch config show --global`

Complete the grammar triangle — everywhere you can `set`/`unset` you can now `show`. Lists the
desired/policy rows stored *at that scope* (not resolved through nodes):

```
$ mch config show --global
global settings (apply to every node unless a group or node overrides):

  KEY              POLICY   DESIRED
  ---              ------   -------
  owner.info       enforce  🦷 rdm
  ...
```

Reuse `_scope_from_flags` (`cli.py:2255`) so the flag rules match `set`/`unset` exactly
(`--group`+`--global` is a usage error; NODE positional is a usage error with either flag).
Daemon side is a straight `db.get_desired_settings(scope, scope_id)` dump. Error out with the
existing "group has no member nodes" wording when the group is unknown, for consistency with
`set`.

### S1.3 Polish the single-key view

Fixes to `show_setting` (`cli.py:2412-2449`, daemon `_handle_show_setting` at `daemon.py:434`):

- **Validate KEY.** Survey evidence: `mch config show corbin node.owner` (not a real key)
  happily reports "not configured … resolved: policy=enforce desired=-". `config set` rejects
  unknown keys with the valid-key list; `config show` must apply the same check
  (`settable_keys() | readable_observation_keys()`) and exit 1 for an unknown key. A *valid*
  key with nothing set keeps the friendly "nothing is set" path.
- **Don't present defaults as configuration.** For a valid-but-unset key, `policy=enforce` is
  just the inherited default; render it as `policy=enforce (default)` so it can't be read as
  an operator-set row.
- **Friendly timestamps.** `observed: '🦷 rdm'  [2026-07-05T10:42:59.908027+00:00]` is the only
  raw ISO timestamp left in the terminal UI. Use `_ago_with_ts` like every other view:
  `observed: '🦷 rdm'  (2026-07-05 20:42:59, 3h ago)`.
- **Operator wording.** "not configured for this node (no desired/policy row resolves at any
  scope)" is implementer-speak (the help-text banned-jargon guard doesn't cover runtime output).
  Replace with: `nothing is set for this key — not on the node, its group, or globally
  (mch config set corbin KEY VALUE to set it)`.
- **Label the trace.** Rename the `scopes:` block heading to `where it's set:` and keep the
  most-specific-wins order comment in the code.

---

## Slice 2 — high-volume views stop drowning the signal

### S2.1 `fleet status` footer becomes a summary; `fleet issues` keeps the tables

**Decision:** `mch status` / `mch fleet status` stops printing the full drifted-settings and
problem-states tables. After the node table and count it prints at most three summary lines:

```
issues: 17 drifted settings on 4 nodes · 12 nodes in problem states  (`mch fleet issues` for detail)
open nodes: 2 with no ACL — anyone on the mesh can send them commands  (`mch fleet issues`)
firmware: 8 nodes behind latest v1.16.0  (advisory — `mch node show NODE` for detail)
```

Rationale: the STATE column already marks every problem node in the main table; the footer
tables added only repetition (in the transcript, 30+ of the ~60 output lines). `mch fleet
issues` keeps the full tables and the exit-2 contract unchanged — it exists precisely to be the
detail/alerting view. The open-nodes line stays prominent (it is the one security-critical
category) but as a one-liner.

Implementation anchor: `status` (`cli.py:724-813`) stops calling `_render_issue_sections` and
gains a `_render_issue_summary`; `issues` (`cli.py:820`) keeps `_render_issue_sections`
unchanged. Update the `mch.1` man page text that says status shows the issues footer, and the
docs-drift guard fixtures if they pin it.

### S2.2 Sort the fleet table deliberately

`get_all_nodes` orders by mesh **name** (`db.py:282 ORDER BY name`), which with empty names,
`💔`/`🦷`/`📡` prefixes, and mid-life renames reads as random — the transcript's table has
deployed, predeployed, and rdmtest nodes interleaved. **Decision:** the fleet table sorts by
**group, then label** (empty groups last, case-insensitive). Sort in the CLI renderer (or
report builder), *not* in the DB query — `get_all_nodes` feeds the scheduler and changing
work-queue iteration order is out of scope for a display fix. Bonus: grouped rows make the
`rdm_test` vs `rdmtest` typo (visible in the transcript) jump out on its own.

### S2.3 States get taught: `mch help states`

Nothing in the CLI explains `unreachable` vs `offline` vs `partial` vs `configured` — the
words carry the whole meaning of the STATE column and the "problem states" section. Add a third
help topic beside `selectors`/`concepts` (`cli.py:463-535`), sourced from the `NodeState`
docstrings (`models.py:13-22`), in operator wording, e.g.:

```
unreachable  mch tried to reach it on its last turn and got no answer.
             The mesh is not a datacenter: a solar node sleeping overnight
             shows unreachable until its next successful sync.
partial      reached, but some settings are still pending or failed.
configured   reached, and everything you asked for matches.
...
```

Include one line on TRIES (see S2.4) and register the topic in the banned-jargon guard test.

### S2.4 Say when "unreachable" was decided, and why drift has 0 TRIES

Two honesty gaps in the same transcript:

- **"unreachable … LAST SEEN 2h ago"** looks contradictory (it advertised 2h ago but did not
  answer when tried, possibly many hours ago). Record the timestamp of the last reachability
  *attempt* (new nullable `nodes.last_attempt_at`, set where the probe outcome is decided in
  `queue.py` ~1614/1669) and render it in the `fleet issues` problem-states table as a `TRIED`
  column, and in `node show` as a `last tried:` line. Named DB migration per the existing
  `PRAGMA user_version` scheme.
- **Drifted enforce rows showing `TRIES 0`** for unreachable nodes read as "mch isn't doing
  anything". One footer line under the drifted table in `fleet issues` when any enforce row has
  0 tries: `enforce drift is corrected on the node's next successful sync; TRIES counts write
  attempts`. (This also covers the BACKLOG item "Explain partial status more directly…" —
  resolve them together.)

### S2.5 Fleet-wide-outage advisory

12 of 14 nodes unreachable is far more likely one gateway/radio problem than twelve node
problems. When **≥ 3 nodes and ≥ half** of non-retired nodes are `unreachable`/`offline`, print
one advisory line after the issue summary:

```
note: 12 of 14 nodes unreachable — if unexpected, check the gateway (`mch daemon logs`, `mch gateway contacts`)
```

Advisory only — never a new issue category, never exit 2 (same rule as the firmware footer,
`report.py:_firmware_report` docstring). Compute it in the CLI from data already in the report;
no daemon change.

### S2.6 `mch gateway contacts` becomes a table

Survey evidence: on luna the gateway sees **337** unmanaged repeaters, and `gateway contacts`
prints a ~10-line commented adopt recipe for each — **~3,400 lines** of output
(`mch-ui-survey.txt`, "previews and read-only diagnostics"). The recipe format was designed for
a handful of unfiled contacts; at mesh scale it is unusable.

**Decision:** the default output is a compact table sorted by name —

```
  PUBKEY        NAME
  ------        ----
  92b9e6368340  KLF XIAO Meshcore
  ...
337 unmanaged repeaters visible to the gateway
(`mch gateway contacts NAME` prints the adopt recipe for matches)
```

— and the full adopt recipe (pubkey, `mch node adopt …` command, secrets.yaml stanza) is
printed only for contacts matching a QUERY (the argument already exists, `cli.py:1432-1435`).
Cap recipe output at a handful of matches; above that, show the table filtered by the query.

### S2.7 Small output polish (batch into any slice-2 PR)

- **Zero-match filter should fail loudly.** `mch fleet status nosuchnode` prints
  `no nodes matching 'nosuchnode'` to stdout and exits 0 (survey). Send the message to stderr
  and exit 1, matching the selector-not-found behavior of `node show` — a typo in a script
  should not look like success.
- **Blank mesh name renders as nothing.** `mch node show 9635` prints `mesh name:` followed by
  emptiness for a never-contacted node (survey). Render `-` like every other absent value.
- **`config set`/`unset` usage lines leak the nargs hack.** Errors print
  `Usage: mch config set [OPTIONS] ARGS...` (survey) — set an explicit
  `options_metavar`/argument metavar so usage reads `mch config set [OPTIONS] [NODE] KEY
  [VALUE]`, matching the help body.
- **Exit-code help is incomplete.** `mch help concepts` says exit 2 means "issues present
  (`mch fleet issues` only)", but click also exits 2 for any usage error (survey: bare `mch`,
  `config show corbin`, bad `config set` key). Add one line: `2 also = command-line usage
  error (bad arguments)`.

---

## Slice 3 — stale observations must not contradict reality (the clock case)

### S3.1 Refresh the clock fact after a *failed* `clock sync` too

`_refresh_clock_fact_after_sync` (`queue.py:2422`) only runs on success (`queue.py:2406`), so a
node whose sync keeps failing keeps its pre-failure clock fact forever — corbin shows
"slow by 51m" from two days ago while the failure text proves the clock is now *fast*. Failing
`clock sync` is precisely when reading the clock has diagnostic value.

**Change:** on `clock sync` failure, also call the refresh (one extra read on an already-open
authed session; the per-op rate limiting already applies). The facts NOTE then recomputes via
`_clock_drift_text` and shows "fast by Xm", agreeing with the error. Add the failure-path test
first; confirm once on the luna bench node (915.800) since this touches live session flow.

*Related but distinct:* actual fast-clock remediation is already in BACKLOG ("Investigate safe
remediation for fast node clocks") with firmware evidence in `command_registry.py` — this item
is display honesty only; do not fold remediation into it.

### S3.2 Show scheduled operations and their outcomes in `mch node show`

corbin's `clock sync` failed three days running, visible only by reading the recent-activity
log lines. The `operations` data (command, last run, status, response) is already in the DB;
`node show` should render it as its own small section between `settings:` and
`recent activity:`:

```
operations:
  COMMAND     LAST RUN            STATUS  RESPONSE
  -------     --------            ------  --------
  clock sync  2026-07-04 (1d ago) failed  ERR: clock cannot go backwards
```

**Decision:** display only — repeated operation failure does *not* become a new issues category
in this slice (that is an alerting-semantics decision for the operator; note it in BACKLOG if
wanted).

---

## Slice 4 — complexity pay-down

The operator named complexity directly; the C2 module splits deferred from parts 1 and 2 are
now due. These are **pure-move refactors: one PR each, no behavior edits mixed in**, pinned by
the existing help-text/contract tests.

### S4.1 Delete the dead RPC verbs and collapse the adapter stack

Since the `--json` unification, the CLI reads exclusively via the `fleet` / `fleet_node` RPCs
(`cli.py` grep confirms), but the daemon still serves `status` (`daemon.py:689`), `plan`
(`daemon.py:943`), and `issues` (`daemon.py:1178`) through `report.legacy_status` /
`legacy_plan` / `legacy_issues`. With the HTTP API retired there is no other client.

- Remove the three RPC verbs and their `legacy_*` builders (`report.py:104-137`) plus tests.
- `cmd: "node"` is still used by `node exec` (`cli.py:2224`) via `legacy_node`: migrate exec to
  `fleet_node` and delete `legacy_node` in the **same PR** (`report.py:139-175`), then delete
  the CLI-side legacy branches (`_status_view`'s `"schema_version" not in fleet` fallback,
  `cli.py:616`; `_node_detail_view`'s `"auth" not in node`, `cli.py:643`) —
  update the tests that feed legacy shapes rather than keeping production fallbacks for them.
- Also fix the stale comment in `_emit_json` (`cli.py:686-691`): it still says the shapes are
  "the same objects the HTTP API serves".
- Ground rule 1 applies in full: no deprecation aliases, no transition period, delete in the
  same change. While here, retire the hidden `node remove` alias kept after part 2 slice 2
  (`cli.py:1912`): it still carries the only `--force` escape hatch for deleting a
  still-managed node, so move `--force` onto `node prune NODE` and delete `remove`.

### S4.2 Split `cli.py` (3,356 lines) into a package

`src/mch/cli/` with modules matching the noun groups the operator already knows:
`__init__.py` (root group, `main`, shared `_rpc`/`_check` plumbing), `render.py` (the `_w*`,
`_ago*`, table renderers), `fleet.py`, `node.py`, `config_cmds.py`, `daemon_cmds.py`,
`gateway.py`, `wizard.py` (the ~500-line commission wizard), `help_topics.py`. Keep
`from mch.cli import main` working for the console-script entry point. The help-text,
banned-jargon, and docs-drift guard tests pin the surface; run `mch --help` diffs before/after.

### S4.3 `daemon._dispatch` if-chain → handler table

The ~700-line if-chain (`daemon.py:1041` region) becomes a `dict[str, Handler]` of the
already-extracted `_handle_*` coroutines; extract the remaining inline blocks (`clean_*`,
`remove`, `retire`/`unretire`, `set_group`/`set_label`, …) into handlers of the same shape as
`_handle_show_setting`. Mechanical; the socket contract tests pin behavior.

### S4.4 Bring the JSON-first stragglers under ground rule 2

These read commands return bespoke RPC payloads with no `--json` and no documented shape —
each is a place where the query/render split doesn't exist yet: **`node path`** (`cmd: path`),
**`gateway contacts`** (`cmd: contacts` — do together with S2.6, which reshapes its output
anyway), **`node discover`** (`cmd: discover`), **`fleet suggest`** (`cmd: suggest`),
**`node probe`** (`cmd: probe`), and the **preview payloads** of `node prune` / `node clean` /
`config import`.

For each: document the shape (a short docstring on the daemon handler is enough), add
`--json` emitting it verbatim, and make the terminal renderer a pure function of it. Where the
current payload is awkward, fix the shape *now* (ground rule 1 — no dual shapes). Extend the
existing socket contract test to cover each verb's shape.

### S4.5 `--json` emits readable UTF-8

Survey evidence: every emoji in `--json` output arrives as ASCII surrogate escapes
(the 🦷 in names comes out as backslash-u `d83e`/`ddb7` pairs) — `json.dumps` defaults to
`ensure_ascii=True`. This fleet's names are emoji-heavy and every
consumer is UTF-8 (jq, terminals, files). Set `ensure_ascii=False` in `_emit_json`
(`cli.py:692`) and pin it in the JSON contract test.

### S4.6 `queue.py` (2,468 lines) — defer, with a tripwire

**Decision:** do *not* split `queue.py` in this sweep. It is the live-tested sync engine; a
mechanical split risks churn in the one module where line-level history matters most during
field debugging. Revisit when the facts-as-policy branch (BACKLOG, "Active — on a branch")
lands, since that work already reshapes the collection pass. If any slice above forces edits in
more than two distinct regions of `queue.py`, stop and propose the split first.

---

## Slice 5 — docs cleanup (small, do anytime)

### S5.1 `docs/man/mchd.8` still documents the retired HTTP API

Lines 27, 70-71, 88-100, 157 describe the `api:` config mapping and a "READ-ONLY API" section
removed in 0.22.0. Remove them and point scripting at `mch ... --json`. The docs-drift guard
(`tests/test_docs_cli_reference.py`) only checks `mch.1` — extend it (or add a sibling test)
to assert `mchd.8` mentions neither `api:` nor an HTTP listener, so retired daemon surface
fails the same way retired CLI surface already does.

### S5.2 Sweep the remaining prose for the retired API

`README.md`, `INSTALL.md`, `DESIGN.md`, and `www/index.html` grep clean for the HTTP API
already; re-check after S4.1 removes the legacy verbs (DESIGN.md's socket-protocol section may
list them).

---

## Noted, deliberately not recommended

Kept per the CLAUDE.md rule against silently dropping observations; each of these was
considered and rejected for now.

- **Group-name typo detection** (`rdm_test` vs `rdmtest` in the transcript): S2.2's
  group-sorted table makes it visible; edit-distance warnings are cleverness with maintenance
  cost.
- **A `mch fleet groups` listing** (groups + member counts): plausible future `fleet` verb,
  but no transcript evidence of need once `config show --group` (S1.2) and the sorted table
  exist. BACKLOG if wanted.
- **Changing the VER `!` marker** to a separate column or color: the footer explains it, and
  the marker is width-safe by design (part 2 S3.3). Leave alone.
- **Making repeated operation failures an issue category** (exit 2): alerting semantics belong
  to the operator; S3.2 surfaces the data without changing contracts.
- **`mch status` vs `mch fleet status` duplication**: intentional shortcut from part 2 S1;
  not revisited.
