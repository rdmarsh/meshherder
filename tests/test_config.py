"""Tests for config.py loading and parsing."""

import textwrap
from pathlib import Path

import pytest
import yaml

from mch.config import NodeSecrets, RadioConfig, load


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


# ------------------------------------------------------------------
# Feature 3: gateway.advert_on_start
# ------------------------------------------------------------------

def test_radio_freq_rounded_to_three_decimal_places(tmp_path):
    """Frequencies read back from nodes with float noise are rounded to 3dp."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            via: "915.7999877,250,10,5"
    """)
    cfg = load(p)
    assert cfg.nodes[0].via.freq == 915.800


def test_gateway_advert_on_start_defaults_to_never(tmp_path):
    """gateway.advert_on_start defaults to 'never' when not specified."""
    p = _write_config(tmp_path, "gateway: {}")
    cfg = load(p)
    assert cfg.gateway.advert_on_start == "never"


def test_gateway_advert_on_start_zerohop(tmp_path):
    """gateway.advert_on_start: zerohop is accepted."""
    p = _write_config(tmp_path, """\
        gateway:
          advert_on_start: zerohop
    """)
    cfg = load(p)
    assert cfg.gateway.advert_on_start == "zerohop"


def test_gateway_advert_on_start_invalid_raises(tmp_path):
    """gateway.advert_on_start with an invalid value raises ValueError."""
    p = _write_config(tmp_path, """\
        gateway:
          advert_on_start: always
    """)
    with pytest.raises(ValueError, match="advert_on_start"):
        load(p)


# ------------------------------------------------------------------
# Feature 4: secrets file validation
# ------------------------------------------------------------------

def test_corrupt_secrets_raises_value_error(tmp_path):
    """Corrupt secrets YAML raises ValueError with a clear message."""
    p = _write_config(tmp_path, "gateway: {}")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text(": bad [yaml\n")
    secrets.chmod(0o600)
    with pytest.raises(ValueError, match="not valid YAML"):
        load(p, secrets_path=secrets)


def test_secrets_not_a_mapping_raises_value_error(tmp_path):
    """Secrets file that is a YAML list (not mapping) raises ValueError."""
    p = _write_config(tmp_path, "gateway: {}")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("- just a list\n")
    secrets.chmod(0o600)
    with pytest.raises(ValueError, match="mapping"):
        load(p, secrets_path=secrets)


def test_secrets_nodes_not_a_mapping_raises_value_error(tmp_path):
    """secrets.nodes that is not a mapping raises ValueError."""
    p = _write_config(tmp_path, "gateway: {}")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  - pubkey1\n  - pubkey2\n")
    secrets.chmod(0o600)
    with pytest.raises(ValueError, match="mapping"):
        load(p, secrets_path=secrets)


# ------------------------------------------------------------------
# Fleet-wide default password
# ------------------------------------------------------------------

def test_secrets_default_password_loaded(tmp_path):
    """secrets.nodes.default is loaded as the fleet-wide fallback password."""
    p = _write_config(tmp_path, "gateway: {}")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  default: fleetpassword\n")
    secrets.chmod(0o600)
    cfg = load(p, secrets_path=secrets)
    assert cfg.secrets["default"].password == "fleetpassword"


def test_secrets_default_with_per_node_override(tmp_path):
    """Per-node entry coexists with default."""
    p = _write_config(tmp_path, "gateway: {}")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  aabbccdd: nodepass\n  default: fleetpass\n")
    secrets.chmod(0o600)
    cfg = load(p, secrets_path=secrets)
    assert cfg.secrets["aabbccdd"].password == "nodepass"
    assert cfg.secrets["default"].password == "fleetpass"


def test_secrets_new_format_password(tmp_path):
    """New per-node dict format loads password field."""
    p = _write_config(tmp_path, "gateway: {}")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  aabbccdd:\n    password: nodepass\n")
    secrets.chmod(0o600)
    cfg = load(p, secrets_path=secrets)
    assert cfg.secrets["aabbccdd"].password == "nodepass"
    assert cfg.secrets["aabbccdd"].guest_password is None
    assert cfg.secrets["aabbccdd"].prv_key is None


def test_secrets_new_format_prv_key(tmp_path):
    """prv_key field is loaded from new per-node dict format."""
    p = _write_config(tmp_path, "gateway: {}")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  aabbccdd:\n    password: p\n    prv_key: deadbeef\n")
    secrets.chmod(0o600)
    cfg = load(p, secrets_path=secrets)
    assert cfg.secrets["aabbccdd"].prv_key == "deadbeef"


def test_secrets_new_format_guest_password(tmp_path):
    """guest_password field is loaded from new per-node dict format."""
    p = _write_config(tmp_path, "gateway: {}")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  aabbccdd:\n    password: p\n    guest_password: guest\n")
    secrets.chmod(0o600)
    cfg = load(p, secrets_path=secrets)
    assert cfg.secrets["aabbccdd"].guest_password == "guest"


def test_secrets_old_flat_format_migrated(tmp_path):
    """Old flat format {prefix: password} is automatically migrated to NodeSecrets."""
    p = _write_config(tmp_path, "gateway: {}")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  aabbccdd: oldpass\n  default: fleetpass\n")
    secrets.chmod(0o600)
    cfg = load(p, secrets_path=secrets)
    assert cfg.secrets["aabbccdd"].password == "oldpass"
    assert cfg.secrets["default"].password == "fleetpass"
    assert cfg.secrets["aabbccdd"].prv_key is None
