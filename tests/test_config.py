"""Tests for config.py loading and parsing."""

import textwrap
from pathlib import Path

import pytest
import yaml

from mch.config import (
    ApiConfig,
    GroupConfig,
    NodeConfig,
    RadioConfig,
    SettingConfig,
    load,
    overlay_db_group_settings,
    overlay_db_settings,
)
from mch.models import DesiredSetting, Scope, SettingPolicy


def _write_config(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text))
    return p


# ------------------------------------------------------------------
# via_radio: reachability radio
# ------------------------------------------------------------------

def test_node_via_radio_block_form(tmp_path):
    """via_radio: as a YAML mapping is parsed into RadioConfig."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            via_radio:
              freq: 916.575
              bw: 62.5
              sf: 7
              cr: 8
    """)
    cfg = load(p)
    node = cfg.nodes[0]
    assert node.via_radio == RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8)


def test_node_via_radio_string_form(tmp_path):
    """via_radio: as a comma-separated string is parsed into RadioConfig."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            via_radio: "916.575,62.5,7,8"
    """)
    cfg = load(p)
    node = cfg.nodes[0]
    assert node.via_radio == RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8)


def test_group_via_radio_string_form(tmp_path):
    p = _write_config(tmp_path, """\
        groups:
          - name: alternate
            via_radio: "916.575,62.5,7,8"
    """)
    cfg = load(p)
    assert cfg.groups[0].via_radio == RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8)


def test_node_via_radio_absent(tmp_path):
    """A node without via_radio: has via_radio=None."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
    """)
    cfg = load(p)
    assert cfg.nodes[0].via_radio is None


def test_load_records_config_and_secrets_paths(tmp_path):
    """mchd uses these paths to watch YAML files for hot reload."""
    p = _write_config(tmp_path, "gateway: {}\n")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes: {}\n")
    secrets.chmod(0o600)

    cfg = load(p)

    assert cfg.config_path == str(p.resolve())
    assert cfg.secrets_path == str(secrets.resolve())


def test_api_config_is_disabled_and_loopback_by_default():
    cfg = ApiConfig()

    assert cfg.enabled is False
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8743


def test_api_config_rejects_non_loopback_bind():
    with pytest.raises(ValueError, match="loopback"):
        ApiConfig(enabled=True, host="0.0.0.0")


def test_api_block_is_loaded(tmp_path):
    p = _write_config(tmp_path, """\
        api:
          enabled: true
          host: localhost
          port: 9000
    """)

    cfg = load(p)

    assert cfg.api == ApiConfig(enabled=True, host="localhost", port=9000)


def test_tab_indentation_in_config_has_actionable_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("nodes:\n\t- pubkey_prefix: aabbccdd\n")

    with pytest.raises(ValueError, match="replace tabs with spaces"):
        load(path, load_secrets=False)


def test_global_settings_block_parses_like_group(tmp_path):
    p = _write_config(tmp_path, """\
        global:
          default_policy: audit
          via_radio: "916.575,62.5,7,8"
          settings:
            owner.info:
              desired: "rdm"
              policy: enforce
          setting_overrides:
            powersaving: skip
        nodes:
          - pubkey_prefix: aabbccdd
    """)
    cfg = load(p)

    assert cfg.global_settings.default_policy == SettingPolicy.AUDIT
    assert cfg.global_settings.via_radio == RadioConfig(freq=916.575, bw=62.5, sf=7, cr=8)
    assert cfg.global_settings.settings["owner.info"].desired == "rdm"
    assert cfg.global_settings.settings["owner.info"].policy == SettingPolicy.ENFORCE
    assert cfg.global_settings.setting_overrides["powersaving"].policy == SettingPolicy.SKIP


def test_name_prefix_parsed_at_node_group_global(tmp_path):
    p = _write_config(tmp_path, """\
        global:
          name_prefix: "G"
        groups:
          - name: edge
            name_prefix: "\U0001f4e1"
        nodes:
          - pubkey_prefix: aabbccdd
            group: edge
            name_prefix: "\U0001f680"
    """)
    cfg = load(p)
    assert cfg.global_settings.name_prefix == "G"
    assert cfg.groups[0].name_prefix == "\U0001f4e1"
    assert cfg.nodes[0].name_prefix == "\U0001f680"


