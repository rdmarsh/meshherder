# Project Journal

## 2026-05-21 - Commissioning Identity Work

- Live test setup:
  - `luna` runs `mchd` with the USB gateway at `/dev/meshcore0`.
  - A freshly flashed factory repeater is attached to the Mac at `/dev/cu.usbserial-0001`.
  - Direct Mac serial uses `meshcli -r -s /dev/cu.usbserial-0001 -b 115200 ...`.
- Factory node observations:
  - Board: `Heltec V3` (`board` over direct serial).
  - Firmware: `v1.15.0-dee3e26`.
  - Factory public key: `C45A1487AF1D00F4CFAB5B639E2BAC328C34C703F28D4FF65287AC0FBC755811`.
  - Factory radio: `869.6179809,62.5,8,5`.
  - `mchd` commission scan on `luna` found exactly this node as unmanaged with strong RSSI/SNR.
- Deployed fix:
  - Commit `5519498 commission: send fire-and-forget commands without waiting`.
  - `set radio` and `reboot` in commissioning now call `send_command(..., await_response=False)`.
  - Full local checks passed before deploy: `333 passed`, `ruff check .`, `mypy src`.
- Design decision:
  - `mch commission new` should not keep the factory-generated identity.
  - mch should generate a MeshCore-compatible private key, send `set prv.key <key>` during
    commissioning, derive the matching public key, and print both:
    - `config.yaml`: derived `pubkey_prefix`.
    - `secrets.yaml`: generated `prv_key`.
  - This avoids later USB-only `get prv.key` access and makes replacement mode viable.
- Open technical question:
  - Need an implementation for MeshCore-compatible private-key-to-public-key derivation.
  - Standard Ed25519 seed APIs are probably insufficient because MeshCore uses a 64-byte private key
    format and community notes mention nonstandard derivation behavior.
- Future idea:
  - Some operators like vanity public key prefixes such as `DEADBEEF`.
  - Park vanity key generation until basic commissioning/replacement is stable.

## 2026-05-21 - Live Commission Attempt on Heltec V3

- Test command:
  - Commissioned the unmanaged factory node through `mchd` on `luna` using mode `new`.
  - Requested label `mch-test`, group `QH`, name `mch test`, target radio `915.800,250,10,5`,
    and admin password `mchtest`.
- Observed progress:
  - Found the factory node as `c45a1487af1d...`.
  - Login with factory password succeeded.
  - Progress reached `setting generated private key`, `setting password`, then failed at
    `set name mch test` with `timeout - no response from node`.
- Post-failure direct serial checks from the Mac:
  - `get public.key` still returned `C45A1487AF1D00F4CFAB5B639E2BAC328C34C703F28D4FF65287AC0FBC755811`.
  - `get name` still returned `Heltec Repeater`.
  - `get radio` still returned `869.6179809,62.5,8,5`.
  - `get prv.key` returned
    `9872309EC7C267CA3A9CAF379FE7AD758404B48429F95AD5986B9E63010E33783362F035F83DCDCEC715D17452534A56BC93D559CECCA0F09EB366C58E690327`.
  - Deriving a public key from that serial `prv.key` matched the original factory public key.
- Conclusions:
  - `set prv.key` did not take effect on this firmware/board during the mesh commissioning attempt.
  - The current code treated any response text as success; it may have missed an error response from
    `set prv.key` or `password`.
  - The failed `set name` happened immediately after changing the admin password, so password changes
    may invalidate the active admin session on v1.15-era firmware.
- Follow-up code direction:
  - Treat CLI-looking error responses as command failures during commissioning.
  - Redact passwords and private keys from commissioning errors.
  - Move session-invalidating changes (`password`, `set prv.key`) after normal settings.

## 2026-05-21 - Commission Retry with Password/Key Late

- Deployed commit `b83ee4d commission: validate command replies` to `luna`.
- First retry failed at login because the previous attempt had successfully changed the admin password
  from `password` to `mchtest`.
- Reset the test node admin password back to `password` over direct serial so the factory-login path
  could be tested again.
- Second retry completed from the daemon's point of view and printed snippets for generated public key
  `f14257729a72...`.
- Direct serial verification showed only partial success:
  - `get name` returned `mch test`.
  - `get radio` returned `915.7999877,250,10,5`.
  - `get public.key` still returned the original factory key
    `C45A1487AF1D00F4CFAB5B639E2BAC328C34C703F28D4FF65287AC0FBC755811`.
  - `get prv.key` still returned the original factory private key.
- Conclusion:
  - On this Heltec V3 / v1.15.0 build, `set prv.key` over the mesh did not apply even though the
    command path did not report a failure.
  - `commission new` must verify the expected generated public key after reboot/radio change before
    returning success or snippets.
- Follow-up code direction:
  - Add post-commission discovery verification for the generated public key on the target radio.
  - Do not trust `set prv.key` success unless the expected key is actually observed.

## 2026-05-21 - Verification Exposed Radio/Reboot Race

