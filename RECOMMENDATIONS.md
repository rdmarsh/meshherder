# Recommendations — project review, todo, missing, cleanup

A full-project review of Mesh Herder at **v0.20.1** (commit `5f9d65a`, 2026-07-03), written as a
work plan that another AI model (or human) can execute item by item. It covers four things:

1. **Broken / stale** — things that are wrong today, mostly fallout from the desired-state sunset.
2. **CLI redesign** — the command surface grew by accretion; commands are misplaced, misnamed, or
   inconsistent. A target command tree is proposed.
3. **Cleanup** — code-health work: oversized modules, type errors, tooling drift.
4. **Missing** — commands, tests, and docs that should exist and don't.

## How to use this document (instructions for the executing agent)

- **Read `CLAUDE.md` first** and follow it: TDD (failing test first), small verified slices,
  preserve debug logging and comments, record rationale in `JOURNAL.md`/`DESIGN.md`.
- **Branch policy** (operator preference): branch + PR for risky or large changes (anything in the
  CLI-redesign section); direct-to-main for small safe fixes (most doc corrections).
- **Check `BACKLOG.md` before starting an item.** Several findings below overlap items already
  tracked there — this doc cross-references them. When you pick an item up, move/merge it into
  BACKLOG rather than tracking progress in two places. Do not silently drop items from either file.
- **Do not send mesh traffic** to verify anything here. Everything below is verifiable with unit
  tests, `--help` output, or reading code. Live verification belongs on the luna bench node only,
  per CLAUDE.md.
- **Breaking CLI changes need aliases.** v0.20.0 already broke every invocation path once
  (`mch set` → `mch config set`). Any further rename must keep the old name as a hidden alias for
  at least one minor release, with a deprecation note in CHANGELOG.

## Review baseline

Verified in a clean Linux venv (Python 3.14, project targets 3.11+):

| Gate | Result |
|---|---|
| `pytest -q` | 893 passed |
| `ruff check .` | clean |
| `mypy src` (mypy 2.1.0) | **7 errors in 3 files** — see Cleanup C1 |

CI (`.github/workflows/ci.yml`) installs unpinned `mypy>=1.10` on Python 3.11, so CI is likely
red (or will go red) for the same 7 errors.

---

## 1. Broken / stale (P0 — fix before anything else)

The 2026-06-17 sunset moved inventory and desired state from `config.yaml` into the DB, and the
daemon now **refuses to start** if `config.yaml` contains a non-empty `nodes:`, `groups:`,
`global:`, or `facts:` block (`src/mch/config.py:890-901`). Several operator-facing surfaces were
never updated and now actively instruct operators to brick their daemon.

### B1. `examples/config.example.yaml` makes the daemon refuse to start

The reference example — which `INSTALL.md:62` says to `install … /etc/meshherder/config.yaml` and
`README.md:103` says to copy and edit — contains uncommented `facts:` (line 83), `global:` (142),
`groups:` (164), and `nodes:` (220) blocks. A fresh install following the docs cannot start `mchd`.

**Fix:** split the example into two files:
- `examples/config.example.yaml` — daemon settings only (gateway, scheduler, messenger, api, paths).
- `examples/desired.example.yaml` — the nodes/groups/global/facts material, documented as input
  for `mch config import`, not as a live config file.
Update INSTALL.md and README to match. Add a test that loads the shipped example with
`load(strict=True)` so this can never regress.

### B2. `mch node contacts` is doubly broken post-sunset

- **Wrong filter:** `get_unfiled_contacts()` (`src/mch/queue.py:2439-2456`) filters gateway
  repeater contacts against `config.nodes`, which is always empty under the strict daemon — so
  every repeater contact, including fully managed nodes, is listed as "not yet managed".
  It must filter against DB inventory instead.