def test_name_prefix_emoji_within_limit_ok(tmp_path):
    # rocket (4 bytes) + "Repeater01" (10 bytes) = 14 bytes, well under 31.
    p = _write_config(tmp_path, """\
        groups:
          - name: edge
            name_prefix: "\U0001f680"
        nodes:
          - pubkey_prefix: aabbccdd
            group: edge
            settings:
              name: "Repeater01"
    """)
    cfg = load(p)  # must not raise
    assert cfg.nodes[0].settings["name"].desired == "Repeater01"


def test_name_prefix_plus_name_over_byte_limit_rejected(tmp_path):
    # prefix (4B) + 28 ASCII chars = 32 bytes > 31-byte stored cap. Firmware would
    # truncate and corrupt, so reject at load rather than enter an enforcement loop.
    p = _write_config(tmp_path, """\
        groups:
          - name: edge
            name_prefix: "\U0001f680"
        nodes:
          - pubkey_prefix: aabbccdd
            group: edge
            settings:
              name: "AAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    """)
    with pytest.raises(ValueError, match="31-byte firmware limit"):
        load(p)


def test_known_setting_desired_values_are_validated(tmp_path):
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            settings:
              powersaving: maybe
    """)

    with pytest.raises(ValueError, match="powersaving.*on.*off"):
        load(p)


def test_invalid_radio_desired_value_raises(tmp_path):
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            settings:
              radio: "915.075,999,9,5"
    """)

    with pytest.raises(ValueError, match="radio.*bandwidth"):
        load(p)


