"""Field-aware desired-vs-observed setting comparison."""

from __future__ import annotations

from dataclasses import dataclass

from mch.command_registry import get_spec


@dataclass(frozen=True)
class SettingComparison:
    equal: bool
    approximate: bool = False


def compare_setting(key: str, desired: str | None, observed: str | None) -> SettingComparison:
    """Compare a setting using firmware-aware tolerances where needed."""
    if desired is None or observed is None:
        return SettingComparison(desired == observed)

    if desired == observed:
        return SettingComparison(True)

    spec = get_spec(key)
    comparator = spec.comparator if spec else "exact"

    if comparator == "radio_tuple":
        return _compare_radio(desired, observed)
    if comparator == "float_0005":
        return _compare_float(desired, observed, tolerance=0.0005)

    return SettingComparison(False)


def _compare_float(desired: str, observed: str, *, tolerance: float) -> SettingComparison:
    try:
        desired_f = float(desired)
        observed_f = float(observed)
    except ValueError:
        return SettingComparison(False)
    equal = abs(desired_f - observed_f) <= tolerance
    return SettingComparison(equal, approximate=equal)


def _compare_radio(desired: str, observed: str) -> SettingComparison:
    desired_parts = _parse_radio(desired)
    observed_parts = _parse_radio(observed)
    if desired_parts is None or observed_parts is None:
        return SettingComparison(False)

    desired_freq, desired_bw, desired_sf, desired_cr = desired_parts
    observed_freq, observed_bw, observed_sf, observed_cr = observed_parts

    equal = (
        abs(desired_freq - observed_freq) <= 0.001
        and abs(desired_bw - observed_bw) <= 0.001
        and desired_sf == observed_sf
        and desired_cr == observed_cr
    )
    return SettingComparison(equal, approximate=equal)


def _parse_radio(raw: str) -> tuple[float, float, int, int] | None:
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 4:
        return None
    try:
        return float(parts[0]), float(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None
