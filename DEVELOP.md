# Development Notes

This file collects bench and development workflows that are useful while building mch, but too
detailed for the main README.

Use `BACKLOG.md` for active development todos, wishlist items, deferred ideas, and explicit
non-goals. Keep this file limited to repeatable bench and development procedures.

## Bench Reflash Commands

For a clean factory-style reset before testing `mch new`, erase flash first, then write the
merged firmware image. Replace the serial device and firmware path for your bench machine.

```bash
esptool --chip esp32s3 --port /dev/cu.usbserial-XXXX --baud 460800 erase-flash
esptool --chip esp32s3 --port /dev/cu.usbserial-XXXX --baud 460800 write-flash 0x0 /path/to/firmware-merged.bin
```

## Direct MeshCore CLI Checks

For one-off bench checks, prefer documenting the `meshcore-cli` command being run over adding local
helper scripts. Local scripts tend to bake in one developer's serial path, firmware assumptions, and
test frequency.

Use the serial device attached to the bench node:

```bash
meshcore-cli -s /dev/cu.usbserial-XXXX -b 115200 -r ver
meshcore-cli -s /dev/cu.usbserial-XXXX -b 115200 -r board
meshcore-cli -s /dev/cu.usbserial-XXXX -b 115200 -r get radio
meshcore-cli -s /dev/cu.usbserial-XXXX -b 115200 -r get public.key
```

Factory-frequency discovery can be done manually by switching the attached node radio, discovering, then
restoring the test mesh frequency. Be careful: `set radio` changes the attached node's radio.

```bash
meshcore-cli -s /dev/cu.usbserial-XXXX -b 115200 -r set radio 869.618,62.5,8,5
meshcore-cli -s /dev/cu.usbserial-XXXX -b 115200 node_discover 15 sleep 8
meshcore-cli -s /dev/cu.usbserial-XXXX -b 115200 -r set radio 915.800,250,10,5
```

For local tests, use `915.800,250,10,5` as the primary test mesh and `916.575,62.5,7,8` only when a
second test frequency is needed. Do not use the production frequency for bench tests.