- **Wrong instructions:** the command prints "config.yaml — append to the 'nodes:' section"
  snippets (`src/mch/cli.py:1305-1311`). Following them makes the daemon refuse to start. It
  should print either an importable YAML doc (for `mch config import`) or, better, the direct
  command (`mch node adopt …` / a future `mch node add`), plus the `secrets.yaml` snippet, which
  is still correct.

Also update the command's help text ("not yet in your config.yaml" → "not yet in the DB") and
`DESIGN.md:381-386` ("`mch node contacts` vs the DB"), which still describes the config-filter
behaviour as intended.

### B3. README contradicts itself about the source of truth

- `README.md:106` — "`config.yaml` and `secrets.yaml` are the source of truth for desired state"
  — directly contradicts the "Editing desired state in the database" section further down and the
  daemon's startup refusal.
- The "minimal working config" (`README.md:115-140`) includes `global:` and `nodes:` blocks the
  daemon rejects.
- "First run — discover and add nodes" (`README.md:240-264`) tells the operator to paste
  `mch node contacts` snippets into config.yaml and rely on hot reload — the pre-sunset flow.
- "Changing a setting fleet-wide" (`README.md:614-617`) says "add the setting to a group in
  config.yaml … restart mchd" — should be `mch config set --group …`.
- "Building a node on the bench" (`README.md:303-307`) — same stale paste-into-config flow.

**Fix:** one pass over README rewriting the config/workflow sections around the DB-primary model:
config.yaml = daemon settings; DB = inventory + desired state, edited via `mch config set` /
`import`; secrets.yaml unchanged. Keep the passwords/secrets sections — they are still accurate.

### B4. Stale wording inside CLI output

- Fleet-status issue sections still print "ghost nodes (in local DB but not config.yaml)" and
  "unseen config nodes (in config.yaml but not known to gateway/DB)" (`src/mch/cli.py:1123,
  1155`) — `report.py` no longer computes them that way (`src/mch/report.py:43,77`). Reword to
  the DB-lifecycle meanings actually computed.
- Docs and messenger help still reference `mch issues`, which does not exist as a command
  (README.md:77, DESIGN.md several places). Either add `mch fleet issues` (see M2) or fix the
  references to `mch fleet status`.

### B5. Commission replace/scan still reads config inventory

Already tracked as the top item in `BACKLOG.md` ("Commission flow: finish the config.yaml
sunset"): `commission.py` `_commission_replace` resolves identity from `config.nodes` (empty →
replace mode errors out), and scan's managed-detection filters by config prefixes. Same class of
bug as B2. Fix both against the DB in one slice; `secrets.yaml` remains the prv_key source.

### B6. mypy gate is failing

See Cleanup C1 — listed here because CI advertises `mypy src` as a required gate.

---

## 2. CLI redesign (P1)

### Principles (derived from ROADMAP "simple yet powerful")

1. **A command lives under the noun it acts on**, not the noun it was implemented near.
2. **One selector grammar everywhere**: `NODE` = label | unique pubkey prefix | mesh-name
   substring. No command should accept only a full pubkey or only a prefix without a stated reason.
3. **One scope grammar everywhere**: `NODE | --group NAME | --all`. (`config set/unset` currently
   use `--global`; `clean` uses `--all`. Pick one — `--all` reads better for actions, `--global`
   for scope-setting; if both survive, document why.)
4. **One mutation convention everywhere**: destructive/mutating commands preview by default and
   execute with `--yes`. (Today: prune/clean/import preview-then-`--yes`; remove prompts
   interactively; exec `--yes` skips a typed-confirmation; path `--apply` prompts unless `--yes`.)
