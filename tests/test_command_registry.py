from mch.command_registry import (
    command_timeout,
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


def test_slow_write_timeouts_are_registered():
    assert command_timeout("set radio 915.075,125,9,5") == 180.0
    assert command_timeout("set prv.key deadbeef") == 180.0
    assert command_timeout("set owner.info rdm") == 30.0


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
