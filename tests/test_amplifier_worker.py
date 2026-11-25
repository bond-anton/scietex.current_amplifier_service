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
    _FakeTcpAmplifier,
    make_device_settings,
    make_serial_transport,
    make_settings,
    make_tcp_transport,
    register_fake_driver,
)
from scietex.hal.current_amplifier.base.capabilities import FilterType
from scietex.hal.current_amplifier.base.data import AmplifierState, FilterSetting
from scietex.hal.current_amplifier.scietex.ebic.modbus.v1 import GatewayBusy
from scietex.service import ValkeyWorker, ValkeyWorkerConfig

from scietex.current_amplifier_service.amplifier_worker import AmplifierStats, CurrentAmplifierWorker
from scietex.current_amplifier_service.config import (
    CURRENT_AMPLIFIER_CONFIG_FILE,
    CURRENT_AMPLIFIER_CONFIG_SUBDIR,
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
async def test_build_amplifiers_instantiates_one_driver_per_device(tmp_path) -> None:
    """Each configured device gets its own driver, built from the shared transport."""
    worker = _make_worker(tmp_path)
    settings = make_settings(
        {
            "a": make_device_settings(address=2),
            "board_b": make_device_settings(address=7, label="Board B"),
        },
        transport=make_serial_transport(
            port="/dev/ttyUSB0",
            baudrate=38400,
            bytesize=7,
            parity="E",
            stopbits=2,
            timeout=0.5,
            framer="ASCII",
        ),
    )

    await worker._build_amplifiers(settings)

    assert set(worker.amplifiers) == {"a", "board_b"}
    first = worker.amplifiers["a"]
    second = worker.amplifiers["board_b"]
    assert isinstance(first, _FakeAmplifier)
    assert first is not second

    # The driver's label is logging-only: it falls back to the device id.
    assert first.label == "a"
    assert first.address == 2
    assert first.logger is worker.logger
    assert second.label == "Board B"
    assert second.address == 7

    for amplifier in (first, second):
        con_params = amplifier.con_params
        assert con_params.port == "/dev/ttyUSB0"
        assert con_params.baudrate == 38400
        assert con_params.bytesize == 7
        assert con_params.parity == "E"
        assert con_params.stopbits == 2
        assert isinstance(con_params.stopbits, int)
        assert con_params.timeout == 0.5
        assert con_params.framer == "ASCII"

    await worker.close_amplifiers()
    assert first.close_calls == 1
    assert second.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["write_timeout", "inter_byte_timeout"])
async def test_build_amplifiers_rejects_a_serial_timeout_the_hal_cannot_carry(tmp_path, field) -> None:
    """A configured serial timeout the installed HAL drops fails with a labelled error.

    The Modbus serial config in this HAL release accepts
    ``write_timeout``/``inter_byte_timeout`` as unused kwargs, so a value would
    be silently inert. The worker refuses instead of pretending.
    """
    worker = _make_worker(tmp_path)
    settings = make_settings({"a": make_device_settings()}, transport=make_serial_transport(**{field: 0.5}))

    with pytest.raises(ValueError, match=rf"device 'a': the installed scietex.hal.serial does not honour transport\.{field}"):
        await worker._build_amplifiers(settings)

    assert worker.amplifiers == {}