def test_unknown_setting_desired_values_are_not_registry_validated(tmp_path):
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            settings:
              future.setting: maybe
    """)

    cfg = load(p)

    assert cfg.nodes[0].settings["future.setting"].desired == "maybe"


def test_node_radio_key_raises(tmp_path):
    """radio: on a node raises ValueError with a hint to use via_radio:."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            radio:
              freq: 916.575
              bw: 62.5
              sf: 7
              cr: 8
    """)
    with pytest.raises(ValueError, match="via_radio"):
        load(p)


# ------------------------------------------------------------------
# gateway.radio stays as-is
# ------------------------------------------------------------------

def test_gateway_radio_still_works(tmp_path):
    """gateway.radio: is unaffected by the via rename."""
    p = _write_config(tmp_path, """\
        gateway:
          radio:
            freq: 915.800
            bw: 250
            sf: 10
            cr: 5
    """)
    cfg = load(p)
    assert cfg.gateway.radio == RadioConfig(freq=915.800, bw=250, sf=10, cr=5)


# ------------------------------------------------------------------
# Feature 3: gateway.advert_on_start
# ------------------------------------------------------------------

def test_radio_freq_rounded_to_three_decimal_places(tmp_path):
    """Frequencies read back from nodes with float noise are rounded to 3dp."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            via_radio: "915.7999877,250,10,5"
    """)
    cfg = load(p)
    assert cfg.nodes[0].via_radio.freq == 915.800


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


def test_tab_indentation_in_secrets_has_actionable_error(tmp_path):
    p = _write_config(tmp_path, "gateway: {}\n")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n\taabbccdd: password\n")
    secrets.chmod(0o600)

    with pytest.raises(ValueError, match="replace tabs with spaces"):
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


def test_secrets_identity_warnings_empty_when_prv_key_matches(tmp_path):
    """A configured prefix that matches the stored private key produces no warning."""
    from mch.config import secrets_identity_warnings
    from mch.meshkey import derive_public_key

    prv_key = "10" * 64
    prefix = derive_public_key(prv_key)[:8]
    p = _write_config(tmp_path, f"nodes:\n  - pubkey_prefix: {prefix}\n")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text(f"nodes:\n  {prefix}:\n    prv_key: {prv_key}\n")
    secrets.chmod(0o600)

    cfg = load(p, secrets_path=secrets)

    assert secrets_identity_warnings(cfg) == []


def test_secrets_identity_warnings_reports_mismatched_prv_key(tmp_path):
    """A prv_key that derives a different public key is reported with a suggested prefix."""
    from mch.config import secrets_identity_warnings
    from mch.meshkey import derive_public_key

    prv_key = "10" * 64
    derived_prefix = derive_public_key(prv_key)[:8]
    p = _write_config(tmp_path, "nodes:\n  - pubkey_prefix: aabbccdd\n")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text(f"nodes:\n  aabbccdd:\n    prv_key: {prv_key}\n")
    secrets.chmod(0o600)

    cfg = load(p, secrets_path=secrets)
    warnings = secrets_identity_warnings(cfg)

    assert len(warnings) == 1
    assert "aabbccdd" in warnings[0]
    assert derived_prefix in warnings[0]


def test_secrets_identity_warnings_reports_invalid_prv_key(tmp_path):
    """Invalid prv_key values are warnings instead of config-load failures."""
    from mch.config import secrets_identity_warnings

    p = _write_config(tmp_path, "nodes:\n  - pubkey_prefix: aabbccdd\n")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  aabbccdd:\n    prv_key: deadbeef\n")
    secrets.chmod(0o600)

    cfg = load(p, secrets_path=secrets)
    warnings = secrets_identity_warnings(cfg)

    assert len(warnings) == 1
    assert "invalid" in warnings[0]


def test_secrets_identity_warnings_ignores_orphaned_secret(tmp_path):
    """Secrets may intentionally keep replacement material for nodes not in config."""
    from mch.config import secrets_identity_warnings

    p = _write_config(tmp_path, "nodes: []\n")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  aabbccdd:\n    prv_key: deadbeef\n")
    secrets.chmod(0o600)

    cfg = load(p, secrets_path=secrets)

    assert secrets_identity_warnings(cfg) == []


def test_fact_name_warnings_suggests_known_readable_setting_for_typo(tmp_path):
    from mch.config import fact_name_warnings

    p = _write_config(tmp_path, "facts:\n  - powersavings\n")
    cfg = load(p, load_secrets=False)

    warnings = fact_name_warnings(cfg)

    assert len(warnings) == 1
    assert "powersavings" in warnings[0]
    assert "did you mean 'powersaving'?" in warnings[0]
    assert "Unknown keys remain permitted" in warnings[0]


def test_fact_name_warnings_accepts_registered_readable_settings(tmp_path):
    from mch.config import fact_name_warnings

    p = _write_config(
        tmp_path,
        "facts:\n  - ver\n  - board\n  - clock\n  - lat\n  - lon\n  - neighbors\n  - powersaving\n",
    )
    cfg = load(p, load_secrets=False)

    assert fact_name_warnings(cfg) == []


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


# ------------------------------------------------------------------
# node label validation
# ------------------------------------------------------------------

def test_label_with_space_raises(tmp_path):
    """A node label containing a space is rejected at load time."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            label: "QH Tower"
    """)
    with pytest.raises(ValueError, match="label"):
        load(p)


def test_label_with_emoji_raises(tmp_path):
    """A node label containing emoji is rejected at load time."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            label: "\U0001f4e1 Tower"
    """)
    with pytest.raises(ValueError, match="label"):
        load(p)


def test_label_valid_chars(tmp_path):
    """A label with letters, digits, dashes, underscores, and dots is accepted."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            label: qh-tower.01
    """)
    cfg = load(p)
    assert cfg.nodes[0].label == "qh-tower.01"


def test_label_absent_is_ok(tmp_path):
    """A node without a label field is accepted (label defaults to empty string)."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
    """)
    cfg = load(p)
    assert cfg.nodes[0].label == ""


def test_duplicate_labels_raise(tmp_path):
    """Two nodes with the same label raise ValueError at load time."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            label: site-a
          - pubkey_prefix: ccddeeff
            label: site-a
    """)
    with pytest.raises(ValueError, match="duplicate"):
        load(p)


def test_duplicate_pubkey_prefix_raises(tmp_path):
    """Two config nodes with the same pubkey_prefix raise ValueError at load time."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
          - pubkey_prefix: aabbccdd
    """)
    with pytest.raises(ValueError, match="pubkey_prefix"):
        load(p)


def test_duplicate_secrets_prefix_raises(tmp_path):
    """Duplicate pubkey_prefix keys in secrets.yaml raise ValueError.

    yaml.safe_load silently overwrites duplicates; we need a custom loader to catch this.
    """
    p = _write_config(tmp_path, "gateway: {}")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  aabbccdd: password1\n  aabbccdd: password2\n")
    secrets.chmod(0o600)
    with pytest.raises(ValueError, match="duplicate"):
        load(p, secrets_path=secrets)


# ------------------------------------------------------------------
# pubkey_prefix type coercion
# ------------------------------------------------------------------

def test_pubkey_prefix_numeric_yaml_coerced_to_str(tmp_path):
    """pubkey_prefix that YAML parses as int (all-digit value) is coerced to str."""
    # YAML parses bare integers without quotes as int; startswith() would TypeError
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: 12345678
    """)
    cfg = load(p)
    assert cfg.nodes[0].pubkey_prefix == "12345678"
    assert isinstance(cfg.nodes[0].pubkey_prefix, str)