- Deployed commit `c4ac899 commission: verify generated identity` to `luna`.
- Restored the test node radio/name/password over direct serial and rebooted.
- Next commission attempt discovered the previous generated identity `f14257729a72...`, which means the
  earlier `set prv.key` eventually applied after a later explicit reboot.
- The run generated a new expected public key `a8d09bd775cd...`, then failed verification:
  - Error: `commission verification failed: expected new public key a8d09bd775cd..., discovered 2fd9c7bbc93d`.
  - Direct serial still showed stored public key `F14257729A729D2FA9A94DE5754180A9A270ECC0BD91F7F50294A5B0195E8928`.
  - Direct serial still showed stored private key from the previous generated identity.
- Updated conclusion:
  - `set prv.key` can work on this Heltec V3 / v1.15.0 node, but it only becomes visible after a
    reliable reboot.
  - `set radio` on this firmware returns `OK - reboot to apply`; it should not be treated as
    fire-and-forget.
  - Sending no-wait `reboot` immediately after no-wait `set radio` can race, leaving private-key
    changes unapplied until a later reboot.
- Follow-up code direction:
  - Wait for the `set radio` response during commissioning.
  - Keep only the final `reboot` as fire-and-forget.

## 2026-05-21 - Test Frequency Guardrail

- Operator rule:
  - Production frequency is `915.075,125,9,5`; it is OK in public README/examples but not for
    local/live testing.
  - Primary test mesh frequency is `915.800,250,10,5`.
  - Secondary test/hop mesh frequency is `916.575,62.5,7,8`.
  - Factory discovery/configuration frequency remains `869.618,62.5,8,5`.
- Updated unit-test fixtures so test commissioning paths use `915.800,250,10,5`.
- Left `src/mch/data/profiles.yaml` unchanged because it is regional preset data, not a test target.

## 2026-05-21 - Commission Verification Needs Retry

- Restored the attached Heltec V3 to a factory-like baseline over direct serial:
  - Factory public/private key restored.
  - Name restored to `Heltec Repeater`.
  - Admin password restored to `password`.
  - Radio restored to factory `869.618,62.5,8,5`.
- Ran `commission new` through `mchd` on `luna` with target radio `915.800,250,10,5`.
- Daemon progress completed commands but verification failed:
  - Expected generated key prefix `68a85cd82403...`.
  - Verification discovery saw `75ab85174506...`.
- Direct serial immediately after showed the node actually had the expected generated identity:
  - `get public.key` returned `68A85CD82403...`.
  - `get prv.key` returned the matching generated private key.
  - `get name` returned `mch test`.
  - `get radio` returned `915.7999877,250,10,5`.
- Conclusion:
  - Commissioning now applies the generated identity correctly.
  - The verification scan can run before the rebooted node is visible to `luna`, and another node on
    the test mesh can answer first.
- Follow-up code direction:
  - Retry post-commission identity discovery before returning failure.

## 2026-05-22 - Fresh Flash Commission Success

- Flashed `/Users/davidmarsh/Downloads/Heltec_v3_repeater-v1.15.0-dee3e26-merged.bin` to the
  attached Heltec V3 at `/dev/cu.usbserial-0001`.
  - Used `esptool` v5.2.0.
  - Confirmed ESP32-S3, erased flash, wrote merged image at `0x0`, and verified hash.
- Post-flash direct serial baseline:
  - Board: `Heltec V3`.
  - Firmware: `v1.15.0-dee3e26`.
  - Factory public key: `4D36AFEB8C018F186A368CADBC849DE4404EDAB73700BC10C3C494552176DF42`.
  - Factory radio: `869.6179809,62.5,8,5`.
- Ran `commission new` through `mchd` on `luna` with target radio `915.800,250,10,5`.
  - Found factory node `4d36afeb8c01...`.
  - Login succeeded with factory password.
  - Set name, password, generated private key, radio, reboot.
  - Verification passed on attempt 2 of 3.
- Commission result:
  - Generated public key: `487131f1a188c4bb1e3580790a931a9f8ecca84ab4e9f16789b33e09a942ed27`.
  - Generated private key:
    `a07bb5f868cad11b71cd7a7cef6b9245eae0a912a4d2ef763f16dd43b2071d7974a30ee496c18cda75338cc0e680ea63b53e2d5a293b9325dd2765dc8ac416f9`.
  - Config snippet:
    `pubkey_prefix: 487131f1`, label `mch-test`, group `QH`, via `915.800,250,10,5`.
- Direct serial verification after commission:
  - `get public.key` returned `487131F1A188C4BB1E3580790A931A9F8ECCA84AB4E9F16789B33E09A942ED27`.
  - `get prv.key` returned the generated private key above.
  - `get name` returned `mch test`.
  - `get radio` returned `915.7999877,250,10,5`.
- Conclusion:
  - Fresh flash -> `mch commission new` works end-to-end on Heltec V3 / v1.15.0 with the current
    command order and retrying identity verifier.

## 2026-05-22 - Post-Commission Documentation Decisions

