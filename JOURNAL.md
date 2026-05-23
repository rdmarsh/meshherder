# Project Journal

This journal records project decisions and lessons learned without preserving private bench
details, node identities, generated private keys, local hostnames, or operator passwords.
Use `BACKLOG.md` for active todo items, wishlist items, deferred ideas, and explicit non-goals.

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