# ------------------------------------------------------------------
# top-level structure validation
# ------------------------------------------------------------------

def test_empty_config_raises(tmp_path):
    """An empty config file (None from YAML) raises ValueError at load time."""
    p = tmp_path / "config.yaml"
    p.write_text("")
    with pytest.raises(ValueError, match="mapping"):
        load(p)


def test_list_config_raises(tmp_path):
    """A config file that is a YAML list (not mapping) raises ValueError."""
    p = tmp_path / "config.yaml"
    p.write_text("- just a list\n")
    with pytest.raises(ValueError, match="mapping"):
        load(p)


def test_invalid_config_yaml_raises_value_error(tmp_path):
    """Malformed config YAML raises ValueError instead of leaking a PyYAML traceback."""
    p = tmp_path / "config.yaml"
    p.write_text('gateway:\n  name: "unterminated\n  device: /dev/meshcore0\n')
    with pytest.raises(ValueError, match="config file is not valid YAML"):
        load(p)


def test_duplicate_config_keys_raise(tmp_path):
    """Duplicate keys in config.yaml are rejected instead of silently overwriting."""
    p = _write_config(tmp_path, """\
        gateway:
          radio: "915.075,125,9,5"
          radio: "915.800,250,10,5"
    """)
    with pytest.raises(ValueError, match="duplicate YAML key 'radio'"):
        load(p)


def test_duplicate_node_mapping_keys_raise(tmp_path):
    """Duplicate keys inside a node mapping are rejected."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            label: first
            label: second
    """)
    with pytest.raises(ValueError, match="duplicate YAML key 'label'"):
        load(p)


def test_gateway_list_raises_clear_error(tmp_path):
    """gateway: must be a mapping, not an implementation TypeError."""
    p = _write_config(tmp_path, """\
        gateway:
          - /dev/ttyACM0
    """)
    with pytest.raises(ValueError, match="gateway.*mapping"):
        load(p)


def test_nodes_mapping_raises_clear_error(tmp_path):
    """nodes: must be a list."""
    p = _write_config(tmp_path, """\
        nodes:
          pubkey_prefix: aabbccdd
    """)
    with pytest.raises(ValueError, match="nodes.*list"):
        load(p)


def test_unknown_top_level_key_raises(tmp_path):
    """Unknown top-level keys are rejected so typos don't silently do nothing."""
    p = _write_config(tmp_path, """\
        databaes_path: /tmp/mch.db
    """)
    with pytest.raises(ValueError, match="unknown config key"):
        load(p)


def test_unknown_node_key_raises(tmp_path):
    """Unknown node keys are rejected so typoed desired config is visible."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            lable: site-a
    """)
    with pytest.raises(ValueError, match="unknown node key"):
        load(p)


# ------------------------------------------------------------------
# pubkey prefix hex validation
# ------------------------------------------------------------------

def test_pubkey_prefix_odd_length_raises(tmp_path):
    """An odd-length pubkey_prefix (not full bytes) raises ValueError."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbc
    """)
    with pytest.raises(ValueError, match="pubkey_prefix"):
        load(p)


def test_pubkey_prefix_non_hex_raises(tmp_path):
    """A pubkey_prefix containing non-hex characters raises ValueError."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: zz1234
    """)
    with pytest.raises(ValueError, match="pubkey_prefix"):
        load(p)


def test_pubkey_prefix_valid_hex_accepted(tmp_path):
    """A valid even-length hex pubkey_prefix is accepted."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
    """)
    cfg = load(p)
    assert cfg.nodes[0].pubkey_prefix == "aabbccdd"


