"""Tests for the current amplifier telemetry snapshot payload."""

from __future__ import annotations

from datetime import datetime, timezone

import msgspec
import pytest
from _helpers import (
    FAKE_DRIVER_NAME,
    make_amplifier_settings,
    make_settings,
    make_tcp_amplifier_settings,
    register_fake_driver,
)
from scietex.hal.current_amplifier.base.capabilities import FilterType
from scietex.hal.current_amplifier.base.data import AmplifierState, FilterSetting
from scietex.service import ValkeyWorkerConfig

from scietex.current_amplifier_service.amplifier_worker import CurrentAmplifierWorker
from scietex.current_amplifier_service.telemetry import (
    CurrentAmplifierServiceTelemetry,
    build_telemetry,
)


@pytest.fixture(autouse=True)
def _fake_driver(monkeypatch):
    """Advertise the shared fake driver as the only entry point in every test."""
    register_fake_driver(monkeypatch)


def _make_worker(tmp_path) -> CurrentAmplifierWorker:
    """Build a worker with a real conf dir and no remote-config channel."""
    return CurrentAmplifierWorker(
        ValkeyWorkerConfig(
            service_name="test",
            conf_dir=str(tmp_path),
            remote_config_enabled=False,
        )
    )


async def _worker_with_boards(tmp_path, labels: list[str]) -> CurrentAmplifierWorker:
    """Build a worker whose registry holds one fake board per label."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(
        make_settings([make_amplifier_settings(label, port=f"/dev/ttyUSB{index}") for index, label in enumerate(labels)])
    )
    return worker


def _sample_state(**overrides: object) -> AmplifierState:
    """A fully populated reading; pass ``field=None`` to blank one out."""
    values: dict[str, object] = {
        "output_voltage_v": 1.0,
        "output_current_a": 2.0e-3,
        "input_current_a": 1.0e-6,
        "boost_gain": 1.0,
        "feedback_resistor_ohm": 1_000_000.0,
        "dc_bias_enabled": False,
        "dc_bias_level": 0.25,
        "bandwidth_hz": 100_000.0,
        "filters": (FilterSetting(type=FilterType.LOW_PASS, enabled=True, high_cutoff_hz=10_000.0),),
        "input_overcurrent": False,
        "output_overload": False,
        "input_compensation_enabled": False,
        "input_compensation_level": 0.5,
        "inverted": False,
    }
    values.update(overrides)
    return AmplifierState(**values)


def _contains_datetime(value: object) -> bool:
    """Whether a decoded msgpack value holds a ``datetime`` anywhere."""
    if isinstance(value, datetime):
        return True
    if isinstance(value, dict):
        return any(_contains_datetime(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_datetime(item) for item in value)
    return False


def test_build_telemetry_unconfigured(tmp_path) -> None:
    """An empty registry reports the unconfigured service state and no devices."""
    worker = _make_worker(tmp_path)

    telemetry = build_telemetry(worker)

    assert telemetry.state == "unconfigured"
    assert telemetry.devices == {}
    assert telemetry.configured is False
    assert telemetry.poll_enabled is False
    assert telemetry.schema_version == 1
    assert telemetry.service == "test"
    assert telemetry.instance_id == worker.instance_id


@pytest.mark.asyncio
async def test_devices_keyed_by_label_with_state_precedence(tmp_path) -> None:
    """The devices map is keyed and sorted by label, with error winning over monitoring."""
    worker = await _worker_with_boards(tmp_path, ["b", "a"])
    reading_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    healthy = worker.stats["b"]
    healthy.last_state = _sample_state(feedback_resistor_ohm=1_000_000.0, boost_gain=3.0)
    healthy.last_reading_at = reading_at
    healthy.reading_count = 7
    worker._monitoring["b"] = True
    worker._connected["b"] = True

    failing = worker.stats["a"]
    failing.consecutive_failures = 2
    failing.error_count = 5
    failing.last_error = "bus timeout"
    worker._connected["a"] = False

    telemetry = build_telemetry(worker)

    assert list(telemetry.devices) == ["a", "b"]
    assert telemetry.state == "running"
    assert telemetry.configured is True
    assert telemetry.poll_enabled is True
    assert datetime.fromisoformat(telemetry.timestamp).tzinfo is not None

    error_device = telemetry.devices["a"]
    assert error_device.state == "error"
    assert error_device.last_error == "bus timeout"
    assert error_device.error_count == 5
    assert error_device.ts is None
    assert error_device.last_ok is None
    assert error_device.preamp_range is None
    assert error_device.feedback_resistor_ohm is None

    monitoring_device = telemetry.devices["b"]
    assert monitoring_device.state == "monitoring"
    assert monitoring_device.driver == FAKE_DRIVER_NAME
    assert monitoring_device.transport == "rs485"
    assert monitoring_device.connected is True
    assert monitoring_device.monitoring is True
    assert monitoring_device.reading_count == 7
    assert monitoring_device.preamp_range == monitoring_device.feedback_resistor_ohm == 1_000_000.0
    assert monitoring_device.ts == reading_at.isoformat()
    assert monitoring_device.last_ok == reading_at.isoformat()
    assert datetime.fromisoformat(monitoring_device.ts) == reading_at
    assert datetime.fromisoformat(monitoring_device.last_ok).tzinfo is not None

    assert len(monitoring_device.filters) == 1
    filter_entry = monitoring_device.filters[0]
    assert filter_entry.type is FilterType.LOW_PASS
    assert filter_entry.enabled is True
    assert filter_entry.low_cutoff_hz is None
    assert filter_entry.high_cutoff_hz == 10_000.0
    assert msgspec.to_builtins(filter_entry) == {
        "type": "low_pass",
        "enabled": True,
        "low_cutoff_hz": None,
        "high_cutoff_hz": 10_000.0,
    }

    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_device_transport_matches_the_list_payload(tmp_path) -> None:
    """Each telemetry entry carries the same transport as ``amplifier:list``."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(
        make_settings(
            [
                make_amplifier_settings("wired", port="/dev/ttyUSB0"),
                make_tcp_amplifier_settings("gatewayed", host="gateway.local"),
            ]
        )
    )

    devices = build_telemetry(worker).devices

    assert devices["wired"].transport == "rs485"
    assert devices["gatewayed"].transport == "tcp"

    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_device_state_order_error_monitoring_ok_idle(tmp_path) -> None:
    """A failing poll beats monitoring, which beats a reading, which beats idle."""
    worker = await _worker_with_boards(tmp_path, ["error", "monitoring", "ok", "idle"])
    state = _sample_state()

    worker.stats["error"].last_state = state
    worker.stats["error"].consecutive_failures = 1
    worker._monitoring["error"] = True
    worker._connected["error"] = True

    worker._monitoring["monitoring"] = True

    worker.stats["ok"].last_state = state
    worker._connected["ok"] = True

    telemetry = build_telemetry(worker)

    assert {label: device.state for label, device in telemetry.devices.items()} == {
        "error": "error",
        "monitoring": "monitoring",
        "ok": "ok",
        "idle": "idle",
    }
    assert telemetry.state == "running"

    # A configured worker with no poll task reports the idle service badge.
    worker._monitoring = {label: False for label in worker.monitoring}
    assert build_telemetry(worker).state == "idle"

    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_msgpack_roundtrip_preserves_the_wire_contract(tmp_path) -> None:
    """The payload decodes back into the API struct and carries no legacy fields."""
    worker = await _worker_with_boards(tmp_path, ["a"])
    reading_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    stats = worker.stats["a"]
    stats.last_state = _sample_state()
    stats.last_reading_at = reading_at
    stats.reading_count = 2
    worker._monitoring["a"] = True
    worker._connected["a"] = True

    telemetry = build_telemetry(worker)
    payload = msgspec.msgpack.encode(telemetry)

    raw = msgspec.msgpack.decode(payload)
    assert "amplifiers" not in raw
    assert isinstance(raw["timestamp"], str)
    assert not _contains_datetime(raw)
    assert raw["state"] == "running"
    assert raw["service"] == "test"
    assert raw["instance_id"] == worker.instance_id
    assert raw["poll_enabled"] is True
    assert raw["schema_version"] == 1
    assert set(raw["devices"]) == {"a"}

    decoded = msgspec.msgpack.decode(payload, type=CurrentAmplifierServiceTelemetry)
    assert decoded.state == telemetry.state
    assert decoded.devices == telemetry.devices
    assert decoded.service == telemetry.service
    assert decoded.instance_id == worker.instance_id
    assert decoded.poll_enabled is True
    assert decoded.timestamp == telemetry.timestamp

    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_transconductance_is_rf_times_gain_when_both_known(tmp_path) -> None:
    """The derived transconductance is ``Rf * boost`` and unknown otherwise."""
    worker = await _worker_with_boards(tmp_path, ["a"])
    stats = worker.stats["a"]

    stats.last_state = _sample_state(feedback_resistor_ohm=1_000_000.0, boost_gain=10.0)
    assert build_telemetry(worker).devices["a"].transconductance_v_per_a == pytest.approx(10_000_000.0)

    stats.last_state = _sample_state(feedback_resistor_ohm=1_000_000.0, boost_gain=None)
    assert build_telemetry(worker).devices["a"].transconductance_v_per_a is None

    stats.last_state = _sample_state(feedback_resistor_ohm=None, boost_gain=10.0)
    assert build_telemetry(worker).devices["a"].transconductance_v_per_a is None

    stats.last_state = _sample_state(feedback_resistor_ohm=None, boost_gain=None)
    assert build_telemetry(worker).devices["a"].transconductance_v_per_a is None

    await worker.close_amplifiers()
