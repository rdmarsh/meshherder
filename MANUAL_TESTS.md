# Luna Manual Test Worksheet

Use this worksheet when live testing can resume. Paste command output and observations below each
test rather than relying on memory. These tests are intended for `luna` and a same-room bench node.

## Safety And Setup

- Do not deliberately test against nodes on `915.075`; that is the field mesh frequency.
- Use a node whose configuration has an explicit `via_radio` on `915.800,...`.
- Do not run the commissioning test unless a disposable/factory-reset bench node is powered on and
  all other unmanaged nodes are powered off.
- Do not paste passwords, private keys, or full `secrets.yaml` contents into this document or the
  project journal.
- Do not run `mch sync` without a node argument during this worksheet: it may schedule field nodes
  on `915.075`.
- Do not use `mch exec` for a live/read command on a `via_radio` bench node until its management
  radio behavior is confirmed; this worksheet uses it only to verify locally blocked commands.

If the build under test has been committed and pushed, update luna before beginning:

```bash
ssh luna
cd /opt/meshherder
sudo git pull --ff-only
sudo /opt/meshherder/.venv/bin/pip install .
sudo systemctl restart mchd
mch --version
git rev-parse --short HEAD
```

Feature prerequisites:

- Test 14 onward requires at least `0.8.5` for management radio/cached path output.
- Test 11 and Test 18 require at least `0.8.6` for fact-name validation.

Record the target before starting:

```text
Date/time:
Installed mch version:
Git commit deployed on luna:
Test node label:
Test node pubkey prefix:
Configured via_radio:
Firmware/version observed:
```

Useful setup commands:

```bash
ssh luna
mch --version
sudo grep -nE 'pubkey_prefix:|label:|via_radio:' /etc/meshherder/config.yaml
systemctl is-active mchd
systemctl status mchd --no-pager
```

Expected:

- The chosen test node has `via_radio: 915.800,...`.
- `mchd` is initially `active`.

Result:

```text

```

## No-Traffic Smoke Tests

These tests use local files, daemon state, or the local SQLite-backed reporting interface. They do
not deliberately contact a remote MeshCore node.

## Test 1: Installed Version, Help, And Service Baseline

Purpose: confirm the installed command and service are the expected build before interpreting any
later result.

```bash
mch --version
mch --help
mch validate --help
mch sync --help
mch logs --help
systemctl is-enabled mchd
systemctl is-active mchd
systemctl status mchd --no-pager
journalctl -u mchd -n 30 --no-pager
```

Expected:

- The version and deployed commit match the build intended for testing.
- Help includes the documented operator commands without a removed `--verbose` sync flag.
- `mchd` is enabled/active and the recent journal contains no startup crash or config-load loop.

Result:

```text

```

## Test 2: Validate The Installed Configuration

Purpose: check the active desired-state files before any sync is run.

```bash
sudo mch validate --config /etc/meshherder/config.yaml
```

Expected:

- Validation succeeds for both config and readable secrets.
- Any warnings are recorded below before continuing; do not treat warnings as harmless without
  deciding whether they are intended.

Result:

```text

```

## Test 3: Validation Rejects Invalid Temporary Configuration

Purpose: verify bad input is reported clearly without altering the live configuration.

```bash
printf 'nodes:\n\t- pubkey_prefix: aabbccdd\n' > /tmp/mch-invalid-tab.yaml
mch validate --config /tmp/mch-invalid-tab.yaml

printf 'databaes_path: /tmp/not-real.db\n' > /tmp/mch-invalid-key.yaml
mch validate --config /tmp/mch-invalid-key.yaml
```

Expected:

- The tab-indented file fails with a message telling the operator to replace tabs with spaces.
- The unknown-key file fails and identifies `databaes_path` as an unknown config key.
- The running daemon and live config are unaffected.

Result:

```text

```

## Test 4: Fleet And Node Status Views

Purpose: confirm the local report view is understandable and node lookup works. `mch status` reads
known daemon/database state; it does not run a node sync.

```bash
mch status
mch status <TEST_LABEL>
mch status <TEST_PREFIX>
mch status no-such-test-node
```

Expected:

- Fleet status includes pubkey, state, local label, mesh name, neighbours, version, and last seen.
- Label and pubkey-prefix lookup select the same bench node detail.
- A nonexistent lookup reports no matching nodes rather than an exception.
- Node detail separates collected facts from desired/observed settings and recent activity.

Result:

```text

```

## Test 5: Plan Is Read-Only

Purpose: check desired-versus-observed reporting without sending mesh traffic or writing a node.

```bash
mch logs <TEST_LABEL> -n 5
mch plan
mch plan <TEST_LABEL>
mch logs <TEST_LABEL> -n 5
```

Expected:

- The plan renders writes, audit drift, and/or unknown observations clearly, or says no planned
  writes.
- Running `mch plan` alone does not create sync boundary entries, setting changes, or command
  failures in activity logs.

Result:

```text

```

## Test 6: Logs And Filters

Purpose: verify activity is usable as an investigation history.

```bash
mch logs -n 10
mch logs <TEST_LABEL> -n 10
mch logs <TEST_PREFIX> -n 10
```

Expected:

- Entries are shown newest at the bottom, with timestamp, relative age, level, and node identity.
- Label and prefix filters identify the same test-node activity.
- Warnings and errors remain distinguishable from informational sync boundary entries.

Result:

```text

```

## Test 7: Suggestions Use Collected Data

Purpose: verify recommendation display from cached neighbour facts. This does not trigger a sync.

```bash
mch suggest
mch suggest <TEST_LABEL>
```

Expected:

- Nodes with collected `neighbors` facts show a recommendation/profile and paste-ready settings.
- A node without a collected neighbour count explains that `neighbors` must be configured and
  collected, rather than guessing.

Result:

```text

```

## Test 8: Contacts Display Is Informational

Purpose: inspect gateway-held unmanaged contacts and its paste-ready output without adopting a
node or changing configuration.

```bash
mch contacts
mch contacts <PARTIAL_NAME_OR_PREFIX>
```

Expected:

- The output is either an explicit no-contacts result or snippets for contacts outside config.
- Filtering by a partial name or prefix narrows the display.
- Running the command does not alter `config.yaml` or add a managed node automatically.

Result:

```text

```

## Test 9: Destructive-Action Guardrails

Purpose: confirm inspection commands do not silently delete state or send irreversible commands.
Do not run `mch prune --yes`, `mch remove --yes`, or `mch remove --force` in this test.

```bash
mch prune
mch remove <TEST_LABEL> --yes
mch exec <TEST_LABEL> erase
mch exec <TEST_LABEL> set prv.key 00000000
```

Expected:

- `mch prune` lists candidates only, or reports none; it does not delete without `--yes`.
- `mch remove <TEST_LABEL> --yes` refuses because the node is still configured and `--force`
  was not supplied.
- Both `mch exec` attempts are blocked locally as unsafe and do not reach the node.

Result:

```text

```

## Test 10: Optional Read-Only HTTP API

Purpose: verify the optional API exposes the same read-only report model as the CLI. Skip this
test if `api.enabled` is false in the active configuration.

```bash
curl -fsS http://127.0.0.1:8743/health
curl -fsS http://127.0.0.1:8743/status
curl -fsS http://127.0.0.1:8743/status/<TEST_LABEL>
curl -fsS http://127.0.0.1:8743/plan
curl -fsS 'http://127.0.0.1:8743/logs?node=<TEST_LABEL>&limit=10'
curl -i http://127.0.0.1:8743/not-a-route
```

Expected:

- `/health` responds successfully; status/plan/logs return JSON local report data.
- Node status corresponds to `mch status <TEST_LABEL>`.
- An unknown path returns `404` rather than exposing an unexpected action.
- No API request initiates sync, exec, or commissioning work.

Result:

```text

```

## Test 11: Configuration Validation Catches A Fact Typo

Purpose: verify a typo such as `powersavings` is identified before it creates another stale
observed fact. This test sends no mesh traffic.

Use a temporary config rather than editing the live configuration:

```bash
printf '\nfacts:\n  - powersavings\n' > /tmp/mch-fact-typo.yaml
mch validate --config /tmp/mch-fact-typo.yaml
```

