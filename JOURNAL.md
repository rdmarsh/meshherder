# Project Journal

## 2026-05-21 - Commissioning Identity Work

- Live test setup:
  - `luna` runs `mchd` with the USB gateway at `/dev/meshcore0`.
  - A freshly flashed factory repeater is attached to the Mac at `/dev/cu.usbserial-0001`.
  - Direct Mac serial uses `meshcli -r -s /dev/cu.usbserial-0001 -b 115200 ...`.
- Factory node observations:
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