@pytest.mark.asyncio
async def test_relay_settle_forwarded_only_when_configured_and_supported(tmp_path, monkeypatch, caplog) -> None:
    """The settle delay reaches the driver only when set and accepted.

    A driver that declares no ``relay_settle_s`` keeps its own default and a
    warning is logged instead of failing the whole device registry.
    """
    worker = _make_worker(tmp_path)

    await worker._build_amplifiers(make_settings({"default": make_device_settings()}))
    assert worker.amplifiers["default"].relay_settle_s == pytest.approx(0.008)

    await worker._build_amplifiers(make_settings({"explicit": make_device_settings(relay_settle_s=0.02)}))
    assert worker.amplifiers["explicit"].relay_settle_s == pytest.approx(0.02)

    register_fake_driver(monkeypatch, factory=_NoSettleAmplifier)
    with caplog.at_level(logging.WARNING):
        await worker._build_amplifiers(make_settings({"strict": make_device_settings(relay_settle_s=0.05)}))

    assert worker.amplifiers["strict"].relay_settle_s == pytest.approx(0.008)
    assert any("does not accept relay_settle_s" in record.getMessage() for record in caplog.records)
    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_build_amplifiers_forwards_tcp_endpoint(tmp_path) -> None:
    """A TCP device is built with the gateway endpoint, not a serial config."""
    worker = _make_worker(tmp_path)
    settings = make_settings(
        {"preamp_1": make_device_settings(driver="ebic_modbus_tcp_v1", address=9, relay_settle_s=0.02)},
        transport=make_tcp_transport(host="gateway.local", tcp_port=1502, tcp_timeout=8.5, tcp_retries=2),
    )

    await worker._build_amplifiers(settings)

    tcp = worker.amplifiers["preamp_1"]
    assert isinstance(tcp, _FakeTcpAmplifier)
    assert tcp.host == "gateway.local"
    assert tcp.port == 1502
    assert tcp.address == 9
    assert tcp.timeout == 8.5
    assert tcp.retries == 2
    assert tcp.label == "preamp_1"
    assert tcp.logger is worker.logger
    assert tcp.con_params is None
    assert tcp.relay_settle_s == pytest.approx(0.02)

    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_build_amplifiers_rejects_a_driver_that_contradicts_the_transport(tmp_path) -> None:
    """The build path re-checks the driver family before instantiating anything.

    The config model rejects a mismatch, so this covers a settings object built
    outside that validated path — ``_build_amplifiers`` is a public hook and
    must not hand a serial driver TCP keywords.
    """
    worker = _make_worker(tmp_path)
    settings = make_settings({"bad": make_device_settings()}, transport=make_tcp_transport())

    with pytest.raises(ValueError, match="is a rs485 driver but transport kind is 'tcp'"):
        await worker._build_amplifiers(settings)

    assert worker.amplifiers == {}


@pytest.mark.asyncio
async def test_build_amplifiers_names_the_device_on_invalid_serial_settings(tmp_path) -> None:
    """A serial value the HAL rejects surfaces as a ValueError naming the device.

    ``baudrate=12345`` passes the config model's positive-value check but is not
    in the HAL's supported baud-rate list, so it fails while building the
    connection config rather than while parsing the file.
    """
    worker = _make_worker(tmp_path)
    settings = make_settings({"bad": make_device_settings()}, transport=make_serial_transport(baudrate=12345))

    with pytest.raises(ValueError, match="device 'bad': invalid serial settings"):
        await worker._build_amplifiers(settings)


@pytest.mark.asyncio
async def test_initialize_reports_bad_serial_as_startup_error_naming_the_device(tmp_path, caplog) -> None:
    """Startup fails with an error that names the offending device."""
    _write_config(
        tmp_path,
        make_settings({"bad": make_device_settings()}, transport=make_serial_transport(baudrate=12345)),
    )
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    messages = [record.getMessage() for record in caplog.records]
    assert any("invalid serial settings" in message and "'bad'" in message for message in messages)


@pytest.mark.asyncio
async def test_initialize_reports_invalid_config_as_startup_error(tmp_path, caplog) -> None:
    """A config the model rejects fails startup and names the offending field."""
    path = tmp_path / CURRENT_AMPLIFIER_CONFIG_SUBDIR / CURRENT_AMPLIFIER_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("transport:\n  port: /dev/ttyUSB0\ndevices:\n  dup_a:\n    address: 2\n  dup_b:\n    address: 2\n")
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    messages = [record.getMessage() for record in caplog.records]
    assert any("share Modbus address 2" in message for message in messages)


@pytest.mark.asyncio
async def test_initialize_seeds_registry_from_yaml(tmp_path, monkeypatch) -> None:
    """With no merged remote section, the YAML-seeded registry is kept."""

    async def fake_initialize(self) -> bool:
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    _write_config(tmp_path, make_settings({"yaml": make_device_settings(address=4)}))
    worker = _make_worker(tmp_path)

    assert await worker.initialize() is True
    assert set(worker.amplifiers) == {"yaml"}
    assert worker.amplifier_settings["yaml"].address == 4
    assert worker._startup_complete is True
    # Boot seeds the registry but never starts a poll loop.
    assert worker.monitoring == {"yaml": False}
    assert worker._poll_tasks == {}
    await worker.cleanup()