def test_pubkey_prefix_too_short_raises(tmp_path):
    """A pubkey_prefix shorter than 8 hex characters is ambiguous and rejected."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabb
    """)
    with pytest.raises(ValueError, match="at least 8"):
        load(p)


def test_pubkey_prefix_uppercase_is_normalized(tmp_path):
    """Uppercase config prefixes are normalized for matching against lowercase DB pubkeys."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: AABBCCDD
    """)
    cfg = load(p)
    assert cfg.nodes[0].pubkey_prefix == "aabbccdd"


def test_secrets_prefix_invalid_raises(tmp_path):
    """secrets.yaml prefixes use the same validation as config prefixes."""
    p = _write_config(tmp_path, "gateway: {}\n")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  not-hex: nodepass\n")
    secrets.chmod(0o600)
    with pytest.raises(ValueError, match="secrets.nodes"):
        load(p, secrets_path=secrets)


def test_secrets_prefix_uppercase_is_normalized(tmp_path):
    """Uppercase secrets prefixes are normalized so auth lookup still matches."""
    p = _write_config(tmp_path, "gateway: {}\n")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("nodes:\n  AABBCCDD: nodepass\n")
    secrets.chmod(0o600)
    cfg = load(p, secrets_path=secrets)
    assert "aabbccdd" in cfg.secrets
    assert cfg.secrets["aabbccdd"].password == "nodepass"


# ------------------------------------------------------------------
# group reference validation
# ------------------------------------------------------------------

def test_unknown_group_reference_raises(tmp_path):
    """A node referencing a group not in groups: raises ValueError at load time."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            group: nonexistent-group
        groups:
          - name: real-group
    """)
    with pytest.raises(ValueError, match="group"):
        load(p)


def test_group_operations_are_parsed(tmp_path):
    p = _write_config(tmp_path, """\
        groups:
          - name: default
            operations:
              - command: clock sync
                every_hours: 24
        nodes:
          - pubkey_prefix: aabbccdd
            group: default
    """)

    cfg = load(p)

    assert cfg.groups[0].operations[0].command == "clock sync"
    assert cfg.groups[0].operations[0].every_hours == 24


def test_global_operations_are_parsed(tmp_path):
    p = _write_config(tmp_path, """\
        global:
          operations:
            - command: clock sync
              every_hours: 6
        nodes:
          - pubkey_prefix: aabbccdd
    """)

    cfg = load(p)

    assert cfg.global_settings.operations[0].command == "clock sync"
    assert cfg.global_settings.operations[0].every_hours == 6


def test_example_config_omits_legacy_region_allowf():
    """The shipped example should match v1.15+ region command requirements."""
    example = Path(__file__).resolve().parents[1] / "examples" / "config.example.yaml"
    raw = yaml.safe_load(example.read_text())
    commands = [
        op.get("command", "")
        for node in raw.get("nodes", [])
        for op in node.get("operations", [])
    ]
    assert all(not command.startswith("region allowf ") for command in commands)


def test_example_config_includes_region_hierarchy_examples():
    """The shipped example should show the common AU region naming pattern."""
    example = Path(__file__).resolve().parents[1] / "examples" / "config.example.yaml"
    raw = yaml.safe_load(example.read_text())
    commands = [
        op.get("command", "")
        for node in raw.get("nodes", [])
        for op in node.get("operations", [])
    ]
    assert "region put au" in commands
    assert "region put au-nsw" in commands
    assert "region put au-nsw-syd" in commands
    assert "region put au-vic" in commands


# ------------------------------------------------------------------
# YAML boolean coercion in settings values
# ------------------------------------------------------------------

# All YAML 1.1 boolean synonyms for True and False — PyYAML parses every one
# of these as a Python bool, losing the original spelling. We coerce to "on"/"off"
# to match MeshCore CLI conventions (powersaving on/off, loop.detect off, etc).
@pytest.mark.parametrize("yaml_value", [
    "on", "On", "ON",
    "yes", "Yes", "YES",
    "true", "True", "TRUE",
    # "y"/"Y" are NOT booleans in PyYAML's SafeLoader — they stay as strings
])
def test_setting_yaml_truthy_coerced_to_on(tmp_path, yaml_value):
    """All YAML truthy synonyms must produce desired='on', not 'True'."""
    p = _write_config(tmp_path, f"""\
        nodes:
          - pubkey_prefix: aabbccdd
            settings:
              powersaving: {yaml_value}
        """)
    cfg = load(p)
    assert cfg.nodes[0].settings["powersaving"].desired == "on", (
        f"YAML '{yaml_value}' should coerce to 'on', got "
        f"{cfg.nodes[0].settings['powersaving'].desired!r}"
    )


