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
    `<redacted-private-key>`.
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