@pytest.mark.asyncio
async def test_rebuild_closes_previous_drivers_once(tmp_path) -> None:
    """``_build_amplifiers`` is idempotent: the old registry is torn down once."""
    worker = _make_worker(tmp_path)
    settings = make_settings({"a": make_device_settings()})

    await worker._build_amplifiers(settings)
    old = worker.amplifiers["a"]
    assert old.close_calls == 0

    await worker._build_amplifiers(settings)

    assert old.close_calls == 1
    assert worker.amplifiers["a"] is not old
    await worker.close_amplifiers()
    assert worker.amplifiers["a"].close_calls == 1


@pytest.mark.asyncio
async def test_registry_maps_every_device_id(tmp_path) -> None:
    """The public maps share the configured device ids and start zeroed."""
    worker = _make_worker(tmp_path)
    settings = make_settings({"a": make_device_settings(address=2), "b": make_device_settings(address=3)})

    await worker._build_amplifiers(settings)

    expected = {"a", "b"}
    assert set(worker.amplifiers) == expected
    assert set(worker.amplifier_settings) == expected
    assert set(worker.monitoring) == expected
    assert set(worker.connected) == expected
    assert set(worker.stats) == expected
    assert worker.monitoring == {"a": False, "b": False}
    assert worker.connected == {"a": False, "b": False}

    for device in expected:
        stats = worker.stats[device]
        assert stats.reading_count == 0
        assert stats.error_count == 0
        assert stats.last_state is None
        assert stats.last_reading_at is None
        assert stats.last_error is None
        assert stats.consecutive_failures == 0

    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_poll_loop_publishes_one_reading_per_iteration(tmp_path, monkeypatch) -> None:
    """One successful poll writes the full stream entry to the device's key.

    The key segment is the device id, not the display label, so the label is
    set to a different string to prove which one is used.
    """
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(
        make_settings({"a": make_device_settings(label="Photodiode A", address=2, poll_interval=0.5)})
    )
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
async def test_failure_of_one_device_does_not_stop_another(tmp_path, monkeypatch) -> None:
    """Device A failing reads never escapes its task and leaves B publishing."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(
        make_settings(
            {
                "a": make_device_settings(address=2, poll_interval=0.001),
                "b": make_device_settings(address=4, poll_interval=0.001),
            },
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

        # Device A published only the readings it completed before the bus
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
    """A successful read resets the failure streak and reconnects the device."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(make_settings({"a": make_device_settings(poll_interval=0.001)}, failure_threshold=2))
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
async def test_failed_polls_back_off_exponentially_and_reset_on_success(tmp_path, monkeypatch) -> None:
    """A failing device backs off to 10x the interval; one success returns to base."""
    worker = _make_worker(tmp_path)
    interval = 0.01
    await worker._build_amplifiers(make_settings({"a": make_device_settings(poll_interval=interval)}, failure_threshold=1))
    amplifier = worker.amplifiers["a"]
    amplifier.read_exc = RuntimeError("bus down")

    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 4:
            # The device answers again; the next poll must not wait for the backoff.
            amplifier.read_exc = None
        if len(delays) >= 6:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", record)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_amplifier("a")

    assert delays == [
        pytest.approx(2 * interval),
        pytest.approx(4 * interval),
        pytest.approx(8 * interval),
        pytest.approx(10 * interval),
        pytest.approx(interval),
        pytest.approx(interval),
    ]
    assert worker.stats["a"].consecutive_failures == 0
    assert worker.connected["a"] is True
    await worker.close_amplifiers()