If testing the real configuration after editing its existing `facts:` list, do not add a second
`facts:` YAML block; run:

```bash
sudo mch validate --config /etc/meshherder/config.yaml
```

Expected once the validation change is installed:

```text
warning: facts contains unrecognised key 'powersavings'; did you mean 'powersaving'? ...
```

Result:

```text

```

## Test 12: Optional Config Hot Reload Of Local Metadata

Purpose: confirm a valid config change is reloaded without a service restart, using only a local
label change. This does not deliberately send mesh traffic; run it while no scheduled/manual sync
is in progress.

Preparation:

- Choose a configured `915.800` bench node and set `<TEMP_LABEL>` to a temporary valid label.
- Change only its local `label:`; do not change `via_radio`, settings, facts, or secrets.

```bash
sudo cp -p /etc/meshherder/config.yaml /tmp/mch-config.before-reload.yaml
sudoedit /etc/meshherder/config.yaml
sudo mch validate --config /etc/meshherder/config.yaml
sudo systemctl kill -s HUP mchd
sleep 2
mch status <TEMP_LABEL>
journalctl -u mchd --since '-5 minutes' --no-pager

sudo cp -p /tmp/mch-config.before-reload.yaml /etc/meshherder/config.yaml
sudo mch validate --config /etc/meshherder/config.yaml
sudo systemctl kill -s HUP mchd
sleep 2
mch status <TEST_LABEL>
```

Expected:

- The changed label appears after reload and the journal reports a successful config reload.
- After restoration, the original label returns.
- No setting write, fact read, or manual sync activity is generated solely by this label test.

Result:

```text

```

## Service And Bench-Traffic Tests

The tests below can affect daemon availability or deliberately contact a node. Use only a
`915.800,...` bench node unless a step explicitly says it uses no node traffic.

## Test 13: Stop And Restart Lifecycle

Purpose: confirm a deliberate stop completes cleanly and CLI commands do not pretend the daemon
is still available.

```bash
mch stop
systemctl is-active mchd
systemctl status mchd --no-pager
pgrep -af '[m]chd'
mch status
mch start
systemctl is-active mchd
mch status
```

Expected:

- `mch stop` prints `stopping current sync, this may take some time...` then `mchd stopped`.
- After stopping, `systemctl is-active mchd` reports `inactive` and `mch status` fails because
  there is no daemon socket/service available. `pgrep -af '[m]chd'` prints no daemon process.
- After `mch start`, `systemctl is-active mchd` reports `active` and `mch status` works again.

Result:

```text

```

## Test 14: Foreground Sync Shows Management Radio And Cached Path

Purpose: verify the new diagnostic context and manual-sync activity markers on a bench node.
Replace `<TEST_LABEL>` with a node explicitly configured for `915.800`.

```bash
mch sync <TEST_LABEL>
mch logs <TEST_LABEL> -n 30
journalctl -u mchd --since '-10 minutes' --no-pager
```

Expected once `0.8.5` is installed:

- Progress includes `management radio: 915.800/...` and says either
  `via_radio temporary switch` or `gateway current radio; no switch`.
- Progress includes `cached management path: direct`, `flood`, or `stored (...)`.
- If the gateway must move to the test radio, the journal shows a switch to `915.800`, followed
  by restoration after sync. Do not proceed if it switches to `915.075` for a test node.
- `mch logs` contains `manual sync started` and a terminal completion, partial-result, failure,
  or abort marker.

Result:

```text

```

## Test 15: Repeated Sync Reliability Evidence

Purpose: capture whether failures are node-wide, command-specific, intermittent, or biased toward
the first command. This is deliberate bench traffic on `915.800`.

Run three separate foreground syncs, leaving the output intact:

```bash
mch sync <TEST_LABEL>
mch sync <TEST_LABEL>
mch sync <TEST_LABEL>
mch logs <TEST_LABEL> -n 100
```

Record for each run:

```text
Run 1:
  started:
  radio/path shown:
  commands that timed out:
  commands that succeeded:
  final result:

Run 2:
  started:
  radio/path shown:
  commands that timed out:
  commands that succeeded:
  final result:

Run 3:
  started:
  radio/path shown:
  commands that timed out:
  commands that succeeded:
  final result:

Pattern observed (all failed / intermittent / first-command only / key-specific / other):
```