5. **Help text is written for the operator**, action-first, without internal jargon ("the DB
   source of truth", "eviction pin", "sunset").

### Current inventory and problems (30 commands)

| Command | Problem |
|---|---|
| `fleet status [NODE]` | Single match silently switches to a full node-detail view — magic. Node detail is a *node* operation. |
| `fleet sync [NODE]` | OK. Output is raw daemon string; fine but undocumented. |
| `fleet plan [QUERY]` | OK where it is. |
| `fleet logs [PUBKEY]` | Help says "by pubkey prefix" but the daemon resolves labels too (`daemon.py:1113`); argument is named `pubkey`. Align with the standard selector. |
| `node contacts` | Broken (B2). Also arguably a *gateway* command — it reads the gateway contact store. |
| `node discover --radio` | OK; part of the fragmented onboarding set (see below). |
| `node adopt PUBKEY --radio` | Requires full 64-char pubkey while every other command takes prefixes. Accept a unique prefix when it matches a recent discovery result. |
| `node probe NODE --radio` | OK. |
| `node path NODE [--discover/--advert/--apply]` | Three operations multiplexed through mutually-exclusive flags. |
| `node new` | Misnamed: it's the commissioning wizard (new **and** replace modes). "new" says nothing; SUGGEST #32 flagged this in May. |
| `node exec NODE CMD` | OK. |
| `node admin` | **Misplaced**: stops mchd and opens `meshcore-cli` on the *gateway* serial port. It never touches a managed node. |
| `node retire / unretire` | Good pair, good names. |
| `node prune` | Fleet-wide purge of retired nodes; no single-node form. |
| `node remove NODE` | Overlaps prune; different convention (interactive prompt vs dry-run). BACKLOG already asks to converge these. |
| `node clean facts/settings` | Scope grammar (`NODE | --group | --all`) differs from config's (`--group | --global`); "clean" vs "prune" vs "remove" is three words for "delete local DB rows". |
| `config validate` | Validates the **daemon's YAML file** — everything else in `config` acts on **DB desired state**. Post-sunset these are different things with the same noun. |
| `config set / unset / show` | Core model — good. `show` vs `fleet status` naming overlap (both "show state"); acceptable. |
| `config suggest` | Advisory report derived from observed neighbour counts. It reads fleet state and suggests; it does not touch config. Belongs with reporting. |
| `config import / export` | Underplayed: export is the **complete fleet backup** (the DB is the only copy of desired state). "config export" does not communicate "this is your disaster-recovery snapshot". |
| `daemon start/stop/restart/status/logs` | Good group, recent, consistent. |

### Proposed target tree

Nouns: keep `daemon`, `fleet`, `node`, `config`; add `gateway`. Moves marked ★ are breaking
(need aliases); additions marked +.

```
mch daemon    start | stop | restart | status | logs
              validate            ★ (from config validate — it checks the daemon's YAML)
              reload              + (SIGHUP wrapper; hot reload exists, no CLI verb for it)

mch fleet     status [QUERY]        (table + issues only; no more magic detail switch)
              issues              + (just the issue sections; docs already reference it)
              sync [NODE]
              plan [QUERY]
              logs [NODE]
              suggest [NODE]      ★ (from config suggest)

mch node      show NODE           ★ (the detail view now hiding in fleet status; alias there)
              add                 ★ (one onboarding pipeline per ROADMAP Direction 1:
                                     wizard modes new/replace/adopt; `node new`,
                                     `node adopt` become entry points/aliases into it)
              discover --radio
              probe NODE --radio
              path show|discover|apply   ★ (subcommands instead of flag multiplexing)
              exec NODE CMD
              retire | unretire NODE
              prune [NODE]        ★ (absorbs `node remove`; NODE form replaces it)
              clean facts|settings
              group NODE NAME     + (assign/move group — currently impossible post-sunset;
                                     already in BACKLOG)

mch config    set | unset | show    (DB desired state only — the noun now means one thing)
              export | import       (keep here OR promote to `mch backup export|restore`;
                                     decide once, then say "backup" loudly in help + docs)

mch gateway   admin               ★ (from node admin)
              contacts            ★ (from node contacts, fixed per B2)
```

Suggested sequencing (each its own PR):
1. **Fix before move**: B2/B5 behaviour fixes land under the current names.
2. **Low-risk moves**: `node admin`→`gateway admin`, `config suggest`→`fleet suggest`,
   `config validate`→`daemon validate`, with hidden aliases + CHANGELOG deprecations.
3. **`node show`** extracted from fleet status; fleet status keeps filtering, prints a hint
   ("use `mch node show X` for detail") for one release before dropping the magic switch.
4. **prune/remove convergence** and the scope/`--yes` grammar unification.
5. **`node add` onboarding pipeline** — largest piece; do last, aligned with ROADMAP Direction 1
   and the BACKLOG auth-first onboarding work.

### Cross-cutting CLI polish

- **Selector help**: write the NODE selector grammar once (epilog or a `mch help selectors`
  topic) and reference it from every command instead of restating variants of it.
- **`--json` output** on read commands (`fleet status/plan/issues/logs`, `node show`,
  `config show`). The API model (`report.py`/`api.py`) already produces the JSON — the CLI just
  needs to emit it instead of tables. Cheap, and unlocks scripting without enabling the HTTP API.
- **Exit codes**: document them (`0` ok, `1` error already; consider `2` = "issues present" for
  `fleet status`/`issues` so cron can alert).
- **Help-text pass**: every command's first line should say what the operator gets, not the
  implementation ("Record a desired setting in the DB (the source of truth)" → "Set a desired
  value for a node, group, or the whole fleet").
- **Shell completion**: click ships it free (`_MCH_COMPLETE`); document it in INSTALL.md.

---

## 3. Cleanup (P2)

### C1. Fix the 7 mypy errors (or pin the toolchain — preferably both)

mypy 2.1.0 on the current tree:

- `src/mch/cli.py:1017,1024` — `_discovery_type`/`_discovery_snr` call `int(value)`/`float(value)`
  on `object`.
- `src/mch/queue.py:393,401,408` — `SettingConfig | None` `.desired` access without narrowing.
- `src/mch/daemon.py:994` — `delete_fact(str | None, str | None)` where `str` is required.

All seven are genuine narrowing gaps, small to fix. Separately, pin CI tool versions (or add a
constraints file) so an upstream mypy/ruff release can't silently redden main.

### C2. Split the oversized modules

Long-standing (SUGGEST #14, when cli.py was 984 lines; it is now **3,009**):

- `cli.py` 3,009 lines → package: `cli/__init__.py` (group skeleton), `cli/render.py` (the ~15
  `_render_*`/width helpers), `cli/commission.py` (the wizard, ~500 lines), one module per noun
  group. Mechanical; tests (`test_cli.py`, 3,388 lines) should not need changes beyond imports.
- `queue.py` 2,456 lines → `scheduler` (due/reschedule/cycle), `node_processor`
  (`_process_node`/observations/operations, ~850 lines), `pathing` (path/radio recovery), keeping
  `WorkQueue` as the facade. Higher risk — do after the CLI split, with `test_queue.py` green at
  every step.
- `daemon.py` `_dispatch` is **783 lines** of `if cmd == …` (`daemon.py:664`). Replace with a
  handler-per-command dict (`dict[str, Handler]`); SUGGEST #2 asked for this in May.

### C3. Named DB migrations

`Database.open()` still applies ad-hoc `ALTER TABLE` patches. With the DB now the *only* copy of
desired state (post-sunset), unversioned schema evolution is a real operator risk. Add a
`schema_version` table + ordered migration list. (SUGGEST #12, BACKLOG "Later".)

### C4. Retire SUGGEST.md properly

README already labels it "historical". Findings still open in it that are not yet in BACKLOG:
#12 (migrations — see C3), #13 (profile cache path), #14 (module split — see C2), #15 (issue
assembly dedup), #16 (typed contacts), #19 (concurrency tests), #33 (partial-commission retry),
#35 (wizard sentinel values), #37-39 (field-aware validation). Move each to BACKLOG (or
deliberately reject with a note), then delete SUGGEST.md and its README table row. Per CLAUDE.md,
every item needs an explicit destination — no silent drops.

### C5. Small code items

- `cli.py` duplicates radio-tuple validation: local `_validate_radio_tuple` (line 2481) wraps
  `command_registry.parse/validate_radio_tuple`; three commands also repeat the
  `validate → parse → assert → unpack` dance. One helper.
- `_check_default_config_when_daemon_unavailable` (`cli.py:247`) suggests `try: mchd --help` when
  the daemon is down — the useful hint is `mch daemon status` / `mch daemon logs`, which now exist.
- The checked-in `.venv` is a macOS build; harmless but worth a DEVELOP.md note that Linux
  contributors must create their own (this review tripped over it).
- `tests/test_cli.py` (3,388 lines) should be split along the same seams as the cli package split.

---

## 4. Missing (P3)

### M1. Scheduled backup unit

Already top-of-mind in BACKLOG ("Installable scheduled-backup unit"). Since export **is** the
disaster-recovery mechanism, this outranks feature work: ship `systemd/mch-backup.service` +
`.timer` writing dated `mch config export` snapshots with keep-N rotation, plus an INSTALL section.

### M2. `mch fleet issues`

Docs, the messenger (`issues` DM command), and the API (`/issues`) all have an issues view; the
CLI only shows it as a footer of `fleet status`. Add the command (pure projection of existing
report data) and fix the stale `mch issues` doc references (B4) to point at it.

### M3. `mch node group` / label editing

Post-sunset there is no way to assign a node to a group or change its label except re-import or
re-commission (BACKLOG flags the group half). `mch node group NODE NAME` and either
`mch node label NODE VALUE` or `mch node group --unset`. Small, high operator value.

### M4. Test gaps

- **Example-config regression test** (see B1): strict-load every shipped example.
- **CLI↔daemon contract tests**: `test_cli.py` mocks `_rpc` with hand-built dicts; nothing
  asserts the daemon actually produces those shapes. A round-trip test over a real Unix socket
  with a stub gateway would catch schema drift (the `_status_view`/`_node_detail_view` projection
  shims in cli.py exist precisely because this has drifted before).
- **Concurrency tests** for scheduler vs manual sync vs exec vs commission ownership
  (SUGGEST #19, BACKLOG "Later") — the operation-lock behaviour is load-bearing and untested.

### M5. Docs-drift guard

The CLI reference block in README.md (lines 629-672), the man page (`docs/man/mch.1`), and the
website (`www/index.html`) each restate the command table by hand — that is how B2/B3 happened.
Either generate the reference from click's own help (small script, run in CI with a diff check)
or add a CI check that every `mch <group> <command>` mentioned in README exists in `mch --help`
output. Cheap insurance for a 30-command surface that is about to be reshuffled again.

---

## Suggested execution order

| Phase | Items | Size | Branch? |
|---|---|---|---|
| 1 | B1, B3, B4 doc/example fixes + example regression test | S | direct-to-main OK |
| 2 | B2 contacts fix, B5 commission-replace fix | M | branch + PR |
| 3 | C1 mypy fixes + CI pinning | S | direct-to-main OK |
| 4 | CLI redesign steps 1-4 (moves, node show, prune convergence, grammar unification) + M2, M3 | M-L | branch + PR each |
| 5 | C2 module splits (cli → queue → dispatch) | L | branch + PR each |
| 6 | M1 backup unit, M4 tests, M5 drift guard, C3 migrations, C4 SUGGEST retirement | M | mixed |
| 7 | `node add` onboarding pipeline (with BACKLOG auth-first work) | L | branch + PR |

Re-verify after each phase: `pytest -q && ruff check . && mypy src`, and for CLI changes a manual
`--help` walk of every group. Record anything learned from live behaviour in JOURNAL.md per
CLAUDE.md.
