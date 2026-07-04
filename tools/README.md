# MeshCore Test Tools

## `refresh_meshcore_networks.py`

Regenerate `src/mch/data/networks.yaml` (the regional radio presets shown by `mch node new`) from
the community [MeshCore Ninja](https://meshcore.ninja) catalogue's published `data.json` (CC0 1.0).
mch never fetches this at runtime — run the tool occasionally and commit the refreshed file:

```bash
python tools/refresh_meshcore_networks.py                 # fetch upstream and rewrite the file
python tools/refresh_meshcore_networks.py --from data.json # regenerate from a local copy
```

## `refresh_meshcore_commands.py`

Regenerate `src/mch/data/repeater_commands.json` (the per-command "first firmware version"
data that powers the advisory `mch node exec` compatibility warning) from the
[MeshCore Ninja](https://meshcore.ninja) cheat sheet's `src/lib/repeaterCommands.js` (MIT — attribute
MeshCore Ninja). Requires Node (the module is imported, not parsed). mch never fetches this at
runtime — run the tool occasionally and commit the refreshed file:

```bash
python tools/refresh_meshcore_commands.py                # fetch upstream and rewrite the file
python tools/refresh_meshcore_commands.py --from cmds.js # regenerate from a local copy
```

## `query_node_info.py`

Query a MeshCore repeater using anonymous request types.

By default the tool sends:

- `ANON_REQ_TYPE_OWNER` (`0x02`) for clock, node name, and owner info.
- `ANON_REQ_TYPE_BASIC` (`0x03`) for clock and basic feature flags.

Firmware version is not available through the anonymous request path. The tool reports
`version: null` with that reason.

When `--radio` or `--frequency` is supplied, the tool reads the attached gateway's current radio,
switches only if needed, and restores the saved radio when the run ends. The restore path also runs
for normal exceptions, Ctrl-C, and SIGTERM. It cannot protect against SIGKILL, power loss, or USB
device failure during the restore command.

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
