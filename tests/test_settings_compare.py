"""Tests for field-aware setting comparison."""

from mch.settings_compare import compare_setting


def test_radio_frequency_float_roundoff_is_equal():
    result = compare_setting("radio", "915.075,125,9,5", "915.0750122,125,9,5")
    assert result.equal is True
    assert result.approximate is True


def test_radio_sf_and_cr_must_match_exactly():
    assert compare_setting("radio", "915.075,125,9,5", "915.0750122,125,8,5").equal is False
    assert compare_setting("radio", "915.075,125,9,5", "915.0750122,125,9,6").equal is False


def test_radio_bandwidth_float_roundoff_is_equal():
    result = compare_setting("radio", "915.075,125,9,5", "915.075,124.9999,9,5")
    assert result.equal is True
    assert result.approximate is True


def test_lat_lon_match_to_three_decimal_places():
    assert compare_setting("lat", "-33.732", "-33.7319984").equal is True
    assert compare_setting("lon", "150.903", "150.9029998").equal is True


def test_non_numeric_values_fall_back_to_not_equal():
    assert compare_setting("lat", "-33.732", "??: lat").equal is False
    assert compare_setting("radio", "915.075,125,9,5", "not radio").equal is False


def test_unknown_settings_still_use_exact_string_comparison():
    assert compare_setting("owner.info", "rdm", "rdm").equal is True
    assert compare_setting("owner.info", "rdm", "rdm ").equal is False
