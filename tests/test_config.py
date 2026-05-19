"""Tests for config.py loading and parsing."""

import textwrap
from pathlib import Path

import pytest

from mch.config import RadioConfig, load


def _write_config(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text))
    return p


# ------------------------------------------------------------------
# via: block form
# ------------------------------------------------------------------

def test_node_via_block_form(tmp_path):
    """via: as a YAML mapping is parsed into RadioConfig."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            via:
              freq: 916.575
              bw: 62.5
              sf: 7
              cr: 8
    """)
    cfg = load(p)
    node = cfg.nodes[0]
    assert node.via == RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8)


def test_node_via_string_form(tmp_path):
    """via: as a comma-separated string is parsed into RadioConfig."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            via: "916.575,62.5,7,8"
    """)
    cfg = load(p)
    node = cfg.nodes[0]
    assert node.via == RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8)


def test_node_via_absent(tmp_path):
    """A node without via: has via=None."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
    """)
    cfg = load(p)
    assert cfg.nodes[0].via is None


def test_node_radio_key_ignored(tmp_path):
    """Old radio: key on a node is silently ignored (does not populate via)."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            radio:
              freq: 916.575
              bw: 62.5
              sf: 7
              cr: 8
    """)
    cfg = load(p)
    assert cfg.nodes[0].via is None


# ------------------------------------------------------------------
# gateway.radio stays as-is
# ------------------------------------------------------------------

def test_gateway_radio_still_works(tmp_path):
    """gateway.radio: is unaffected by the via rename."""
    p = _write_config(tmp_path, """\
        gateway:
          radio:
            freq: 915.075
            bw: 125
            sf: 9
            cr: 5
    """)
    cfg = load(p)
    assert cfg.gateway.radio == RadioConfig(freq=915.075, bw=125, sf=9, cr=5)