@pytest.mark.parametrize("yaml_value", [
    "off", "Off", "OFF",
    "no", "No", "NO",
    "false", "False", "FALSE",
    # "n"/"N" are NOT booleans in PyYAML's SafeLoader — they stay as strings
])
def test_setting_yaml_falsy_coerced_to_off(tmp_path, yaml_value):
    """All YAML falsy synonyms must produce desired='off', not 'False'."""
    p = _write_config(tmp_path, f"""\
        nodes:
          - pubkey_prefix: aabbccdd
            settings:
              powersaving: {yaml_value}
        """)
    cfg = load(p)
    assert cfg.nodes[0].settings["powersaving"].desired == "off", (
        f"YAML '{yaml_value}' should coerce to 'off', got "
        f"{cfg.nodes[0].settings['powersaving'].desired!r}"
    )


def test_setting_yaml_integer_coerced_to_string(tmp_path):
    """Integer setting values (e.g. multi.acks: 1) are stored as strings."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            settings:
              multi.acks: 1
        """)
    cfg = load(p)
    assert cfg.nodes[0].settings["multi.acks"].desired == "1"


def test_setting_yaml_float_coerced_to_string(tmp_path):
    """Float setting values (e.g. txdelay: 1.0) are stored as strings."""
    p = _write_config(tmp_path, """\
        nodes:
          - pubkey_prefix: aabbccdd
            settings:
              txdelay: 1.0
        """)
    cfg = load(p)
    assert cfg.nodes[0].settings["txdelay"].desired == "1.0"


# ------------------------------------------------------------------
# overlay_db_settings: DB node-scope desired state overrides config.yaml
# ------------------------------------------------------------------

def _node(prefix="aabbccdd", label="n1", settings=None):
    return NodeConfig(pubkey_prefix=prefix, label=label, settings=settings or {})


def test_overlay_no_db_rows_returns_unchanged():
    node = _node(settings={"tx": SettingConfig(desired="20")})
    merged, warnings = overlay_db_settings(node, [])
    assert merged is node
    assert warnings == []


def test_overlay_adds_db_only_key():
    node = _node(settings={})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "tx", desired="14",
                           policy=SettingPolicy.ENFORCE, poll_hours=12)]
    merged, warnings = overlay_db_settings(node, rows)
    assert merged.settings["tx"].desired == "14"
    assert merged.settings["tx"].policy == SettingPolicy.ENFORCE
    assert merged.settings["tx"].poll_hours == 12
    assert warnings == []


def test_overlay_db_wins_over_yaml_and_warns_on_conflict():
    node = _node(label="solar1", settings={"tx": SettingConfig(desired="20")})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "tx", desired="14")]
    merged, warnings = overlay_db_settings(node, rows)
    assert merged.settings["tx"].desired == "14"   # DB wins
    assert len(warnings) == 1
    assert "tx" in warnings[0]
    assert "'20'" in warnings[0] and "'14'" in warnings[0]
    assert "solar1" in warnings[0]


def test_overlay_warns_when_key_present_in_both_even_if_equal():
    # the YAML line is now dead/ignored regardless of value; operator should remove it
    node = _node(settings={"tx": SettingConfig(desired="14")})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "tx", desired="14")]
    merged, warnings = overlay_db_settings(node, rows)
    assert len(warnings) == 1


def test_overlay_does_not_mutate_original():
    node = _node(settings={"tx": SettingConfig(desired="20")})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "tx", desired="14")]
    overlay_db_settings(node, rows)
    assert node.settings["tx"].desired == "20"  # original untouched


def test_overlay_policy_only_db_row():
    node = _node(settings={})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "tx", desired=None,
                           policy=SettingPolicy.AUDIT)]
    merged, warnings = overlay_db_settings(node, rows)
    assert merged.settings["tx"].desired is None
    assert merged.settings["tx"].policy == SettingPolicy.AUDIT
    assert warnings == []


