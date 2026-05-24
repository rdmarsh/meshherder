"""Curated MeshCore command metadata used by mch.

This is intentionally not a full MeshCore CLI reference. Unknown settings still
fall back to `get <key>` / `set <key> <value>` so operators can use firmware
features before mch has first-class metadata for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CommandKind = Literal["setting", "fact", "operation", "dangerous"]
ComparatorName = Literal["exact", "radio_tuple", "float_0005"]
ValidatorName = Literal[
    "on_off",
    "radio_tuple",
    "latitude",
    "longitude",
    "integer",
    "float",
    "sf",
    "cr",
    "string",
]


@dataclass(frozen=True)
class CommandSpec:
    key: str
    kind: CommandKind
    read: str | None = None
    write: str | None = None
    staged_write: str | None = None
    apply: str | None = None
    writable: bool = False
    value_type: str = "string"
    validator: ValidatorName | None = None
    comparator: ComparatorName = "exact"
    write_timeout_seconds: float | None = None
    reboot_required: bool = False
    min_firmware: str | None = None
    serial_only: bool = False
    dangerous: bool = False
    confirm: bool = False
    fire_and_forget: bool = False
    needs_hardware_test: bool = False
    notes: str = ""

    def read_command(self) -> str | None:
        return self.read

    def write_command(self, value: str) -> str | None:
        return self.write.format(key=self.key, value=value) if self.write else None


DEFAULT_COMMAND_TIMEOUT = 30.0
VALID_RADIO_BANDWIDTHS = (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0)


_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "name",
        "setting",
        read="get name",
        write="set name {value}",
        writable=True,
    ),
    CommandSpec(
        "owner.info",
        "setting",
        read="get owner.info",
        write="set owner.info {value}",
        writable=True,
    ),
    CommandSpec(
        "radio",
        "setting",
        read="get radio",
        write="set radio {value}",
        writable=True,
        value_type="freq,bw,sf,cr",
        validator="radio_tuple",
        comparator="radio_tuple",
        write_timeout_seconds=180.0,
        reboot_required=True,
        staged_write="tempradio {value},5",
        needs_hardware_test=True,
        notes=(
            "Firmware may echo float-rounded frequency and bandwidth values. On v1.15.0 repeater "
            "firmware, `tempradio {value},<minutes>` is temporary/runtime-only, bare `radio` is "
            "unknown, and `set radio {value}` replies `OK - reboot to apply`."
        ),
    ),
    CommandSpec(
        "powersaving",
        "setting",
        read="powersaving",
        write="powersaving {value}",
        writable=True,
        value_type="on|off",
        validator="on_off",
    ),
    CommandSpec(
        "lat",
        "setting",
        read="get lat",
        write="set lat {value}",
        writable=True,
        value_type="latitude",
        validator="latitude",
        comparator="float_0005",
    ),
    CommandSpec(
        "lon",
        "setting",
        read="get lon",
        write="set lon {value}",
        writable=True,
        value_type="longitude",
        validator="longitude",
        comparator="float_0005",
    ),
    CommandSpec(
        "txdelay",
        "setting",
        read="get txdelay",
        write="set txdelay {value}",
        writable=True,
        validator="float",
    ),
    CommandSpec(
        "direct.txdelay",
        "setting",
        read="get direct.txdelay",
        write="set direct.txdelay {value}",
        writable=True,
        validator="float",
    ),
    CommandSpec(
        "flood.advert.interval",
        "setting",
        read="get flood.advert.interval",
        write="set flood.advert.interval {value}",
        writable=True,
        validator="integer",
    ),
    CommandSpec(
        "advert.interval",
        "setting",
        read="get advert.interval",
        write="set advert.interval {value}",
        writable=True,
        validator="integer",
    ),
    CommandSpec(
        "path.hash.mode",
        "setting",
        read="get path.hash.mode",
        write="set path.hash.mode {value}",
        writable=True,
        validator="integer",
    ),
    CommandSpec(
        "loop.detect",
        "setting",
        read="get loop.detect",
        write="set loop.detect {value}",
        writable=True,
    ),
    CommandSpec(
        "multi.acks",
        "setting",
        read="get multi.acks",
        write="set multi.acks {value}",
        writable=True,
        validator="integer",
    ),
    CommandSpec(
        "agc.reset.interval",
        "setting",
        read="get agc.reset.interval",
        write="set agc.reset.interval {value}",
        writable=True,
        validator="integer",
    ),
    CommandSpec("ver", "fact", read="ver"),
    CommandSpec("board", "fact", read="board"),
    CommandSpec("role", "fact", read="get role"),
    CommandSpec("clock", "fact", read="clock"),
    CommandSpec("neighbors", "fact", read="neighbors"),
    CommandSpec("public.key", "fact", read="get public.key"),
    CommandSpec("reboot", "operation", confirm=True, fire_and_forget=True),
    CommandSpec(
        "clkreboot",
        "operation",
        confirm=True,
        fire_and_forget=True,
        needs_hardware_test=True,
        notes=(
            "On Mac-attached v1.15.0 repeater, command drops serial with `Device not configured`; "
            "node returns after about 5 seconds and responds to `ver`."
        ),
    ),
    CommandSpec("clear stats", "operation", confirm=True),
    CommandSpec("log erase", "operation", confirm=True),
    CommandSpec(
        "region allowf",
        "operation",
        needs_hardware_test=True,
        notes=(
            "Older firmware reportedly needed this after adding regions; newer firmware may add "
            "regions enabled by default. On Mac-attached v1.15.0 repeater, `region` returned "
            "`*^ F`; `region list` and `region help` returned `Err - ??`."
        ),
    ),
    CommandSpec("erase", "dangerous", dangerous=True, serial_only=True),
    CommandSpec(
        "set prv.key",
        "dangerous",
        dangerous=True,
        write_timeout_seconds=180.0,
        reboot_required=True,
    ),
    CommandSpec("password", "dangerous", dangerous=True),
    CommandSpec("set guest.password", "dangerous", dangerous=True),
    CommandSpec("start ota", "dangerous", dangerous=True),
    CommandSpec("setperm", "dangerous", dangerous=True),
    CommandSpec("neighbor.remove", "dangerous", dangerous=True),
)

SPECS: dict[str, CommandSpec] = {spec.key: spec for spec in _SPECS}


def get_spec(key: str) -> CommandSpec | None:
    return SPECS.get(key)


def read_command_for_key(key: str) -> str:
    spec = get_spec(key)
    if spec and spec.read_command():
        return spec.read_command() or f"get {key}"
    return f"get {key}"


def write_command_for_key(key: str, value: str) -> str:
    spec = get_spec(key)
    if spec and spec.write_command(value):
        return spec.write_command(value) or f"set {key} {value}"
    return f"set {key} {value}"


def command_timeout(command: str, default: float = DEFAULT_COMMAND_TIMEOUT) -> float:
    spec = matched_command_spec(command)
    return spec.write_timeout_seconds if spec and spec.write_timeout_seconds else default


def matched_command_spec(command: str) -> CommandSpec | None:
    normalized = normalize_command(command)
    for spec in sorted(_SPECS, key=lambda s: len(s.key), reverse=True):
        for candidate in _match_prefixes(spec):
            if normalized == candidate or normalized.startswith(candidate + " "):
                return spec
    return None


def _match_prefixes(spec: CommandSpec) -> tuple[str, ...]:
    prefixes = [normalize_command(spec.key)]
    if spec.write:
        prefixes.append(normalize_command(spec.write.split("{", maxsplit=1)[0]))
    return tuple(prefix for prefix in prefixes if prefix)


def normalize_command(command: str) -> str:
    return " ".join(command.strip().lower().split())


def blocked_command_prefixes() -> frozenset[str]:
    return frozenset(spec.key for spec in _SPECS if spec.dangerous)


def confirm_command_prefixes() -> frozenset[str]:
    return frozenset(spec.key for spec in _SPECS if spec.confirm)


def fire_and_forget_prefixes() -> frozenset[str]:
    return frozenset(spec.key for spec in _SPECS if spec.fire_and_forget)


def validate_value(key: str, value: str) -> str | None:
    spec = get_spec(key)
    if spec is None or spec.validator is None:
        return None
    return validate_by_name(spec.validator, value)


def validate_by_name(validator: ValidatorName, value: str) -> str | None:
    if validator == "string":
        return None
    if validator == "on_off":
        return None if value in ("on", "off") else "must be 'on' or 'off'"
    if validator == "integer":
        try:
            int(value)
        except ValueError:
            return "must be an integer"
        return None
    if validator == "float":
        try:
            float(value)
        except ValueError:
            return "must be a number"
        return None
    if validator == "sf":
        return _validate_int_range(value, 7, 12, "must be an integer 7-12")
    if validator == "cr":
        return _validate_int_range(value, 5, 8, "must be an integer 5-8")
    if validator == "latitude":
        return _validate_float_range(
            value,
            -90.0,
            90.0,
            "must be a latitude between -90 and 90",
        )
    if validator == "longitude":
        return _validate_float_range(
            value, -180.0, 180.0, "must be a longitude between -180 and 180"
        )
    if validator == "radio_tuple":
        return validate_radio_tuple(value)
    return None


def _validate_int_range(value: str, low: int, high: int, message: str) -> str | None:
    try:
        parsed = int(value)
    except ValueError:
        return message
    return None if low <= parsed <= high else message


def _validate_float_range(value: str, low: float, high: float, message: str) -> str | None:
    try:
        parsed = float(value)
    except ValueError:
        return message
    return None if low <= parsed <= high else message


def parse_radio_tuple(raw: str) -> tuple[str, str, str, str] | None:
    parts = [part.strip() for part in raw.split(",")]
    return (parts[0], parts[1], parts[2], parts[3]) if len(parts) == 4 else None


def validate_radio_tuple(raw: str) -> str | None:
    parsed = parse_radio_tuple(raw)
    if parsed is None:
        return "must be freq,bw,sf,cr"
    freq, bw, sf, cr = parsed
    try:
        freq_float = float(freq)
    except ValueError:
        return "radio frequency must be a number"
    if not 400.0 <= freq_float <= 928.0:
        return "radio frequency must be between 400 and 928 MHz"
    try:
        bw_float = float(bw)
    except ValueError:
        return "radio bandwidth must be a number"
    if bw_float not in VALID_RADIO_BANDWIDTHS:
        return (
            "radio bandwidth must be one of "
            "7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125, 250, 500"
        )
    sf_error = validate_by_name("sf", sf)
    if sf_error:
        return f"radio spreading factor {sf_error}"
    cr_error = validate_by_name("cr", cr)
    if cr_error:
        return f"radio coding rate {cr_error}"
    return None
