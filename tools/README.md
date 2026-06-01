# MeshCore Test Tools

## `query_node_info.py`

Query a MeshCore repeater using anonymous request types.

By default the tool sends:

- `ANON_REQ_TYPE_OWNER` (`0x02`) for clock, node name, and owner info.
- `ANON_REQ_TYPE_BASIC` (`0x03`) for clock and basic feature flags.

Firmware version is not available through the anonymous request path. The tool reports
`version: null` with that reason.

### Zero-hop example

```bash
./tools/query_node_info.py \
  --serial /dev/ttyUSB0 \
  --radio 915.075,125,9,5 \
  --path-hash-mode 1 \
  --json \
  d4d4d4d461d7a35846714c3d217e4530ddd492e1b62655939529a13cdb5ff291
```

Detailed radio flags override fields from `--radio`. For example, this uses
`915.800,250,11` from `--radio`, but coding rate `8` from `--coding-rate`:

```bash
./tools/query_node_info.py \
  --serial /dev/ttyUSB0 \
  --radio 915.800,250,11,5 \
  --coding-rate 8 \
  d4d4d4d461d7a35846714c3d217e4530ddd492e1b62655939529a13cdb5ff291
```

### Routed return path example

```bash
./tools/query_node_info.py \
  --serial /dev/ttyUSB0 \
  --path 2fd9 \
  --path-hash-mode 1 \
  d4d4d4d461d7a35846714c3d217e4530ddd492e1b62655939529a13cdb5ff291
```

### Include regions

```bash
./tools/query_node_info.py \
  --serial /dev/ttyUSB0 \
  --regions \
  d4d4d4d461d7a35846714c3d217e4530ddd492e1b62655939529a13cdb5ff291
```