# ------------------------------------------------------------------
# overlay_db_group_settings: DB group/global-scope desired state overrides config.yaml
# ------------------------------------------------------------------

def _group(name="g1", settings=None, setting_overrides=None):
    return GroupConfig(
        name=name,
        settings=settings or {},
        setting_overrides=setting_overrides or {},
    )


def test_group_overlay_no_db_rows_returns_unchanged():
    group = _group(settings={"tx": SettingConfig(desired="20")})
    merged, warnings = overlay_db_group_settings(group, [])
    assert merged is group
    assert warnings == []


def test_group_overlay_adds_db_only_key():
    group = _group(settings={})
    rows = [DesiredSetting(Scope.GROUP, "g1", "tx", desired="14",
                           policy=SettingPolicy.ENFORCE, poll_hours=12)]
    merged, warnings = overlay_db_group_settings(group, rows)
    assert merged.settings["tx"].desired == "14"
    assert merged.settings["tx"].policy == SettingPolicy.ENFORCE
    assert merged.settings["tx"].poll_hours == 12
    assert warnings == []


def test_group_overlay_db_wins_over_yaml_and_warns_on_conflict():
    group = _group(name="solar", settings={"tx": SettingConfig(desired="20")})
    rows = [DesiredSetting(Scope.GROUP, "solar", "tx", desired="14")]
    merged, warnings = overlay_db_group_settings(group, rows)
    assert merged.settings["tx"].desired == "14"   # DB wins
    assert len(warnings) == 1
    assert "tx" in warnings[0]
    assert "'20'" in warnings[0] and "'14'" in warnings[0]
    assert "solar" in warnings[0]


def test_group_overlay_warns_on_conflict_with_policy_only_override():
    # A YAML setting_override (policy-only) is shadowed once the DB sets the key.
    group = _group(setting_overrides={"tx": SettingConfig(policy=SettingPolicy.SKIP)})
    rows = [DesiredSetting(Scope.GROUP, "g1", "tx", desired="14")]
    merged, warnings = overlay_db_group_settings(group, rows)
    assert merged.settings["tx"].desired == "14"
    assert len(warnings) == 1
    assert "tx" in warnings[0]


def test_group_overlay_does_not_mutate_original():
    group = _group(settings={"tx": SettingConfig(desired="20")})
    rows = [DesiredSetting(Scope.GROUP, "g1", "tx", desired="14")]
    overlay_db_group_settings(group, rows)
    assert group.settings["tx"].desired == "20"  # original untouched


def test_group_overlay_policy_only_db_row():
    group = _group(settings={})
    rows = [DesiredSetting(Scope.GLOBAL, "", "tx", desired=None,
                           policy=SettingPolicy.AUDIT)]
    merged, warnings = overlay_db_group_settings(group, rows)
    assert merged.settings["tx"].desired is None
    assert merged.settings["tx"].policy == SettingPolicy.AUDIT
    assert warnings == []


# ------------------------------------------------------------------
# directive overlay: reserved keys map to NodeConfig/GroupConfig fields, not settings
# ------------------------------------------------------------------

def test_overlay_applies_name_prefix_directive():
    node = _node(settings={})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "name_prefix", desired="\U0001f31e")]
    merged, warnings = overlay_db_settings(node, rows)
    assert merged.name_prefix == "\U0001f31e"
    assert "name_prefix" not in merged.settings  # directive, never a setting row
    assert warnings == []


def test_overlay_name_prefix_directive_warns_on_conflict():
    node = NodeConfig(pubkey_prefix="aabbccdd", label="solar1", name_prefix="OLD")
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "name_prefix", desired="NEW")]
    merged, warnings = overlay_db_settings(node, rows)
    assert merged.name_prefix == "NEW"   # DB wins
    assert len(warnings) == 1
    assert "name_prefix" in warnings[0]
    assert "solar1" in warnings[0]
    assert "'OLD'" in warnings[0] and "'NEW'" in warnings[0]


def test_overlay_name_prefix_empty_clears_prefix():
    # an empty desired coerces to None, so a DB row can clear an inherited prefix
    node = NodeConfig(pubkey_prefix="aabbccdd", name_prefix="OLD")
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "name_prefix", desired="")]
    merged, _ = overlay_db_settings(node, rows)
    assert merged.name_prefix is None


