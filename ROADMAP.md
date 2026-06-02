# Mesh Herder Roadmap

Forward-looking product direction - the "north star" and the major themes that organise the
backlog. This is intentionally higher-altitude than the other docs:

- **ROADMAP.md** (this file) - where we are going and why.
- **DESIGN.md** - how the system works *today* (stable architecture).
- **BACKLOG.md** - the concrete, prioritised work items.
- **CHANGELOG.md** - what has shipped.

When a roadmap direction turns into committed work, break it into BACKLOG items; when it ships,
record it in CHANGELOG and fold the now-current behaviour into DESIGN.

## North star

A management tool for fleets of MeshCore nodes that is **simple yet powerful** - Ansible/Salt for
the mesh. The operator should be able to find a node, bring it under management, configure it, and
keep it converged, without logging into each node by hand and without hammering the mesh.

The recurring tension is exactly that pairing: every "powerful" feature is tempting to add as a new
command or a new config knob, which erodes "simple". The design rule is to **add power by
extending one model, not by adding parallel commands.**

## Operating reality: the mesh is unreliable

This assumption sits under every other decision. The mesh is not a datacenter network:

- Links go down and come back. A node that is unreachable now may answer fine next cycle.
- Batteries run flat. Solar nodes are intermittently available by design.
- Commands time out, drop, or arrive out of order. A single timeout is not "the node is down".
- Routes go stale - a learned path can be correct yet still lose an isolated command.

So mch is built for **gradual, tolerant convergence**: cached observations with visible staleness,
useful failure-pattern history, retries that are bounded rather than aggressive, and "skip and try
again later" instead of "fail hard". Anything that treats one missed reply as a fault, or that
hammers a weak link to force a result, is working against the medium. This is the *why* behind
#7 (don't hammer the mesh) and the slow-convergence model in DESIGN.md.

## Status snapshot (2026-06-02)

| # | Capability | State |
|---|---|---|
| 1 | Factory-fresh node into management | Done - `mch new` (new mode) |
| 2 | Restore a known node onto fresh hardware | Done-ish - `mch new` replace mode (needs live retest) |
| 3 | Known node into management (e.g. faulkland) | **Gap** - no working over-mesh path yet |
| 3a | Adopt a node already out on the mesh | Mostly via config + sync when reachable; `mch adopt` seeds DB + flood contact only |
| 4 | Retrieve facts | Done |
| 5 | Configure node | Done (enforce / audit / drift) |
| 6 | Remove / decommission | Done - `mch remove` |
| 7 | Don't hammer the mesh | Done, strong - rate limit, reachability probe, randomized reads, bounded recovery |
| 8 | Be as simple as possible | Ongoing - see "simple yet powerful" tension above |
| 9 | CLI + API | Done - CLI over Unix socket; read-only HTTP API |
| 10 | API extensible to a web page | Open - API is the projection layer; see direction 3 |
| 11 | Send updates over the mesh | Partial - `announce_channel` posts status each cycle |
| 12 | Basic commands via DM / channel | Done - messenger, prefix-gated |
| 13 | Logs | Done |
| 14 | Alerts (what is *not* working) | Gap - issues are passive/pull only |

## Direction 1: One onboarding pipeline, not four commands

Onboarding cases 1, 2, 3, and 3a are the **same operation with different identity front-ends**.
Every one must end at the same place:

> a DB record + a working **management channel** (reach-radio + route + auth) + initial facts +
> converged config.

The only part that differs is the identity step at the front:

- **#1 factory-fresh** - generate/accept an identity, then converge.
- **#2 restore** - push `prv.key` from secrets onto fresh hardware to rebuild a known node that
  has gone AWOL/broken in the field, then converge. The dead field node is not contacted; its
  identity is *reused* on the new hardware.
- **#3 / #3a known node** - identity already exists; skip straight to converge.