def test_poll_delay_stays_capped_for_a_long_outage(tmp_path) -> None:
    """A device down for thousands of polls stays at the cap instead of overflowing."""
    worker = _make_worker(tmp_path)
    stats = AmplifierStats(consecutive_failures=5000)

    delay = worker._poll_delay(make_device_settings(poll_interval=1.0), stats)

    assert delay == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_gateway_busy_backs_off_without_marking_the_device_disconnected(tmp_path, monkeypatch) -> None:
    """A contended gateway is retried with backoff but the device stays connected."""
    worker = _make_worker(tmp_path)
    interval = 0.01
    await worker._build_amplifiers(make_settings({"a": make_device_settings(poll_interval=interval)}, failure_threshold=1))
    worker.amplifiers["a"].read_exc = GatewayBusy("busy")
    worker._connected["a"] = True

    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", record)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_amplifier("a")

    assert worker.stats["a"].consecutive_failures == 3
    assert worker.stats["a"].error_count == 3
    assert worker.connected["a"] is True
    assert delays == [pytest.approx(2 * interval), pytest.approx(4 * interval), pytest.approx(8 * interval)]
    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_start_and_stop_monitoring(tmp_path) -> None:
    """Monitoring selection is per device id, idempotent, and rejects unknown ids."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(make_settings({"a": make_device_settings(address=2), "b": make_device_settings(address=3)}))

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

    # Start all: only the not-yet-monitoring devices are returned.
    assert worker.start_monitoring() == ["b"]
    task_b = worker._poll_tasks["b"]

    assert worker.stop_monitoring("a") == ["a"]
    assert worker.monitoring["a"] is False
    assert worker.connected["a"] is False

    # Stopping an already stopped device is a no-op.
    assert worker.stop_monitoring("a") == []

    assert worker.stop_monitoring() == ["b"]
    assert worker._poll_tasks == {}
    assert worker.monitoring == {"a": False, "b": False}

    await asyncio.gather(task_a, task_b, return_exceptions=True)
    assert task_a.cancelled() is True
    assert task_b.cancelled() is True
    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_start_monitoring_skips_devices_out_of_the_poll_plan(tmp_path) -> None:
    """A ``poll=false`` device is never started, even when named explicitly."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(
        make_settings(
            {
                "polled": make_device_settings(address=2, poll=True),
                "on_demand": make_device_settings(address=3, poll=False),
            }
        )
    )

    assert worker.start_monitoring() == ["polled"]
    assert worker.start_monitoring("on_demand") == []
    assert worker._poll_tasks.keys() == {"polled"}
    assert worker.monitoring == {"polled": True, "on_demand": False}
    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_poll_enabled_false_starts_nothing(tmp_path) -> None:
    """``poll_enabled=false`` gates the whole poll plan."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(
        make_settings({"a": make_device_settings(), "b": make_device_settings(address=3)}, poll_enabled=False)
    )

    assert worker.start_monitoring() == []
    assert worker.start_monitoring("a") == []
    assert worker._poll_tasks == {}
    assert worker.monitoring == {"a": False, "b": False}
    await worker.close_amplifiers()


@pytest.mark.asyncio
async def test_on_demand_read_of_a_poll_false_device_still_works(tmp_path, monkeypatch) -> None:
    """A device outside the poll plan publishes a reading when polled explicitly."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(make_settings({"on_demand": make_device_settings(address=3, poll=False)}))

    client = _FakeClient()
    monkeypatch.setattr(worker, "_client", client)

    async def cancel_after_one(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_one)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_amplifier("on_demand")

    assert worker.stats["on_demand"].reading_count == 1
    assert [key for key, _, _ in client.calls] == [
        f"scietex:{worker.service_name}:{worker.instance_id}:current_amplifier:on_demand:readings"
    ]
    await worker.close_amplifiers()


def test_restart_required_after_post_startup_apply(tmp_path) -> None:
    """A settings apply before startup only stores; after startup it needs a restart."""
    worker = _make_worker(tmp_path)
    assert worker.restart_required is False

    worker._apply_settings(make_settings())
    assert worker.restart_required is False

    worker._startup_complete = True
    worker._apply_settings(make_settings({"a": make_device_settings()}))
    assert worker.restart_required is True


@pytest.mark.asyncio
async def test_cleanup_cancels_poll_tasks_and_closes_drivers(tmp_path) -> None:
    """The shutdown path cancels every poll task and closes each driver once."""
    worker = _make_worker(tmp_path)
    await worker._build_amplifiers(make_settings({"a": make_device_settings(address=2), "b": make_device_settings(address=3)}))
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