def test_overlay_name_prefix_does_not_mutate_original():
    node = NodeConfig(pubkey_prefix="aabbccdd", name_prefix="OLD")
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "name_prefix", desired="NEW")]
    overlay_db_settings(node, rows)
    assert node.name_prefix == "OLD"


def test_group_overlay_applies_name_prefix_directive():
    group = _group(settings={})
    rows = [DesiredSetting(Scope.GROUP, "g1", "name_prefix", desired="\U0001f6f0")]
    merged, warnings = overlay_db_group_settings(group, rows)
    assert merged.name_prefix == "\U0001f6f0"
    assert "name_prefix" not in merged.settings
    assert warnings == []


def test_overlay_applies_default_policy_directive():
    node = _node(settings={})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "default_policy", desired="enforce")]
    merged, warnings = overlay_db_settings(node, rows)
    assert merged.default_policy == SettingPolicy.ENFORCE
    assert "default_policy" not in merged.settings
    assert warnings == []


def test_group_overlay_applies_default_policy_directive():
    group = _group(settings={})
    rows = [DesiredSetting(Scope.GROUP, "g1", "default_policy", desired="audit")]
    merged, warnings = overlay_db_group_settings(group, rows)
    assert merged.default_policy == SettingPolicy.AUDIT


def test_overlay_invalid_directive_value_is_skipped_with_warning():
    # defensive: a malformed DB directive value (e.g. hand-edited) must not wedge the
    # overlay/sync cycle - skip the override and warn, falling back to YAML/inherit.
    node = _node(settings={})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "default_policy", desired="bogus")]
    merged, warnings = overlay_db_settings(node, rows)
    assert merged.default_policy is None
    assert len(warnings) == 1
    assert "default_policy" in warnings[0] and "bogus" in warnings[0]


def test_overlay_applies_via_radio_directive():
    node = _node(settings={})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "via_radio", desired="915.8,250,11,5")]
    merged, warnings = overlay_db_settings(node, rows)
    assert merged.via_radio == RadioConfig(freq=915.8, bw=250.0, sf=11, cr=5)
    assert "via_radio" not in merged.settings
    assert warnings == []


def test_group_overlay_applies_via_radio_directive():
    group = _group(settings={})
    rows = [DesiredSetting(Scope.GROUP, "g1", "via_radio", desired="915.075,125,9,5")]
    merged, _ = overlay_db_group_settings(group, rows)
    assert merged.via_radio == RadioConfig(freq=915.075, bw=125.0, sf=9, cr=5)


def test_overlay_invalid_via_radio_is_skipped_with_warning():
    node = _node(settings={})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "via_radio", desired="not-a-tuple")]
    merged, warnings = overlay_db_settings(node, rows)
    assert merged.via_radio is None
    assert len(warnings) == 1
    assert "via_radio" in warnings[0]


def test_overlay_name_prefix_drops_leading_space_keeps_trailing():
    # name_prefix is prepended with no separator, so a trailing space is meaningful
    # ("🌞 " + name); a leading space is not, and is dropped.
    node = _node(settings={})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "name_prefix", desired=" \U0001f31e ")]
    merged, _ = overlay_db_settings(node, rows)
    assert merged.name_prefix == "\U0001f31e "


def test_overlay_name_prefix_all_spaces_clears():
    node = NodeConfig(pubkey_prefix="aabbccdd", name_prefix="OLD")
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "name_prefix", desired="   ")]
    merged, _ = overlay_db_settings(node, rows)
    assert merged.name_prefix is None


def test_overlay_default_policy_strips_surrounding_space():
    node = _node(settings={})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "default_policy", desired=" enforce ")]
    merged, _ = overlay_db_settings(node, rows)
    assert merged.default_policy == SettingPolicy.ENFORCE


def test_overlay_via_radio_strips_surrounding_space():
    node = _node(settings={})
    rows = [DesiredSetting(Scope.NODE, "aabbccdd", "via_radio", desired=" 915.8,250,11,5 ")]
    merged, _ = overlay_db_settings(node, rows)
    assert merged.via_radio == RadioConfig(freq=915.8, bw=250.0, sf=11, cr=5)
