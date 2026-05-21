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