- Keep `config.yaml` and `secrets.yaml` operator-managed for now.
  - `mch commission new` should print paste-ready snippets, not auto-edit config files.
  - This keeps private keys/passwords reviewable and commit-friendly while replacement mode is still
    being proven live.
- Next live workflow to test:
  - Add the successful `487131f1` config/secrets snippets manually.
  - Restart mchd.
  - Reflash/reset the Heltec V3 to factory.
  - Run `mch commission` in replacement mode and verify the node returns with public key
    `487131f1...`.
- Factory reset/reflash note:
  - A separate `mch` reset command is not needed yet; documenting the explicit `esptool` commands is
    enough for bench testing.
  - Example for Heltec V3:
    `esptool --chip esp32s3 --port /dev/cu.usbserial-0001 --baud 460800 erase-flash`
  - Then write a merged image at `0x0`, for example:
    `esptool --chip esp32s3 --port /dev/cu.usbserial-0001 --baud 460800 write-flash 0x0 /path/to/Heltec_v3_repeater-v1.15.0-dee3e26-merged.bin`
- CLI wording:
  - Prefer `mch status <node>` for node detail views going forward.
  - Treat `mch node <node>` references as legacy wording to remove or alias later.

## 2026-05-22 - Commission Wizard UX Notes

- Live `mch commission` transcript after successful new-node commissioning showed the flow works,
  but the copy is still rough:
  - The top-level mode menu appears only after discovery, so the operator cannot say "new" or
    "replace" up front.
  - `New node` / `Replace node` wording is understandable but still a little abstract for the
    workflow being tested.
  - The final next step says `mch sync <prefix>`; future docs/UX should prefer `mch status <prefix>`
    for detail views and use sync only when asking mchd to apply desired state.
- Candidate CLI shapes to consider:
  - Keep `mch commission` as the interactive wizard, but clean up text and prompts.
  - Add explicit subcommands: `mch commission new` and `mch commission replace`.
  - Shorter aliases such as `mch new` / `mch replace` are possible, but may be too broad for a tool
    that will grow beyond commissioning.
- Specific copy cleanup ideas:
  - "New node" -> "Commission fresh node".
  - "Replace node" -> "Restore existing identity".
  - Make group prompt sentinel clearer: `Group label [- for none]`.
  - After successful commissioning, say "Add snippets, restart/reload mchd, then run
    `mch status <prefix>` to inspect or `mch sync <prefix>` to force sync."

## 2026-05-22 - Config Reload Direction

- User concern: SQLite can look like a second source of truth when `config.yaml` is edited but the
  running daemon still has old desired state in memory.
- Decision:
  - `config.yaml` and `secrets.yaml` remain the source of truth for desired state.
  - SQLite remains observed/cache/history state: last seen, facts, command history, sync schedule.
  - mchd should detect YAML file changes and reload desired state without an operator restart.
- Implemented reload behaviour:
  - `mchd` records the resolved config and secrets file paths.
  - It polls file metadata every 10 seconds and also reloads on SIGHUP.
  - On a valid reload, it updates the existing in-memory `Config` object, refreshes messenger
    allow-list/channel config, refreshes gateway contacts, and reseeds DB labels/groups from YAML.
  - On invalid YAML or validation failure, it logs the error and keeps the last valid config active.
- Related fix:
  - Relative `secrets_file: secrets.yaml` now resolves relative to the config file directory, not the
    daemon's current working directory.

## 2026-05-22 - Name Display After Sync

- Live output showed `LABEL` updating from config while `NAME` still showed the node's old
  mesh-visible advertised name.
- Clarification:
  - `LABEL` is local-only desired metadata from `config.yaml`.
  - `NAME` is observed MeshCore state from the node/contact store.
  - A config edit alone should not make `NAME` change; a sync must successfully apply
    `settings.name.desired`.
- Bug found:
  - After a successful `set name ...`, mchd kept the old contact advertisement in the DB until a
    later contact refresh.
- Fix:
  - While syncing, update the DB node name from `get name`.
  - After successful `set name`, update the DB node name immediately to the desired value.
- UI wording:
  - Rename CLI display text from `NAME` to `MESH NAME` so it is distinct from the local-only
    `LABEL`.

## 2026-05-22 - Desired vs Known Safety Model

- User concern:
  - SQLite can feel like a second source of truth, but its real job is known/observed state.
  - The tool must not accidentally clobber the mesh.
- Model:
  - YAML is desired state.
  - SQLite is known state: observations, command results, history, schedule.
  - CLI output should make that distinction visible (`LABEL` local desired metadata, `MESH NAME`
    known mesh-visible state, settings table desired vs observed).
- Safety gap found:
  - `resolve_policy()` currently falls back to `ENFORCE`.
  - A safer operator posture is global fallback `AUDIT`, with writes only after explicit
    `policy: enforce` or `default_policy: enforce`.
- Implemented:
  - Changed global fallback to `AUDIT`.
  - A desired setting with no explicit policy now reports drift but does not write.
- Parked next step:
  - Add a plan/dry-run view before broad real-mesh use.