## Test 16: Interrupted Foreground Sync Is Logged

Purpose: confirm cancellation is visible and the gateway returns from any temporary test-radio
switch.

```bash
mch sync <TEST_LABEL>
# Press Ctrl-C while a read is waiting.
mch logs <TEST_LABEL> -n 30
journalctl -u mchd --since '-10 minutes' --no-pager
```

Expected:

- The CLI returns with `Aborted!`.
- Activity includes `manual sync started`, `sync interrupted before completion`, and
  `manual sync aborted`.
- If a temporary switch was made to `915.800`, the system journal shows the gateway restored
  afterward.

Result:

```text

```

## Test 17: Stop During A Slow Foreground Sync

Purpose: verify daemon shutdown waits for/cancels in-progress work and restores the gateway after
a temporary move to the test frequency.

In terminal A:

```bash
mch sync <TEST_LABEL>
```

While it is waiting for a slow reply, in terminal B:

```bash
mch stop
systemctl is-active mchd
journalctl -u mchd --since '-10 minutes' --no-pager
mch start
```

Expected:

- `mch stop` warns that stopping current sync may take time, then completes.
- `systemctl is-active mchd` reports `inactive` before it is restarted.
- The journal shows cancellation/cleanup and restoration from `915.800` if a temporary switch was
  in effect; it must not leave the gateway on the bench channel unintentionally.

Result:

```text

```

## Test 18: Reproduce Stale Cached Fact From A Typo

Purpose: document the current cleanup gap tracked in `BACKLOG.md`. Only do this on the bench test
node because it deliberately sends one invalid fact read.

1. Add `powersavings` temporarily to the live `facts:` list, validate, and restart so the test has
   a clear loaded-config boundary.
2. Sync only the `915.800` test node.
3. Remove `powersavings` again, leaving valid facts such as `powersaving` intact.
4. Inspect status after the valid config is active.

```bash
sudoedit /etc/meshherder/config.yaml
sudo mch validate --config /etc/meshherder/config.yaml
mch restart
mch sync <TEST_LABEL>
mch status <TEST_LABEL>
sudoedit /etc/meshherder/config.yaml
sudo mch validate --config /etc/meshherder/config.yaml
mch restart
mch status <TEST_LABEL>
```

Expected with current behavior:

- Validation warns that `powersavings` is unrecognised.
- The sync records the failed read from the deliberately misspelled key.
- After removing it from config, the cached `powersavings: ??: powersavings` observation may
  continue to appear in status. The planned cleanup command is not implemented yet.

Result:

```text

```

## Test 19: Optional New-Node Commissioning On The Test Mesh

Purpose: verify a factory-reset bench node can be commissioned onto the test mesh. This changes
node identity, password, radio, and config/secrets state; skip it unless that is intentional.

Preparation:

- Power off any other unmanaged bench nodes.
- Erase/reflash the disposable target if a factory-new starting state is required.
- Keep the target radio choice on `915.800,...`, never `915.075`.

```bash
mch new
# Select new-node mode.
# Choose or paste target radio: 915.800,250,10,5
# After success, add the printed node and secret snippets to the appropriate config files.
sudoedit /etc/meshherder/config.yaml
sudoedit /etc/meshherder/secrets.yaml
sudo mch validate --config /etc/meshherder/config.yaml
mch restart
mch status <NEW_PREFIX>
mch sync <NEW_PREFIX>
mch logs <NEW_PREFIX> -n 30
```

Expected:

- Commissioning discovers only one unmanaged node on factory frequency.
- It completes with a new managed public-key prefix and emits config/secrets snippets.
- The configured node uses `via_radio: 915.800,...`.
- The follow-up sync shows management radio/path context and activity markers.

Result:

```text

```

## Findings To Move Into The Project

After completing tests, copy durable outcomes into `JOURNAL.md` and move actionable gaps into
`BACKLOG.md`. Include:

```text
Version/commit tested:
Hardware and firmware:
Which tests passed:
Which tests failed:
Observed timeout/retry/path/radio behavior:
Any new backlog item:
Any confirmed design decision:
```
