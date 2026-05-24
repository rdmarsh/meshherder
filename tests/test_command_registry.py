from mch.command_registry import (
    command_timeout,
    get_spec,
    read_command_for_key,
    validate_value,
    write_command_for_key,
)
from mch.command_safety import (
    blocked_exec_command,
    confirm_exec_command,
    fire_and_forget_command,
)


def test_unknown_settings_fall_back_to_standard_get_set_commands():
    assert read_command_for_key("new.firmware.key") == "get new.firmware.key"
    assert write_command_for_key("new.firmware.key", "value") == "set new.firmware.key value"
    assert validate_value("new.firmware.key", "anything") is None


def test_nonstandard_powersaving_command_shape_is_registered():
    assert read_command_for_key("powersaving") == "powersaving"
    assert write_command_for_key("powersaving", "on") == "powersaving on"


def test_fact_command_shapes_are_registered():
    assert read_command_for_key("clock") == "clock"
    assert read_command_for_key("neighbors") == "neighbors"
    assert read_command_for_key("role") == "get role"


def test_clock_sync_behavior_is_recorded():
    spec = get_spec("clock sync")

    assert spec is not None
    assert spec.kind == "operation"
    assert "OK - clock set" in spec.notes


def test_slow_write_timeouts_are_registered():
    assert command_timeout("set radio 915.075,125,9,5") == 180.0
    assert command_timeout("set prv.key deadbeef") == 180.0
    assert command_timeout("set owner.info rdm") == 30.0


def test_radio_records_unverified_staged_write_path():
    spec = get_spec("radio")

    assert spec is not None
    assert spec.staged_write == "tempradio {value},5"
    assert spec.apply is None
    assert spec.needs_hardware_test is True
    assert "temporary/runtime-only" in spec.notes
    assert "reboot to apply" in spec.notes


def test_reboot_like_commands_and_region_allowf_are_marked_for_hardware_discovery():
    reboot = get_spec("reboot")
    clkreboot = get_spec("clkreboot")
    region_allowf = get_spec("region allowf")

    assert reboot is not None
    assert reboot.fire_and_forget is True
    assert reboot.needs_hardware_test is True
    assert "Device not configured" in reboot.notes
    assert "wrong 2024 baseline" in reboot.notes
    assert "Heltec V3" in reboot.notes
    assert "preserved the synced 2026 clock" in reboot.notes
    assert clkreboot is not None
    assert clkreboot.fire_and_forget is True
    assert clkreboot.needs_hardware_test is True
    assert "Device not configured" in clkreboot.notes
    assert "clock sync" in clkreboot.notes
    assert "wrong 2024 baseline" in clkreboot.notes
    assert "reset-clock-and-reboot" in clkreboot.notes
    assert "ESP32/V4" in clkreboot.notes
    assert "Heltec V3" in clkreboot.notes
    assert region_allowf is not None
    assert region_allowf.needs_hardware_test is True
    assert "Err - ??" in region_allowf.notes


def test_value_validation_for_known_keys():
    assert validate_value("powersaving", "on") is None
    assert validate_value("powersaving", "yes") == "must be 'on' or 'off'"
    assert validate_value("lat", "-33.732") is None
    assert "latitude" in (validate_value("lat", "-91") or "")
    assert validate_value("lon", "150.903") is None
    assert "longitude" in (validate_value("lon", "181") or "")
    assert validate_value("radio", "915.075,125,9,5") is None
    assert "spreading factor" in (validate_value("radio", "915.075,125,13,5") or "")
    assert "coding rate" in (validate_value("radio", "915.075,125,9,9") or "")
    assert "bandwidth" in (validate_value("radio", "915.075,999,9,5") or "")


def test_safety_classification_is_registry_backed():
    assert blocked_exec_command("set prv.key deadbeef") == "set prv.key"
    assert blocked_exec_command("set prv.keyring deadbeef") is None
    assert confirm_exec_command("reboot") == "reboot"
    assert fire_and_forget_command("clkreboot") == "clkreboot"
