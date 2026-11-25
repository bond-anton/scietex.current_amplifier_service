"""Tests for the current amplifier worker registry lifecycle and poll loop."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime

import msgspec
import pytest
from _helpers import (
    _FakeAmplifier,
    make_amplifier_settings,
    make_settings,
    register_fake_driver,
)
from scietex.hal.current_amplifier.base.capabilities import FilterType
from scietex.hal.current_amplifier.base.data import AmplifierState, FilterSetting
from scietex.service import ValkeyWorker, ValkeyWorkerConfig

from scietex.current_amplifier_service.amplifier_worker import CurrentAmplifierWorker
from scietex.current_amplifier_service.config import (
    CURRENT_AMPLIFIER_CONFIG_FILE,
    CURRENT_AMPLIFIER_CONFIG_SUBDIR,
    AmplifierSerialSettings,
    CurrentAmplifierServiceSettings,
)


class _NoSettleAmplifier(_FakeAmplifier):
    """A driver whose constructor declares no ``relay_settle_s``.

    Mirrors a driver family that owns its settle delay internally; the worker
    must not pass the configured keyword to it.
    """

    def __init__(self, con_params=None, address: int = 2, label: str = "EBIC", logger=None) -> None:
        super().__init__(con_params=con_params, address=address, label=label, logger=logger)


class _FakeClient:
    """A glide-client stand-in recording ``xadd`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[tuple[bytes, bytes]], object]] = []

    async def xadd(self, key: str, values: list[tuple[bytes, bytes]], options=None) -> None:
        self.calls.append((key, values, options))


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


def _write_config(tmp_path, settings: CurrentAmplifierServiceSettings) -> None:
    """Write the service-owned ``current_amplifier.yml`` at the namespaced path."""
    path = tmp_path / CURRENT_AMPLIFIER_CONFIG_SUBDIR / CURRENT_AMPLIFIER_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(msgspec.yaml.encode(settings))


async def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Yield until ``predicate`` holds, failing the test instead of hanging."""

    async def _spin() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_spin(), timeout)


@pytest.mark.asyncio
async def test_build_amplifiers_instantiates_one_driver_per_board(tmp_path) -> None:
    """Each configured board gets its own driver, built from its own settings."""
    worker = _make_worker(tmp_path)
    settings = make_settings(
        [
            make_amplifier_settings(
                "a",
                address=2,
                serial=AmplifierSerialSettings(
                    port="/dev/ttyUSB0",
                    baudrate=38400,
                    bytesize=7,
                    parity="E",
                    stopbits=2,
                    timeout=0.5,
                    framer="ASCII",
                ),
            ),
            make_amplifier_settings("b", port="/dev/ttyUSB1", address=7),
        ]
    )

    await worker._build_amplifiers(settings)

    assert set(worker.amplifiers) == {"a", "b"}
    first = worker.amplifiers["a"]
    second = worker.amplifiers["b"]
    assert isinstance(first, _FakeAmplifier)
    assert first is not second

    assert first.label == "a"
    assert first.address == 2
    assert first.logger is worker.logger

    con_params = first.con_params
    assert con_params.port == "/dev/ttyUSB0"
    assert con_params.baudrate == 38400
    assert con_params.bytesize == 7
    assert con_params.parity == "E"
    assert con_params.stopbits == 2
    assert isinstance(con_params.stopbits, int)
    assert con_params.timeout == 0.5
    assert con_params.framer == "ASCII"

    assert second.label == "b"
    assert second.address == 7
    assert second.logger is worker.logger
    assert second.con_params.port == "/dev/ttyUSB1"
    assert second.con_params.baudrate == 19200
    assert second.con_params.parity == "N"
    assert second.con_params.framer == "RTU"

    await worker.close_amplifiers()
    assert first.close_calls == 1
    assert second.close_calls == 1


@pytest.mark.asyncio
async def test_relay_settle_forwarded_only_when_configured_and_supported(tmp_path, monkeypatch, caplog) -> None:
    """The settle delay reaches the driver only when set and accepted.

    A driver that declares no ``relay_settle_s`` keeps its own default and a
    warning is logged instead of failing the whole board registry.
    """
    worker = _make_worker(tmp_path)

    await worker._build_amplifiers(make_settings([make_amplifier_settings("default")]))
    assert worker.amplifiers["default"].relay_settle_s == pytest.approx(0.008)

    await worker._build_amplifiers(make_settings([make_amplifier_settings("explicit", relay_settle_s=0.02)]))
    assert worker.amplifiers["explicit"].relay_settle_s == pytest.approx(0.02)

    register_fake_driver(monkeypatch, factory=_NoSettleAmplifier)
    with caplog.at_level(logging.WARNING):
        await worker._build_amplifiers(make_settings([make_amplifier_settings("strict", relay_settle_s=0.05)]))

    assert worker.amplifiers["strict"].relay_settle_s == pytest.approx(0.008)
    assert any("does not accept relay_settle_s" in record.getMessage() for record in caplog.records)
    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_build_amplifiers_rejects_duplicate_labels(tmp_path) -> None:
    """Labels are the registry key, so a duplicate is a hard configuration error."""
    worker = _make_worker(tmp_path)
    settings = make_settings([make_amplifier_settings("dup"), make_amplifier_settings("dup")])

    with pytest.raises(ValueError, match="Duplicate amplifier label 'dup'"):
        await worker._build_amplifiers(settings)


@pytest.mark.asyncio
async def test_build_amplifiers_names_the_board_on_invalid_serial_settings(tmp_path) -> None:
    """A serial value the HAL rejects surfaces as a ValueError naming the label.

    ``baudrate=12345`` passes the config model's positive-value check but is not
    in the HAL's supported baud-rate list, so it fails while building the
    connection config rather than while parsing the file.
    """
    worker = _make_worker(tmp_path)
    serial = AmplifierSerialSettings(port="/dev/ttyUSB0", baudrate=12345)

    with pytest.raises(ValueError, match="amplifier 'bad': invalid serial settings"):
        await worker._build_amplifiers(make_settings([make_amplifier_settings("bad", serial=serial)]))


@pytest.mark.asyncio
async def test_initialize_reports_bad_serial_as_startup_error_naming_the_label(tmp_path, caplog) -> None:
    """Startup fails with an error that names the offending board."""
    serial = AmplifierSerialSettings(port="/dev/ttyUSB0", baudrate=12345)
    _write_config(tmp_path, make_settings([make_amplifier_settings("bad", serial=serial)]))
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    messages = [record.getMessage() for record in caplog.records]
    assert any("invalid serial settings" in message and "'bad'" in message for message in messages)


@pytest.mark.asyncio
async def test_initialize_reports_duplicate_label_as_startup_error(tmp_path, caplog) -> None:
    """A duplicate label fails startup and is named in the log."""
    _write_config(tmp_path, make_settings([make_amplifier_settings("dup"), make_amplifier_settings("dup")]))
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    messages = [record.getMessage() for record in caplog.records]
    assert any("Duplicate amplifier label 'dup'" in message for message in messages)


@pytest.mark.asyncio
async def test_initialize_seeds_registry_from_yaml(tmp_path, monkeypatch) -> None:
    """With no merged remote section, the YAML-seeded registry is kept."""

    async def fake_initialize(self) -> bool:
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    _write_config(tmp_path, make_settings([make_amplifier_settings("yaml", address=4)]))
    worker = _make_worker(tmp_path)

    assert await worker.initialize() is True
    assert set(worker.amplifiers) == {"yaml"}
    assert worker.amplifier_settings["yaml"].address == 4
    assert worker._startup_complete is True
    await worker.cleanup()


@pytest.mark.asyncio
async def test_rebuild_closes_previous_drivers_once(tmp_path) -> None:
    """``_build_amplifiers`` is idempotent: the old registry is torn down once."""
    worker = _make_worker(tmp_path)
    settings = make_settings([make_amplifier_settings("a")])

    await worker._build_amplifiers(settings)
    old = worker.amplifiers["a"]
    assert old.close_calls == 0

    await worker._build_amplifiers(settings)

    assert old.close_calls == 1
    assert worker.amplifiers["a"] is not old
    await worker.close_amplifiers()
    assert worker.amplifiers["a"].close_calls == 1


@pytest.mark.asyncio
async def test_registry_maps_every_configured_label(tmp_path) -> None:
    """The public maps share the configured labels and start zeroed."""
    worker = _make_worker(tmp_path)
    settings = make_settings([make_amplifier_settings("a"), make_amplifier_settings("b")])

    await worker._build_amplifiers(settings)

    expected = {"a", "b"}
    assert set(worker.amplifiers) == expected
    assert set(worker.amplifier_settings) == expected
    assert set(worker.monitoring) == expected
    assert set(worker.connected) == expected
    assert set(worker.stats) == expected
    assert worker.monitoring == {"a": False, "b": False}
    assert worker.connected == {"a": False, "b": False}

    for label in expected:
        assert worker.amplifier_settings[label].label == label
        stats = worker.stats[label]
        assert stats.reading_count == 0
        assert stats.error_count == 0
        assert stats.last_state is None
        assert stats.last_reading_at is None
        assert stats.last_error is None
        assert stats.consecutive_failures == 0

    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_poll_loop_publishes_one_reading_per_iteration(tmp_path, monkeypatch) -> None:
    """One successful poll writes the full stream entry to the board's key."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(make_settings([make_amplifier_settings("a", poll_interval=0.5)]))
    worker.amplifiers["a"].state = AmplifierState(
        output_voltage_v=1.25,
        output_current_a=2.5e-3,
        input_current_a=2.5e-6,
        boost_gain=3.0,
        feedback_resistor_ohm=1_000_000.0,
        dc_bias_enabled=True,
        dc_bias_level=0.5,
        bandwidth_hz=None,
        filters=(FilterSetting(type=FilterType.LOW_PASS, enabled=True, high_cutoff_hz=2000.0),),
        input_overcurrent=True,
        output_overload=False,
        input_compensation_enabled=None,
        input_compensation_level=1.5,
        inverted=None,
    )

    client = _FakeClient()
    monkeypatch.setattr(worker, "_client", client)

    async def cancel_after_one(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_one)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_amplifier("a")

    assert len(client.calls) == 1
    key, values, options = client.calls[0]
    assert key == f"scietex:{worker.service_name}:{worker.instance_id}:current_amplifier:a:readings"

    fields = dict(values)
    assert list(fields) == [
        b"ts",
        b"output_voltage_v",
        b"output_current_a",
        b"input_current_a",
        b"boost_gain",
        b"feedback_resistor_ohm",
        b"preamp_range",
        b"dc_bias_level",
        b"bandwidth_hz",
        b"input_compensation_level",
        b"dc_bias_enabled",
        b"input_compensation_enabled",
        b"input_overcurrent",
        b"output_overload",
        b"inverted",
        b"filters",
        b"transconductance_v_per_a",
    ]

    # The timestamp is a tz-aware ISO-8601 instant.
    assert datetime.fromisoformat(fields[b"ts"].decode()).tzinfo is not None

    # Both tooltip spellings carry the selected feedback resistor.
    assert fields[b"preamp_range"] == fields[b"feedback_resistor_ohm"] == b"1000000.0"

    # Flags stay tri-state: reported True/False and unknown None.
    assert fields[b"dc_bias_enabled"] == b"1"
    assert fields[b"output_overload"] == b"0"
    assert fields[b"input_overcurrent"] == b"1"
    assert fields[b"input_compensation_enabled"] == b""
    assert fields[b"inverted"] == b""
    assert fields[b"bandwidth_hz"] == b""

    # Filters are one JSON string, not a struct sequence.
    decoded_filters = json.loads(fields[b"filters"])
    assert decoded_filters == [{"type": "low_pass", "enabled": True, "low_cutoff_hz": None, "high_cutoff_hz": 2000.0}]

    assert fields[b"transconductance_v_per_a"] == b"3000000.0"

    assert worker.stats["a"].reading_count == 1
    assert worker.stats["a"].last_error is None
    assert options.trim.exact is False
    assert options.trim.threshold == 10000
    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_failure_of_one_board_does_not_stop_another(tmp_path, monkeypatch) -> None:
    """Board A failing reads never escapes its task and leaves B publishing."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(
        make_settings(
            [
                make_amplifier_settings("a", poll_interval=0.001),
                make_amplifier_settings("b", poll_interval=0.001),
            ],
            failure_threshold=2,
        )
    )

    client = _FakeClient()
    monkeypatch.setattr(worker, "_client", client)

    async def short_sleep(_delay: float) -> None:
        await asyncio.sleep(0.001)

    monkeypatch.setattr(worker, "_sleep", short_sleep)

    task_a = asyncio.create_task(worker._poll_amplifier("a"))
    task_b = asyncio.create_task(worker._poll_amplifier("b"))
    try:
        await _wait_for(lambda: worker.stats["a"].reading_count >= 1 and worker.stats["b"].reading_count >= 1)
        assert worker.connected["a"] is True

        published_before_failure = worker.stats["a"].reading_count
        worker.amplifiers["a"].read_exc = RuntimeError("bus down")

        await _wait_for(lambda: worker.stats["a"].consecutive_failures >= 2 and worker.stats["b"].reading_count >= 3)

        assert not task_a.done()
        assert not task_b.done()

        assert worker.connected["a"] is False
        assert worker.stats["a"].reading_count == published_before_failure
        assert worker.stats["a"].error_count >= 2
        assert worker.stats["a"].last_error == "bus down"

        assert worker.connected["b"] is True
        assert worker.stats["b"].consecutive_failures == 0
        assert worker.stats["b"].error_count == 0

        # Board A published only the readings it completed before the bus
        # failed; B kept publishing throughout.
        a_key = f"scietex:{worker.service_name}:{worker.instance_id}:current_amplifier:a:readings"
        b_key = f"scietex:{worker.service_name}:{worker.instance_id}:current_amplifier:b:readings"
        assert [key for key, _, _ in client.calls].count(a_key) == worker.stats["a"].reading_count
        assert [key for key, _, _ in client.calls].count(b_key) == worker.stats["b"].reading_count
        assert worker.stats["b"].reading_count >= 3
    finally:
        task_a.cancel()
        task_b.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_poll_loop_recovers_after_failed_reads(tmp_path, monkeypatch) -> None:
    """A successful read resets the failure streak and reconnects the board."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(make_settings([make_amplifier_settings("a", poll_interval=0.001)], failure_threshold=2))
    amplifier = worker.amplifiers["a"]
    amplifier.read_exc = RuntimeError("bus down")

    monkeypatch.setattr(worker, "_client", _FakeClient())

    async def short_sleep(_delay: float) -> None:
        await asyncio.sleep(0.001)

    monkeypatch.setattr(worker, "_sleep", short_sleep)

    task = asyncio.create_task(worker._poll_amplifier("a"))
    try:
        await _wait_for(lambda: worker.stats["a"].consecutive_failures >= 2)
        assert worker.connected["a"] is False

        amplifier.read_exc = None

        await _wait_for(lambda: worker.stats["a"].reading_count >= 1)
        assert worker.stats["a"].consecutive_failures == 0
        assert worker.connected["a"] is True
        assert worker.stats["a"].last_error is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_start_and_stop_monitoring(tmp_path) -> None:
    """Monitoring selection is per label, idempotent, and rejects unknown labels."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(make_settings([make_amplifier_settings("a"), make_amplifier_settings("b")]))

    assert worker.start_monitoring("a") == ["a"]
    task_a = worker._poll_tasks["a"]
    assert worker.monitoring["a"] is True
    assert worker.monitoring["b"] is False

    # Already monitoring: nothing new is started.
    assert worker.start_monitoring("a") == []

    with pytest.raises(KeyError):
        worker.start_monitoring("unknown")
    with pytest.raises(KeyError):
        worker.stop_monitoring("unknown")

    # Start all: only the not-yet-monitoring labels are returned.
    assert worker.start_monitoring() == ["b"]
    task_b = worker._poll_tasks["b"]

    assert worker.stop_monitoring("a") == ["a"]
    assert worker.monitoring["a"] is False
    assert worker.connected["a"] is False

    # Stopping an already stopped board is a no-op.
    assert worker.stop_monitoring("a") == []

    assert worker.stop_monitoring() == ["b"]
    assert worker._poll_tasks == {}
    assert worker.monitoring == {"a": False, "b": False}

    await asyncio.gather(task_a, task_b, return_exceptions=True)
    assert task_a.cancelled() is True
    assert task_b.cancelled() is True
    await worker.close_amplifiers()


def test_restart_required_after_post_startup_apply(tmp_path) -> None:
    """A settings apply before startup only stores; after startup it needs a restart."""
    worker = _make_worker(tmp_path)
    assert worker.restart_required is False

    worker._apply_settings(make_settings([]))
    assert worker.restart_required is False

    worker._startup_complete = True
    worker._apply_settings(make_settings([make_amplifier_settings("a")]))
    assert worker.restart_required is True


@pytest.mark.asyncio
async def test_cleanup_cancels_poll_tasks_and_closes_drivers(tmp_path) -> None:
    """The shutdown path cancels every poll task and closes each driver once."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(make_settings([make_amplifier_settings("a"), make_amplifier_settings("b")]))
    amplifier_a = worker.amplifiers["a"]
    amplifier_b = worker.amplifiers["b"]

    assert worker.start_monitoring() == ["a", "b"]
    tasks = list(worker._poll_tasks.values())

    await worker.cleanup()

    assert worker._poll_tasks == {}
    assert worker.monitoring == {"a": False, "b": False}
    assert amplifier_a.close_calls == 1
    assert amplifier_b.close_calls == 1

    await asyncio.gather(*tasks, return_exceptions=True)
    assert all(task.cancelled() for task in tasks)
