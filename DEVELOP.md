# Development Notes

This file collects bench and development workflows that are useful while building mch, but too
detailed for the main README.

## Bench Reflash Transcript

For a clean factory-style reset before testing `mch commission`, erase flash first, then write the
merged firmware image.

```bash
esptool --chip esp32s3 --port /dev/cu.usbserial-0001 --baud 460800 erase-flash
esptool --chip esp32s3 --port /dev/cu.usbserial-0001 --baud 460800 write-flash 0x0 /Users/davidmarsh/Downloads/Heltec_v3_repeater-v1.15.0-dee3e26-merged.bin
```

Observed output from a Heltec V3 on macOS:

```text
Fri May 22 09:37:01 dm-mini mch $ esptool --chip esp32s3 --port /dev/cu.usbserial-0001 --baud 460800 erase-flash
esptool v5.2.0
Connected to ESP32-S3 on /dev/cu.usbserial-0001:
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
Features:           Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded Flash 8MB (GD)
Crystal frequency:  40MHz
MAC:                ac:a7:04:00:af:74

Stub flasher running.
Changing baud rate to 460800...
Changed.

Flash memory erased successfully in 2.9 seconds.

Hard resetting via RTS pin...
Fri May 22 09:39:26 dm-mini mch $ esptool --chip esp32s3 --port /dev/cu.usbserial-0001 --baud 460800 write-flash 0x0  /Users/davidmarsh/Downloads/Heltec_v3_repeater-v1.15.0-dee3e26-merged.bin
esptool v5.2.0
Connected to ESP32-S3 on /dev/cu.usbserial-0001:
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
Features:           Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded Flash 8MB (GD)
Crystal frequency:  40MHz
MAC:                ac:a7:04:00:af:74

Stub flasher running.
Changing baud rate to 460800...
Changed.

Configuring flash size...
Flash will be erased from 0x00000000 to 0x0012bfff...
Wrote 1227120 bytes (784693 compressed) at 0x00000000 in 21.9 seconds (447.9 kbit/s).
Hash of data verified.

Hard resetting via RTS pin...
Fri May 22 09:40:02 dm-mini mch $
```