Today these are separate bespoke flows (`mch new` new mode, `mch new` replace mode, `mch adopt`,
and nothing for #3), which is why #3 (faulkland) falls through the cracks and `adopt` doesn't
actually make a node manageable.

### The real difficulty axis: do we have a route?

Identity is the obvious variable, but the thing that actually decides how hard onboarding is, is
**whether the gateway already has a usable route to the node** - a real `out_path` in its mesh
contacts. Bench-vs-wild is secondary; it only matters when a route is *missing*.

- **Routable (a contact with a stored/direct path):** the easy, already-working case. Directed
  commands route; normal config + sync manages it, bench or wild alike. A node that has been
  meshing in the field is usually here. `mch contacts` already shows what the gateway knows.
  (Stale stored paths still happen - handled by path repair / lossy retry.)
- **Known contact but no usable route (flood/missing) - this is faulkland:** a route must be
  established before management works.
  - *In direct radio range (bench):* hop to its radio and install a **direct (zero-hop) route** via
    discovery. This is the minimal-first work below, and it unblocks the bench side of #1/#2/#3a
    too (their post-commission re-find is the same problem).
  - *Out of direct range (wild):* mesh path discovery has to find a route. Harder; later.
- **Unknown to the gateway and out of direct range (wild + unknown):** no contact, no route, and
  local discovery can't reach it. **We won't try - *remotely*.** This is a deliberate scope
  boundary, not a gap: it keeps the tool simple and honest about what the radio can actually do.
  "Out of scope" means out of *remote* scope - the node isn't permanently unmanageable, it just has
  to be carried into direct range (the bench), at which point it becomes the case above. See
  BACKLOG Won't Do.

Sequencing falls out of this: routable nodes already work; next is establishing a direct route for
known-but-unrouted bench nodes (faulkland); mesh path-discovery for unrouted wild nodes is later;
unknown wild nodes are out of scope.

**Plan:** build the common back-half once - "reach a node I can only see via discovery, on its own
radio, and pull it under management" - then make the identity steps thin front-ends that hand off
to it. This also hardens #1/#2, whose post-reboot re-find hits the same reachability problem.

The hard part the faulkland case exposed: establishing a reliable channel to a node that only
answers broadcast discovery (no route, on a non-home radio). A directed command over flood does not
complete even at strong SNR, while broadcast discovery does. The fix is to **hop to the node's
radio and install a direct (zero-hop) route**, justified by the discovery SNR.

**Minimal-first (next work):** extend `mch adopt` to install a direct route and persist the
reach-radio on the radio where discovery just succeeded - enough to get faulkland managed and
learn from a real node - before generalising into the fuller DB-primary version. faulkland on the
bench is the proving case.

> **Keystone assumption - unproven.** This whole branch rests on: *discovery succeeds → install a
> direct (zero-hop) route → directed commands now work.* We have proven discovery succeeds on
> faulkland; we have **not** proven the direct route fixes the directed-command failure. It is
> well-motivated (a flood-routed directed `ver` fails at strong SNR while a broadcast advert
> succeeds, so a one-hop direct route should be more reliable) but it is a hypothesis. If a direct
> route *also* fails on faulkland, this branch collapses and the open question becomes "why does a
> directed command fail where a broadcast succeeds." So the faulkland bench test is not just the
> first delivery - it is what confirms or kills the model this direction depends on. Also note
> "direct range" is a fuzzy, asymmetric heuristic (we hear their advert ≠ they reliably hear our
> command), not a crisp measurable boundary.

**Onboarding completion check (new):** after a node is adopted/synced, verify access actually
works rather than assuming it: try a wrong password (expect rejection), the right password (expect
success), no password (tests whether the gateway is already in the node's ACL / the node is open),
and the guest password. This is the reliable way to distinguish a genuinely secured node from one
that merely has the gateway in its ACL - the missing piece that made the old "open-node detection"
unreliable. See BACKLOG.

## Direction 2: Outgrow config.yaml - move to DB-primary inventory

We have outgrown the YAML-first model. Today `config.yaml` owns desired state and inventory, and
`secrets.yaml` owns passwords/keys (and is easy to forget). The friction shows up everywhere:
discovered full pubkeys, mesh names, observed radios, and known routes have nowhere natural to live
because the DB is treated as throwaway observed-state.

**Target model:**

- DB owns **inventory and observed/learned state**: discovered full pubkeys, labels, mesh names,
  observed radios, known routes, facts, schedule.
- Config (whatever form) describes **desired policy/defaults** and remains exportable as a
  reviewable backup/import format - never lossy shorthand like literal `...`.
- Add explicit `mch export config` / import / backup commands so the DB is never the *only* copy.
- Secrets/private keys need a separate security decision before they enter any ordinary
  backup/export path. (See the protected-secret-storage backlog item.)

This is the existing DESIGN.md "Future direction" note, now promoted to an active goal. It is a
large change - sequence it after the onboarding pipeline so onboarding tells us what inventory the
DB actually needs to own.

## Direction 3: CLI + API share the model, not the transport

Keep the CLI on the Unix socket (filesystem permissions, no network surface, no tokens, works with
no web server running). Keep the HTTP API as a separate read-only projection for a future web UI.
They already share `report.py` / `models.py` - that is the correct seam. Do **not** collapse the
CLI onto HTTP; that would couple the local control plane's availability and auth to a web server
for no real gain. Powerful *and* simple = one data model, two thin transports.

Later, any *write* surface on the API (beyond read-only) needs its own auth and scoping decision -
it must not become an unauthenticated remote control of the mesh.

## Direction 4: Mesh updates and alerts must respect "don't hammer"

#11 (send updates) and #14 (alerts) collide directly with #7 (don't hammer the mesh). Anything
pushed over LoRa must be rare and high-value:

- Alerts should be **edge-triggered** (a node *became* unreachable, a write *started* failing), not
  periodic status dumps.
- The per-cycle `announce_channel` status post is already borderline; grow it cautiously.
- Prefer pulling status locally (CLI/API) over pushing it across the radio.

## Direction 5: Recommend good derived settings, don't just enforce literals

Some settings are best **derived from observed state** rather than hand-typed per node. The
canonical example is `txdelay` from neighbour count. Making the operator compute and maintain that
by hand across a fleet is exactly the "log into each node" toil mch exists to remove.

**The direction of that relationship is unverified, contested, and the current presets are
probably backwards.** txdelay is not "right of way" - it scales the random backoff window before a
repeater *retransmits a flood* (`delay = random(0, 5*t+1)`, `t = airtime * txdelay`). So txdelay=0
means retransmit immediately with no spreading, and a dense neighbourhood of low-delay repeaters
all rebroadcasting the same flood is exactly where collisions happen.

- The MeshCore community discussion
  (https://github.com/meshcore-dev/MeshCore/discussions/2053) leans toward **density-based
  backoff: more neighbours → *higher* txdelay** (table: 0 → ~0.05, 5 → 1.5, 12 → 3.6, via
  `base + neighbours * 0.3`).
- Our `profiles.yaml` currently does the **opposite** (tower 20+ neighbours → 0.3, mobile 0 → 2.0),
  on a "high-reach node fires first" intuition. By the flood-backoff mechanic that intuition looks
  wrong - it minimises backoff precisely where backoff is most needed.

It is genuinely unresolved even upstream: their own simulator recommended txdelay=0 everywhere,
yet real-world testing showed zero delay made error rates *worse*, and the thread closed asking for
more testing. So mch must **not** hard-code a confident txdelay formula yet - `mch suggest` txdelay
output stays provisional, and we likely need to flip profiles.yaml after a real test. See the
BACKLOG live-test item. Whatever the outcome, the recommendation engine's job is unchanged; only
the formula's direction and slope are in question.

The tool should:

- **Recommend** sound values from observed facts (neighbour count, route length/quality, board
  type, region), surfaced via `mch suggest` / the recommend layer - advisory and paste-ready.
- Optionally **resolve them automatically** (`txdelay: auto`) at sync time using the same formula,
  so the recommendation and the enforced value never drift apart. Keep the formula in one place
  (`recommend.py`) so it is testable and consistent between display and enforcement.
- Show *why* a value was chosen (e.g. "txdelay 1.0 - rooftop, 6 neighbours") so recommendations
  are reviewable, not magic numbers.

Recommendations must degrade gracefully when the input fact is unknown (fall back to a safe
default rather than refusing), consistent with the unreliable-mesh reality above.

## Principles

- Add power by extending one model, not by adding parallel commands.
- The DB should own what it learns; config should own what the operator declares.
- Treat every byte over the mesh as expensive.
- Fail fast and visibly at load time; converge slowly and tolerantly over the air.
