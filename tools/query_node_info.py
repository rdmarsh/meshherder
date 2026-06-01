#!/usr/bin/env python3
"""Query MeshCore repeater info using anonymous request types.

This is a deliberately small test tool. It sends firmware SEND_ANON_REQ
commands through a locally attached companion and parses the anonymous repeater
responses that current MeshCore firmware exposes:

* OWNER  (0x02): remote clock, node name, owner info
* BASIC  (0x03): remote clock, basic feature flags
* REGIONS (0x01): remote clock, region list

Firmware does not expose an anonymous firmware-version request. Version still
requires the logged-in/guest REQ_TYPE_GET_OWNER_INFO path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from dataclasses import dataclass
from typing import Any

from meshcore import MeshCore
from meshcore.events import EventType

ANON_REQ_TYPE_REGIONS = 0x01
ANON_REQ_TYPE_OWNER = 0x02
ANON_REQ_TYPE_BASIC = 0x03


@dataclass(frozen=True)
class PathSpec:
    path: bytes
    path_hash_mode: int

    @property
    def hop_count(self) -> int:
        hash_size = self.path_hash_mode + 1
        if hash_size <= 0:
            raise ValueError("path_hash_mode must be 0, 1, or 2")
        if len(self.path) % hash_size:
            raise ValueError(
                f"path byte length {len(self.path)} is not divisible by hash size {hash_size}"
            )
        return len(self.path) // hash_size

    def reply_path_payload(self) -> bytes:
        if not 0 <= self.path_hash_mode <= 2:
            raise ValueError("path_hash_mode must be 0, 1, or 2")
        if self.hop_count > 63:
            raise ValueError("path has more than 63 hops")

        # Firmware expects the return path in reverse byte order, matching
        # meshcore_py's send_anon_req implementation.
        path_len_byte = (self.path_hash_mode << 6) | self.hop_count
        return bytes([path_len_byte]) + self.path[::-1]


@dataclass(frozen=True)
class RadioSpec:
    frequency: float
    bandwidth: float
    spreading_factor: int
    coding_rate: int

    def as_tuple(self) -> tuple[float, float, int, int]:
        return (
            self.frequency,
            self.bandwidth,
            self.spreading_factor,
            self.coding_rate,
        )


def parse_path(path_hex: str, path_hash_mode: int) -> PathSpec:
    path_hex = path_hex.replace(",", "").replace(":", "").strip()
    if len(path_hex) % 2:
        raise ValueError("path must contain an even number of hex characters")
    try:
        path = bytes.fromhex(path_hex)
    except ValueError as exc:
        raise ValueError(f"invalid path hex: {path_hex!r}") from exc
    return PathSpec(path=path, path_hash_mode=path_hash_mode)


def parse_radio(value: str) -> RadioSpec:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("radio must be freq,bw,sf,cr, e.g. 915.800,250,11,5")
    try:
        return RadioSpec(
            frequency=float(parts[0]),
            bandwidth=float(parts[1]),
            spreading_factor=int(parts[2]),
            coding_rate=int(parts[3]),
        )
    except ValueError as exc:
        raise ValueError("radio must be freq,bw,sf,cr, e.g. 915.800,250,11,5") from exc


def resolve_radio(args: argparse.Namespace) -> RadioSpec | None:
    radio = parse_radio(args.radio) if args.radio else None
    if args.frequency is None and radio is None:
        return None

    frequency = args.frequency if args.frequency is not None else radio.frequency
    bandwidth = args.bandwidth if args.bandwidth is not None else 125.0
    spreading_factor = args.spreading_factor if args.spreading_factor is not None else 9
    coding_rate = args.coding_rate if args.coding_rate is not None else 5

    if radio is not None:
        bandwidth = args.bandwidth if args.bandwidth is not None else radio.bandwidth
        spreading_factor = (
            args.spreading_factor if args.spreading_factor is not None else radio.spreading_factor
        )
        coding_rate = args.coding_rate if args.coding_rate is not None else radio.coding_rate

    return RadioSpec(
        frequency=frequency,
        bandwidth=bandwidth,
        spreading_factor=spreading_factor,
        coding_rate=coding_rate,
    )


def radios_match(left: tuple[float, float, int, int], right: tuple[float, float, int, int]) -> bool:
    return (
        abs(left[0] - right[0]) < 0.0005
        and abs(left[1] - right[1]) < 0.0005
        and left[2] == right[2]
        and left[3] == right[3]
    )


async def current_radio(mc: MeshCore) -> tuple[float, float, int, int]:
    result = await mc.commands.send_appstart()
    if result.type == EventType.ERROR:
        raise RuntimeError(f"send_appstart failed: {result.payload}")
    payload = result.payload
    return (
        float(payload["radio_freq"]),
        float(payload["radio_bw"]),
        int(payload["radio_sf"]),
        int(payload["radio_cr"]),
    )


def parse_owner_response(data_hex: str) -> dict[str, Any]:
    data = bytes.fromhex(data_hex)
    if len(data) < 4:
        raise ValueError("owner response shorter than remote clock")
    clock = int.from_bytes(data[:4], "little", signed=False)
    text = data[4:].decode("utf-8", "replace").rstrip("\x00")
    parts = text.split("\n", 1)
    return {
        "target_clock": clock,
        "node_name": parts[0] if parts else "",
        "owner_info": parts[1] if len(parts) > 1 else "",
        "text": text,
    }


def parse_basic_response(data_hex: str) -> dict[str, Any]:
    data = bytes.fromhex(data_hex)
    if len(data) < 5:
        raise ValueError("basic response shorter than remote clock + flags")
    clock = int.from_bytes(data[:4], "little", signed=False)
    flags = data[4]
    bridge_kind = flags & 0x03
    bridge = {
        0x00: None,
        0x01: "uart",
        0x03: "espnow",
    }.get(bridge_kind, f"unknown:{bridge_kind}")
    return {
        "target_clock": clock,
        "features_byte": flags,
        "features_hex": f"0x{flags:02x}",
        "bridge": bridge,
        "disabled_forwarding": bool(flags & 0x80),
    }


def parse_regions_response(data_hex: str) -> dict[str, Any]:
    data = bytes.fromhex(data_hex)
    if len(data) < 4:
        raise ValueError("regions response shorter than remote clock")
    clock = int.from_bytes(data[:4], "little", signed=False)
    text = data[4:].decode("utf-8", "replace").rstrip("\x00")
    regions = [line for line in text.splitlines() if line]
    return {
        "target_clock": clock,
        "regions_text": text,
        "regions": regions,
    }


async def connect(args: argparse.Namespace) -> MeshCore:
    if args.tcp_host:
        mc = await MeshCore.create_tcp(
            args.tcp_host,
            args.tcp_port,
            debug=args.debug,
            only_error=not args.debug,
            default_timeout=args.timeout,
        )
    else:
        mc = await MeshCore.create_serial(
            args.serial,
            args.baud,
            debug=args.debug,
            only_error=not args.debug,
            default_timeout=args.timeout,
        )
    if mc is None:
        raise RuntimeError("failed to connect to local MeshCore companion")
    return mc


async def send_anon_request(
    mc: MeshCore,
    target_hex: str,
    request_type: int,
    path: PathSpec,
    timeout: float,
) -> dict[str, Any]:
    target = bytes.fromhex(target_hex)
    if len(target) != 32:
        raise ValueError("target public key must be 32 bytes / 64 hex characters")

    command = (
        b"\x39"
        + target
        + bytes([request_type])
        + path.reply_path_payload()
    )
    sent = await mc.commands.send(command, [EventType.MSG_SENT, EventType.ERROR], timeout=timeout)
    if sent.type == EventType.ERROR:
        return {
            "success": False,
            "error": sent.payload,
            "request_type": request_type,
        }

    tag = sent.payload["expected_ack"].hex()
    suggested_timeout = sent.payload.get("suggested_timeout", int(timeout * 800)) / 800.0
    wait_timeout = max(timeout, suggested_timeout)
    event = await mc.dispatcher.wait_for_event(
        EventType.BINARY_RESPONSE,
        attribute_filters={"tag": tag},
        timeout=wait_timeout,
    )
    if event is None:
        return {
            "success": False,
            "error": "timeout waiting for binary response",
            "tag": tag,
            "request_type": request_type,
            "wait_timeout": wait_timeout,
        }

    return {
        "success": True,
        "tag": tag,
        "request_type": request_type,
        "raw_hex": event.payload["data"],
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    path = parse_path(args.path, args.path_hash_mode)
    radio_spec = resolve_radio(args)
    requests: list[tuple[str, int]] = []
    if args.owner:
        requests.append(("owner", ANON_REQ_TYPE_OWNER))
    if args.basic:
        requests.append(("basic", ANON_REQ_TYPE_BASIC))
    if args.regions:
        requests.append(("regions", ANON_REQ_TYPE_REGIONS))
    if not requests:
        requests = [
            ("owner", ANON_REQ_TYPE_OWNER),
            ("basic", ANON_REQ_TYPE_BASIC),
        ]

    mc = await connect(args)
    saved_radio: tuple[float, float, int, int] | None = None
    restore_needed = False
    primary_exc: BaseException | None = None
    try:
        if radio_spec is not None:
            saved_radio = await current_radio(mc)
            if not radios_match(saved_radio, radio_spec.as_tuple()):
                restore_needed = True
                radio = await mc.commands.set_radio(
                    radio_spec.frequency,
                    radio_spec.bandwidth,
                    radio_spec.spreading_factor,
                    radio_spec.coding_rate,
                )
                if radio.type == EventType.ERROR:
                    raise RuntimeError(f"set_radio failed: {radio.payload}")

        result: dict[str, Any] = {
            "success": True,
            "target": args.target.lower(),
            "route_path": args.path,
            "path_hash_mode": args.path_hash_mode,
            "radio": None if radio_spec is None else {
                "frequency": radio_spec.frequency,
                "bandwidth": radio_spec.bandwidth,
                "spreading_factor": radio_spec.spreading_factor,
                "coding_rate": radio_spec.coding_rate,
            },
            "version": None,
            "version_reason": "anonymous MeshCore repeater requests do not expose firmware version",
            "responses": {},
        }
        for name, request_type in requests:
            response = await send_anon_request(mc, args.target, request_type, path, args.timeout)
            if response["success"]:
                try:
                    if name == "owner":
                        response.update(parse_owner_response(response["raw_hex"]))
                    elif name == "basic":
                        response.update(parse_basic_response(response["raw_hex"]))
                    elif name == "regions":
                        response.update(parse_regions_response(response["raw_hex"]))
                except ValueError as exc:
                    response["parse_error"] = str(exc)
            else:
                result["success"] = False
            result["responses"][name] = response
        return result
    except BaseException as exc:
        primary_exc = exc
        raise
    finally:
        if restore_needed and saved_radio is not None:
            try:
                radio = await mc.commands.set_radio(*saved_radio)
                if radio.type == EventType.ERROR:
                    raise RuntimeError(f"restore radio failed: {radio.payload}")
            except Exception as exc:
                if primary_exc is not None:
                    print(f"error: failed to restore radio: {exc}", file=sys.stderr)
                else:
                    await mc.disconnect()
                    raise
        await mc.disconnect()


async def run_with_signals(args: argparse.Namespace) -> dict[str, Any]:
    task = asyncio.create_task(run(args))
    loop = asyncio.get_running_loop()
    handled_signals: list[signal.Signals] = []

    def cancel_task() -> None:
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, cancel_task)
            handled_signals.append(sig)
        except NotImplementedError:
            pass

    try:
        return await task
    finally:
        for sig in handled_signals:
            loop.remove_signal_handler(sig)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query MeshCore repeaters with anonymous owner/basic/regions requests."
    )
    conn = parser.add_mutually_exclusive_group(required=True)
    conn.add_argument("--serial", help="Serial device for the local companion, e.g. /dev/ttyUSB0")
    conn.add_argument("--tcp-host", help="TCP host for the local companion")
    parser.add_argument("--tcp-port", type=int, default=5000)
    parser.add_argument("--baud", type=int, default=115200)

    parser.add_argument("target", help="Target repeater public key, 64 hex characters")
    parser.add_argument(
        "--path",
        default="",
        help="Return/direct path hex. Empty means zero-hop/direct.",
    )
    parser.add_argument(
        "--path-hash-mode",
        type=int,
        default=0,
        choices=[0, 1, 2],
        help="0=1-byte hashes, 1=2-byte hashes, 2=3-byte hashes",
    )
    parser.add_argument("--timeout", type=float, default=20.0)

    parser.add_argument(
        "--radio",
        help="Temporarily set local radio as freq,bw,sf,cr, e.g. 915.800,250,11,5",
    )
    parser.add_argument(
        "--frequency",
        type=float,
        default=None,
        help="Override radio frequency in MHz",
    )
    parser.add_argument(
        "--bandwidth",
        type=float,
        default=None,
        help="Override radio bandwidth in kHz",
    )
    parser.add_argument("--spreading-factor", type=int, default=None, help="Override radio SF")
    parser.add_argument("--coding-rate", type=int, default=None, help="Override radio coding rate")

    parser.add_argument("--owner", action="store_true", help="Request owner info")
    parser.add_argument("--basic", action="store_true", help="Request basic feature flags")
    parser.add_argument("--regions", action="store_true", help="Request regions")
    parser.add_argument("--json", action="store_true", help="Print compact JSON")
    parser.add_argument("--debug", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = asyncio.run(run_with_signals(args))
    except KeyboardInterrupt:
        if args.json:
            print(json.dumps({"success": False, "error": "interrupted"}, sort_keys=True))
        else:
            print("error: interrupted", file=sys.stderr)
        return 130
    except asyncio.CancelledError:
        if args.json:
            print(json.dumps({"success": False, "error": "cancelled"}, sort_keys=True))
        else:
            print("error: cancelled", file=sys.stderr)
        return 130
    except Exception as exc:
        if args.json:
            print(json.dumps({"success": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
