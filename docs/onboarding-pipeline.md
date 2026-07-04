# Design: `mch node add` — one onboarding pipeline

**Status: DRAFT for operator review (RECOMMENDATIONS-2 slice 4, Deliverable 1).**
No code lands until this is reviewed and approved. This document fixes the *process* that
ROADMAP Direction 1 and BACKLOG Phase 7 already asked for; it does not re-argue the requirements.

Related: `ROADMAP.md` "Direction 1: One onboarding pipeline, not four commands", BACKLOG
"`node add` onboarding pipeline (Phase 7 / ROADMAP Direction 1)" and the "Operation Safety And
Reachability" auth-first items, `DESIGN.md` "Two-layer commissioning", and `src/mch/commission.py`.

## 1. Problem

Onboarding is fragmented across four bespoke flows — `mch node new` (new mode), `mch node new`
(replace mode), `mch node adopt`, and *nothing at all* for an already-known node (case #3,
faulkland) — plus hand-editing `secrets.yaml` and typing raw `--radio FREQ,BW,SF,CR` tuples. The
operator's report: *"the workflow feels clunky."* ROADMAP Direction 1 diagnosed this in June; it is
still open.

The insight from Direction 1: cases #1/#2/#3/#3a are **the same operation with different identity
front-ends**. Every one must end at the same place:

> a DB record + a working **management channel** (reach-radio + route + auth) + initial facts +
> converged config.

Only the identity step at the front differs.

## 2. Target command

One entry point with explicit modes (mode may be chosen interactively when omitted):

```
mch node add                      # interactive wizard: asks which mode, then walks it
mch node add --new                # factory-fresh: generate/accept identity, then converge
mch node add --replace NODE       # restore a known identity onto fresh hardware from secrets
mch node add --adopt PUBKEY       # already-running node discovered on the mesh/bench
```

`mch node new` and `mch node adopt` become **thin aliases/entry points** into `node add`'s
respective modes, kept working until the pipeline covers each mode, then deprecated (hidden alias
for one release, per the project's breaking-change rule — no release has been cut carrying the
replacements yet, so aliases are mandatory, not optional).

The three modes converge on one shared tail (§5): auth → routing (only if needed) → facts →
converge → completion check.

## 3. The difficulty axis is auth first, then routing (settled by faulkland)

This is the load-bearing correction from ROADMAP Direction 1, and it reshapes the pipeline. Two
*independent* variables decide whether onboarding works; a no-response timeout cannot tell you which
one failed, so the pipeline must **isolate them in order**:

1. **Authentication / ACL — the real blocker.** A secured node silently *drops* a directed command
   from a peer not in its ACL: no reply, no permission error, just a timeout — indistinguishable
   from unreachable. So the gateway must **log in at least once** (entering the node's ACL) before
   any directed command is expected to answer. `_send_authed` today only re-authenticates on an
   *explicit* permission error, which a silently-dropping node never sends. The pipeline must log in
   *proactively* when a password is configured and the gateway is not yet `auth_cached`.
2. **Routing — real but secondary.** Whether the gateway has a usable `out_path`. Genuine unrouted
   nodes exist and need path repair/discovery, but routing is only investigated **after** a
   successful login still fails to get directed commands through. The existing route tooling
   (`node adopt`'s direct-route install, `node probe --radio [--advert] [--login]`) is reused here —
   it was just previously pointed at the wrong cause.

**Scope boundary (unchanged, ROADMAP + BACKLOG "Won't Do"):** a node both unknown to the gateway and
out of direct radio range stays out of *remote* onboarding scope — carry it to the bench.

## 4. Radio selection: presets, not raw tuples

No raw `FREQ,BW,SF,CR` prompt as the primary path. The wizard offers the vendored regional presets
already shipped in `src/mch/data/networks.yaml` (loaded by `recommend.load_network_radio_presets`,
the same data `mch node new`'s radio prompt already uses) — Australia, New Zealand, EU/UK,
USA/Canada, etc. — each mapped to a `freq,bw,sf,cr` tuple.

- **Default** offered: the gateway's home radio / the group's desired radio, so the common case is a
  single confirm.
- **Escape hatch retained:** an explicit "custom" choice still accepts a raw `freq,bw,sf,cr` tuple
  for bench/split-mesh work. The presets are the friendly path, not a cage.
- **Later (out of scope here, tracked in BACKLOG "External catalog integration"):** location-aware
  suggestion — pick the region's radio by point-in-polygon on the node's `lat`/`lon`. The design
  should leave a seam for it (the preset list is already the right abstraction) but not build it.

## 5. Shared onboarding tail (all modes end here)

After the identity front-end, every mode runs the same sequence:

1. **DB record** — upsert the node (full pubkey is the identity), label, group, chosen reach-radio.
2. **Auth first** — if a password is configured and the gateway is not yet in the node's ACL, log in
   proactively (enter the ACL). Do not rely on a bare probe.
3. **Routing only if needed** — if directed commands still fail after login, repair/discover the
   path; on the bench, install a direct (zero-hop) route on the node's radio.
4. **Initial facts** — collect the baseline (`ver`, `board`, `role`, …) once reachable.
5. **Converge** — apply the resolved desired config for the node's group/global scope.
6. **Completion check (new, from ROADMAP Direction 1)** — verify access actually works rather than
   assuming: try a wrong password (expect rejection), the right password (expect success), no
   password (is the gateway already in the ACL / is the node open?), and the guest password. This is
   the reliable way to distinguish a genuinely secured node from one that merely has the gateway in
   its ACL — the missing piece that made the old open-node detection unreliable.

## 6. The wizard writes `secrets.yaml` (open decision — needs operator sign-off)

S4 asks the wizard to **write `secrets.yaml` itself** instead of printing a snippet for the operator
to paste. This collides head-on with a standing rule and must be resolved before any code:

> **BACKLOG "Won't Do / Not Yet":** Do not auto-edit `config.yaml` or `secrets.yaml` from `mchd`;
> they remain operator-owned source files until a reviewed, audited configuration-write workflow
> exists.

Reading, and a proposed resolution to confirm:

- That rule is about the **daemon** (`mchd`) auto-editing files behind the operator's back. The
  onboarding wizard runs in the **`mch` CLI** — operator-invoked, interactive, foreground. A
  client-side wizard writing `secrets.yaml` with an explicit confirmation is a *different actor* from
  the daemon silently rewriting config during a sync.
- **Proposed guard rails** (this is the "reviewed, audited configuration-write workflow" the rule
  asks for, made concrete):
  - Only the `mch` client writes it, only during an interactive `node add`, only after showing the
    exact diff and getting a `--yes`/confirm (matching the slice-2 mutation convention).
  - Preserve mode `0600` and ownership; never widen permissions.
  - Never overwrite an existing entry silently — append/merge, and refuse to clobber a differing
    prv_key without an explicit `--force`.
  - Write via temp-file-then-rename so a crash never truncates a good `secrets.yaml` (the same
    pattern `systemd/mch-backup.sh` already uses).
  - Redact secrets from all echo/log output (the wizard already redacts `set prv.key`).
- **Decision needed from the operator:** approve the client-side-wizard carve-out with these guard
  rails, or keep printing snippets. **If not approved, the wizard keeps printing the `secrets.yaml`
  snippet** (today's behaviour) and only the fragmentation/auth/radio parts of this design proceed.
  This is the one item flagged in RECOMMENDATIONS-2 as "if a decision proves wrong, stop and ask" —
  so it is surfaced here rather than chosen unilaterally.

## 7. Delivery plan (each slice keeps the old commands working)

Per RECOMMENDATIONS-2 slice 4 and the project's small-verified-slices rule:

1. **This doc, reviewed.** ← we are here. No code until approved.
2. **Pipeline skeleton + shared tail (§5)** behind `mch node add`, with `--new`/`--replace`/`--adopt`
   delegating to the existing `commission.py` flows unchanged. `node new`/`node adopt` become
   aliases. No behaviour change yet — just the single entry point and the shared tail extracted.
3. **Auth-first ordering (§3)** — proactive login for password-configured, non-`auth_cached` nodes,
   wired into the tail and the reachability probe. Bench-verified on luna (test freq `915.800`),
   observations recorded in `JOURNAL.md` before any rule/constant. This is the highest-value fix
   (it is what stranded faulkland).
4. **Preset radio selection (§4)** in the wizard; raw tuple demoted to a "custom" escape hatch.
5. **Completion check (§5.6).**
6. **`secrets.yaml` writing (§6)** — *only if* the operator approves the carve-out.
7. Fold in the opportunistic `cli.py` commissioning-code extraction (part-1 C2) while the wizard is
   being rewritten anyway; leave the rest of C2 for its own PR.

Live verification (steps 3+) happens on the luna bench node only, per CLAUDE.md — never the field
frequency `915.075`.

## 8. Explicitly out of scope here

- Location-aware radio suggestion (BACKLOG "External catalog integration") — leave a seam, don't
  build.
- DB-primary secrets storage (ROADMAP Direction 2 / the protected-secret-storage item) — secrets
  stay in `secrets.yaml`; this design does not move them into the DB.
- Onboarding a node that is both unknown to the gateway and out of direct radio range — permanently
  out of scope (BACKLOG "Won't Do").
