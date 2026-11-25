"""Tests for the ``amplifier:*`` data-plane task handler."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import msgspec
import pytest
from _helpers import (
    FAKE_DRIVER_NAME,
    _make_capabilities,
    _make_context,
    _make_task,
    make_device_settings,
    make_settings,
    make_tcp_transport,
)
from scietex.hal.current_amplifier.base.capabilities import (
    Access,
    BandwidthCapabilities,
    BoostCapabilities,
    CompensationCapabilities,
    CurrentAmplifierCapabilities,
    DcBiasCapabilities,
    FilterCapabilities,
    FilterType,
    PreampCapabilities,
    SelectableCapabilities,
)
from scietex.hal.current_amplifier.base.data import AmplifierState, FilterSetting
from scietex.hal.current_amplifier.base.errors import UnexpectedResponse
from scietex.hal.current_amplifier.base.protocol import CurrentAmplifierProtocol
from scietex.hal.current_amplifier.scietex.ebic.modbus.v1 import GatewayBusy, GatewayTargetFailed
from scietex.hal.current_amplifier.scietex.ebic.modbus.v1.semantic import RelayTransitionError
from scietex.hal.serial import ModbusOperationError
from scietex.service.task_handler import TaskData

from scietex.current_amplifier_service.amplifier_worker import AmplifierStats
from scietex.current_amplifier_service.config import (
    AmplifierDeviceSettings,
    CurrentAmplifierServiceSettings,
)
from scietex.current_amplifier_service.task_handlers import (
    AMPLIFIER_LIST_TASK_NAME,
    AMPLIFIER_READ_TASK_NAME,
    AMPLIFIER_SET_BANDWIDTH_TASK_NAME,
    AMPLIFIER_SET_BOOST_GAIN_TASK_NAME,
    AMPLIFIER_SET_DC_BIAS_LEVEL_TASK_NAME,
    AMPLIFIER_SET_DC_BIAS_TASK_NAME,
    AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME,
    AMPLIFIER_SET_FILTER_TASK_NAME,
    AMPLIFIER_SET_INPUT_COMPENSATION_LEVEL_TASK_NAME,
    AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME,
    AMPLIFIER_SET_INVERTED_TASK_NAME,
    AMPLIFIER_STATUS_TASK_NAME,
    AMPLIFIER_WRITE_OUTCOME_UNKNOWN,
    AmplifierCommandError,
    AmplifierCommandResponse,
    AmplifierControlHandler,
    AmplifierListResponse,
    AmplifierStatusResponse,
)

if TYPE_CHECKING:
    from scietex.current_amplifier_service.amplifier_worker import CurrentAmplifierWorker

DEVICE = "a"

_READ_WRITE = Access(read=True, write=True)
_READ_ONLY = Access(read=True)
_RESISTOR_OPTIONS = (1_000.0, 1_000_000.0)
_GAIN_OPTIONS = (1.0, 3.0, 10.0, 30.0)
_FILTER = FilterSetting(type=FilterType.LOW_PASS, enabled=True, low_cutoff_hz=10.0, high_cutoff_hz=1_000.0)

_WRITE_TASK_NAMES = [
    AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME,
    AMPLIFIER_SET_BOOST_GAIN_TASK_NAME,
    AMPLIFIER_SET_DC_BIAS_TASK_NAME,
    AMPLIFIER_SET_DC_BIAS_LEVEL_TASK_NAME,
    AMPLIFIER_SET_BANDWIDTH_TASK_NAME,
    AMPLIFIER_SET_FILTER_TASK_NAME,
    AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME,
    AMPLIFIER_SET_INPUT_COMPENSATION_LEVEL_TASK_NAME,
    AMPLIFIER_SET_INVERTED_TASK_NAME,
]

_ALL_TASK_NAMES = [
    AMPLIFIER_LIST_TASK_NAME,
    AMPLIFIER_STATUS_TASK_NAME,
    AMPLIFIER_READ_TASK_NAME,
    *_WRITE_TASK_NAMES,
]


def _full_capabilities() -> CurrentAmplifierCapabilities:
    """A capability tree with every optional write enabled."""
    return CurrentAmplifierCapabilities(
        preamp=PreampCapabilities(
            feedback_resistor=SelectableCapabilities(access=_READ_WRITE, options=_RESISTOR_OPTIONS),
            max_output_v=5.0,
        ),
        boost=BoostCapabilities(gain=SelectableCapabilities(access=_READ_WRITE, options=_GAIN_OPTIONS)),
        dc_bias=DcBiasCapabilities(enabled=_READ_WRITE, level=_READ_WRITE),
        bandwidth=BandwidthCapabilities(access=_READ_WRITE, options=(1_000.0, 10_000.0)),
        filters=FilterCapabilities(
            types=(FilterType.LOW_PASS, FilterType.HIGH_PASS, FilterType.BAND_PASS), enabled=_READ_WRITE, cutoff=_READ_WRITE
        ),
        input_compensation=CompensationCapabilities(enabled=_READ_WRITE, level=_READ_WRITE),
        invert_output=_READ_WRITE,
    )


def _read_only(gate: str) -> CurrentAmplifierCapabilities:
    """The full tree with exactly the write access behind ``gate`` closed.

    The toggle tasks gate on ``enabled.write`` (the filter adds
    ``cutoff.write`` only when cutoffs are supplied), so the sibling write
    stays open where it is not the gate under test. That leaves the OR-based
    ``can_set_*`` protocol properties truthy, proving the handler gates on the
    field the task exercises.
    """
    tree = _full_capabilities()
    if gate == "can_select_preamp":
        preamp = replace(tree.preamp, feedback_resistor=SelectableCapabilities(access=_READ_ONLY, options=_RESISTOR_OPTIONS))
        return replace(tree, preamp=preamp)
    if gate == "can_set_boost":
        boost = replace(tree.boost, gain=SelectableCapabilities(access=_READ_ONLY, options=_GAIN_OPTIONS))
        return replace(tree, boost=boost)
    if gate == "capabilities.dc_bias.enabled.write":
        return replace(tree, dc_bias=DcBiasCapabilities(enabled=_READ_ONLY, level=_READ_WRITE))
    if gate == "capabilities.dc_bias.level.write":
        return replace(tree, dc_bias=DcBiasCapabilities(enabled=_READ_WRITE, level=_READ_ONLY))
    if gate == "can_set_bandwidth":
        return replace(tree, bandwidth=BandwidthCapabilities(access=_READ_ONLY, options=(1_000.0, 10_000.0)))
    if gate == "capabilities.filters.enabled.write":
        filters = FilterCapabilities(
            types=(FilterType.LOW_PASS, FilterType.HIGH_PASS, FilterType.BAND_PASS),
            enabled=_READ_ONLY,
            cutoff=_READ_WRITE,
        )
        return replace(tree, filters=filters)
    if gate == "capabilities.filters.cutoff.write":
        filters = FilterCapabilities(
            types=(FilterType.LOW_PASS, FilterType.HIGH_PASS, FilterType.BAND_PASS),
            enabled=_READ_WRITE,
            cutoff=_READ_ONLY,
        )
        return replace(tree, filters=filters)
    if gate == "capabilities.input_compensation.enabled.write":
        return replace(tree, input_compensation=CompensationCapabilities(enabled=_READ_ONLY, level=_READ_WRITE))
    if gate == "capabilities.input_compensation.level.write":
        return replace(tree, input_compensation=CompensationCapabilities(enabled=_READ_WRITE, level=_READ_ONLY))
    if gate == "can_set_invert_output":
        return replace(tree, invert_output=_READ_ONLY)
    raise AssertionError(f"unmapped capability gate {gate!r}")


def _make_state(filters: tuple[FilterSetting, ...] = ()) -> AmplifierState:
    """A state snapshot with fixed values and the requested filter entries."""
    return AmplifierState(
        output_voltage_v=2.5,
        output_current_a=2.5e-3,
        input_current_a=2.5e-6,
        boost_gain=1.0,
        feedback_resistor_ohm=1_000_000.0,
        dc_bias_enabled=False,
        dc_bias_level=0.0,
        bandwidth_hz=None,
        filters=filters,
        input_overcurrent=False,
        output_overload=False,
        input_compensation_enabled=False,
        input_compensation_level=0.0,
        inverted=False,
    )


class _RecordingAmplifier(CurrentAmplifierProtocol):
    """A protocol fake recording every control call and able to raise on demand.

    ``capabilities`` defaults to a fully writeable tree and may be shadowed per
    instance. ``control_exc`` makes every setter raise after recording the call,
    with ``control_cause`` as its ``__cause__`` when supplied — the shape the
    EBIC driver produces, where a ``RelayTransitionError`` wraps the Modbus
    failure. ``read_exc`` makes ``read_data`` raise, with ``read_cause`` as its
    ``__cause__``. ``strict`` makes any I/O raise, so a registry-only task
    proves it never touched the driver.
    """

    capabilities = _full_capabilities()

    def __init__(
        self,
        *,
        capabilities: CurrentAmplifierCapabilities | None = None,
        state: AmplifierState | None = None,
        control_exc: Exception | None = None,
        control_cause: BaseException | None = None,
        read_exc: Exception | None = None,
        read_cause: BaseException | None = None,
        strict: bool = False,
    ) -> None:
        if capabilities is not None:
            self.capabilities = capabilities
        self.state = _make_state() if state is None else state
        self.control_exc = control_exc
        self.control_cause = control_cause
        self.read_exc = read_exc
        self.read_cause = read_cause
        self.strict = strict
        self.connect_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self.select_feedback_resistor_calls: list[float] = []
        self.set_boost_gain_calls: list[float] = []
        self.set_dc_bias_calls: list[bool] = []
        self.set_dc_bias_level_calls: list[float] = []
        self.set_bandwidth_calls: list[float] = []
        self.set_filter_calls: list[FilterSetting] = []
        self.set_input_compensation_calls: list[bool] = []
        self.set_input_compensation_level_calls: list[float] = []
        self.set_inverted_calls: list[bool] = []

    def _forbid_io(self, operation: str) -> None:
        if self.strict:
            raise AssertionError(f"unexpected driver {operation} during a registry-only task")

    def _raise_control(self) -> None:
        if self.control_exc is not None:
            if self.control_cause is not None:
                raise self.control_exc from self.control_cause
            raise self.control_exc

    async def connect(self) -> None:
        self._forbid_io("connect")
        self.connect_calls += 1

    async def close(self) -> None:
        self._forbid_io("close")
        self.close_calls += 1

    async def read_data(self) -> AmplifierState:
        self._forbid_io("read_data")
        self.read_calls += 1
        if self.read_exc is not None:
            if self.read_cause is not None:
                raise self.read_exc from self.read_cause
            raise self.read_exc
        return self.state

    async def select_feedback_resistor(self, resistance_ohm: float) -> None:
        self._forbid_io("select_feedback_resistor")
        self.select_feedback_resistor_calls.append(resistance_ohm)
        self._raise_control()

    async def set_boost_gain(self, gain: float) -> None:
        self._forbid_io("set_boost_gain")
        self.set_boost_gain_calls.append(gain)
        self._raise_control()

    async def set_dc_bias(self, enabled: bool) -> None:
        self._forbid_io("set_dc_bias")
        self.set_dc_bias_calls.append(enabled)
        self._raise_control()

    async def set_dc_bias_level(self, level: float) -> None:
        self._forbid_io("set_dc_bias_level")
        self.set_dc_bias_level_calls.append(level)
        self._raise_control()

    async def set_bandwidth(self, bandwidth_hz: float) -> None:
        self._forbid_io("set_bandwidth")
        self.set_bandwidth_calls.append(bandwidth_hz)
        self._raise_control()

    async def set_filter(self, setting: FilterSetting) -> None:
        self._forbid_io("set_filter")
        self.set_filter_calls.append(setting)
        self._raise_control()

    async def set_input_compensation(self, enabled: bool) -> None:
        self._forbid_io("set_input_compensation")
        self.set_input_compensation_calls.append(enabled)
        self._raise_control()

    async def set_input_compensation_level(self, level: float) -> None:
        self._forbid_io("set_input_compensation_level")
        self.set_input_compensation_level_calls.append(level)
        self._raise_control()

    async def set_inverted(self, inverted: bool) -> None:
        self._forbid_io("set_inverted")
        self.set_inverted_calls.append(inverted)
        self._raise_control()


def _all_control_calls(amplifier: _RecordingAmplifier) -> list[list[Any]]:
    """Every control-call record on the fake, in task-table order."""
    return [
        amplifier.select_feedback_resistor_calls,
        amplifier.set_boost_gain_calls,
        amplifier.set_dc_bias_calls,
        amplifier.set_dc_bias_level_calls,
        amplifier.set_bandwidth_calls,
        amplifier.set_filter_calls,
        amplifier.set_input_compensation_calls,
        amplifier.set_input_compensation_level_calls,
        amplifier.set_inverted_calls,
    ]


class _StubWorker:
    """The slice of ``CurrentAmplifierWorker`` the control handler reads."""

    def __init__(self, *, settings: CurrentAmplifierServiceSettings | None = None) -> None:
        self.amplifiers: dict[str, _RecordingAmplifier] = {}
        self.amplifier_settings: dict[str, AmplifierDeviceSettings] = {}
        self.monitoring: dict[str, bool] = {}
        self.connected: dict[str, bool] = {}
        self.stats: dict[str, AmplifierStats] = {}
        self.settings = settings if settings is not None else make_settings()
        self.service_name = "test"
        self.instance_id = "inst"


def _worker_for(
    amplifier: _RecordingAmplifier,
    device: str = DEVICE,
    *,
    settings: CurrentAmplifierServiceSettings | None = None,
    device_settings: AmplifierDeviceSettings | None = None,
) -> _StubWorker:
    """A stub worker holding one board."""
    worker = _StubWorker(settings=settings)
    worker.amplifiers[device] = amplifier
    worker.amplifier_settings[device] = device_settings if device_settings is not None else make_device_settings(address=2)
    worker.monitoring[device] = False
    worker.connected[device] = False
    worker.stats[device] = AmplifierStats()
    return worker


def _worker(*devices: str, strict: bool = False) -> _StubWorker:
    """A stub worker holding one full-capability board per device id.

    Registry insertion keeps the argument order; the Modbus address follows the
    sorted device id so the list expectation is order-independent.
    """
    worker = _StubWorker()
    position = {device: index for index, device in enumerate(sorted(devices), start=1)}
    for device in devices:
        index = position[device]
        amplifier = _RecordingAmplifier(strict=strict)
        worker.amplifiers[device] = amplifier
        worker.amplifier_settings[device] = make_device_settings(address=index)
        worker.monitoring[device] = False
        worker.connected[device] = False
        worker.stats[device] = AmplifierStats()
    return worker


def _handler(worker: _StubWorker) -> AmplifierControlHandler:
    return AmplifierControlHandler("amplifier", _make_context(), worker=cast("CurrentAmplifierWorker", worker))


def _raw_task(task_name: str, payload: bytes) -> TaskData:
    """A ``TaskData`` carrying an already-encoded payload."""
    return TaskData(task_id=str(uuid4()), task=task_name, payload=payload)


def test_handler_is_data_lane() -> None:
    """The amplifier control handler is dispatched on the data lane."""
    assert AmplifierControlHandler.control is False


def test_supported_tasks_are_the_twelve_amplifier_tasks() -> None:
    """The handler declares exactly the twelve ``amplifier:*`` task names."""
    handler = _handler(_StubWorker())

    assert len(handler.supported_tasks) == 12
    assert set(handler.supported_tasks) == set(_ALL_TASK_NAMES)
    assert all(name.startswith("amplifier:") for name in handler.supported_tasks)


async def _list(worker: _StubWorker) -> AmplifierListResponse:
    """Dispatch ``amplifier:list`` and decode its response."""
    result = await _handler(worker).handle(_make_task(AMPLIFIER_LIST_TASK_NAME, None), capabilities=_make_capabilities())
    assert result.status == "success"
    return msgspec.json.decode(result.payload, type=AmplifierListResponse)


@pytest.mark.asyncio
async def test_list_reports_every_board_without_driver_io() -> None:
    """``amplifier:list`` answers from the registry and never touches the driver."""
    worker = _worker("b", "a", strict=True)

    response = await _list(worker)

    assert response.service == "test"
    assert response.instance_id == "inst"
    assert [entry.device for entry in response.amplifiers] == ["a", "b"]
    by_device = {entry.device: entry for entry in response.amplifiers}
    assert by_device["a"].driver == FAKE_DRIVER_NAME
    assert by_device["a"].address == 1
    assert by_device["a"].port == "/dev/ttyUSB0"
    assert by_device["a"].transport == "rs485"
    assert by_device["a"].label is None
    assert by_device["b"].address == 2
    # The transport is service-wide, so every device renders the same endpoint.
    assert by_device["b"].port == "/dev/ttyUSB0"
    assert by_device["b"].transport == "rs485"
    for amplifier in worker.amplifiers.values():
        assert amplifier.read_calls == 0
        assert amplifier.connect_calls == 0
        assert amplifier.close_calls == 0
        assert all(calls == [] for calls in _all_control_calls(amplifier))


@pytest.mark.asyncio
async def test_list_carries_the_label_as_display_copy() -> None:
    """A configured label rides alongside the device id, which stays the identity."""
    worker = _worker_for(
        _RecordingAmplifier(strict=True),
        device_settings=make_device_settings(address=2, label="Photodiode A"),
    )

    entry = (await _list(worker)).amplifiers[0]

    assert entry.device == DEVICE
    assert entry.label == "Photodiode A"


@pytest.mark.asyncio
async def test_list_reports_a_tcp_board_as_host_port() -> None:
    """A board behind a gateway reports ``host:port`` and its transport, not a serial device."""
    worker = _worker_for(
        _RecordingAmplifier(strict=True),
        settings=make_settings(transport=make_tcp_transport(host="gateway.local", tcp_port=1502)),
    )

    response = await _list(worker)

    entry = response.amplifiers[0]
    assert entry.port == "gateway.local:1502"
    assert entry.transport == "tcp"


@pytest.mark.asyncio
@pytest.mark.parametrize(("host", "expected"), [("::1", "[::1]:502"), ("[::1]", "[::1]:502")])
async def test_list_brackets_an_ipv6_gateway_address(host, expected) -> None:
    """An IPv6 literal keeps its colons inside brackets so host and port stay parseable."""
    worker = _worker_for(
        _RecordingAmplifier(strict=True),
        settings=make_settings(transport=make_tcp_transport(host=host)),
    )

    response = await _list(worker)

    assert response.amplifiers[0].port == expected


@pytest.mark.asyncio
async def test_list_control_summary_follows_capabilities() -> None:
    """The list entry advertises the enumerated domains of a full board."""
    response = await _list(_worker_for(_RecordingAmplifier(strict=True)))
    controls = response.amplifiers[0].controls

    assert controls.feedback_resistor is True
    assert controls.feedback_resistor_ohm == [1_000.0, 1_000_000.0]
    assert controls.boost_gain is True
    assert controls.boost_gain_options == [1.0, 3.0, 10.0, 30.0]
    assert controls.dc_bias is True
    assert controls.dc_bias_level is True
    assert controls.bandwidth is True
    assert controls.filters is True
    assert controls.filter_types == ["low_pass", "high_pass", "band_pass"]
    assert controls.input_compensation is True
    assert controls.input_compensation_level is True
    assert controls.invert_output is True


@pytest.mark.asyncio
async def test_list_control_summary_of_an_unadvertised_board() -> None:
    """A board declaring nothing writes nothing; the boost domain keeps its fallback."""
    response = await _list(_worker_for(_RecordingAmplifier(capabilities=CurrentAmplifierCapabilities(), strict=True)))
    controls = response.amplifiers[0].controls

    assert controls.feedback_resistor is False
    assert controls.feedback_resistor_ohm == []
    assert controls.boost_gain is False
    assert controls.boost_gain_options == [1.0, 3.0, 10.0, 30.0]
    assert controls.dc_bias is False
    assert controls.dc_bias_level is False
    assert controls.bandwidth is False
    assert controls.filters is False
    assert controls.filter_types == []
    assert controls.input_compensation is False
    assert controls.input_compensation_level is False
    assert controls.invert_output is False


@pytest.mark.asyncio
async def test_list_control_summary_gates_on_the_field_each_task_writes() -> None:
    """A board writing only a level does not advertise the toggle, and vice versa."""
    tree = replace(
        _full_capabilities(),
        dc_bias=DcBiasCapabilities(enabled=_READ_ONLY, level=_READ_WRITE),
        input_compensation=CompensationCapabilities(enabled=_READ_ONLY, level=_READ_WRITE),
        filters=FilterCapabilities(
            types=(FilterType.LOW_PASS, FilterType.HIGH_PASS, FilterType.BAND_PASS),
            enabled=_READ_WRITE,
            cutoff=_READ_ONLY,
        ),
    )
    response = await _list(_worker_for(_RecordingAmplifier(capabilities=tree, strict=True)))
    controls = response.amplifiers[0].controls

    assert controls.dc_bias is False
    assert controls.dc_bias_level is True
    assert controls.input_compensation is False
    assert controls.input_compensation_level is True
    assert controls.filters is True


@pytest.mark.asyncio
async def test_status_reports_cached_values_only() -> None:
    """``amplifier:status`` renders the poll counters and never reads the board."""
    amplifier = _RecordingAmplifier(strict=True)
    worker = _worker_for(amplifier)
    worker.connected[DEVICE] = True
    worker.monitoring[DEVICE] = True
    worker.stats[DEVICE] = AmplifierStats(
        reading_count=4,
        error_count=2,
        last_state=_make_state(filters=(_FILTER,)),
        last_reading_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        last_error="bus timeout",
        consecutive_failures=1,
    )

    result = await _handler(worker).handle(_make_task(AMPLIFIER_STATUS_TASK_NAME, None), capabilities=_make_capabilities())

    assert result.status == "success"
    response = msgspec.json.decode(result.payload, type=AmplifierStatusResponse)
    assert len(response.amplifiers) == 1
    entry = response.amplifiers[0]
    assert entry.device == DEVICE
    assert entry.connected is True
    assert entry.monitoring is True
    assert entry.reading_count == 4
    assert entry.error_count == 2
    assert entry.consecutive_failures == 1
    assert entry.last_error == "bus timeout"
    assert entry.last_reading_at == "2026-01-02T03:04:05+00:00"
    assert entry.state is not None
    assert entry.state["filters"] == [{"type": "low_pass", "enabled": True, "low_cutoff_hz": 10.0, "high_cutoff_hz": 1_000.0}]
    assert amplifier.read_calls == 0


@pytest.mark.asyncio
async def test_status_without_payload_lists_boards_sorted() -> None:
    """An empty status payload reports every board, sorted by device id."""
    worker = _worker("b", "a", strict=True)

    result = await _handler(worker).handle(_make_task(AMPLIFIER_STATUS_TASK_NAME, None), capabilities=_make_capabilities())

    response = msgspec.json.decode(result.payload, type=AmplifierStatusResponse)
    assert [entry.device for entry in response.amplifiers] == ["a", "b"]
    assert all(entry.state is None for entry in response.amplifiers)
    assert all(entry.connected is False for entry in response.amplifiers)


@pytest.mark.asyncio
async def test_status_unknown_label_is_rejected() -> None:
    """An unknown status device yields UNKNOWN_AMPLIFIER and is not retried."""
    worker = _worker(DEVICE)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_STATUS_TASK_NAME, {"device": "nope"}), capabilities=_make_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "UNKNOWN_AMPLIFIER"
    assert result.retryable is False
    assert worker.amplifiers[DEVICE].read_calls == 0


@pytest.mark.asyncio
async def test_status_malformed_payload_is_rejected() -> None:
    """A status payload with an unknown field is an INVALID_PAYLOAD."""
    worker = _worker(DEVICE)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_STATUS_TASK_NAME, {"bogus": 1}), capabilities=_make_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PAYLOAD"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_read_calls_the_driver_once_and_renders_filters() -> None:
    """``amplifier:read`` performs one live read and returns filters as dicts."""
    amplifier = _RecordingAmplifier(state=_make_state(filters=(_FILTER,)))
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_READ_TASK_NAME, {"device": DEVICE}), capabilities=_make_capabilities()
    )

    assert result.status == "success"
    response = msgspec.json.decode(result.payload, type=AmplifierCommandResponse)
    assert response.device == DEVICE
    assert response.task == AMPLIFIER_READ_TASK_NAME
    assert response.applied == {}
    assert response.state is not None
    assert isinstance(response.state["filters"], list)
    assert response.state["filters"] == [
        {"type": "low_pass", "enabled": True, "low_cutoff_hz": 10.0, "high_cutoff_hz": 1_000.0}
    ]
    assert amplifier.read_calls == 1


@pytest.mark.asyncio
async def test_read_unknown_label_is_rejected() -> None:
    """An unknown read device yields UNKNOWN_AMPLIFIER without a driver read."""
    amplifier = _RecordingAmplifier()
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_READ_TASK_NAME, {"device": "nope"}), capabilities=_make_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "UNKNOWN_AMPLIFIER"
    assert result.retryable is False
    assert amplifier.read_calls == 0


@pytest.mark.asyncio
async def test_read_driver_failure_is_retryable() -> None:
    """A bus error during a read is a retryable AMPLIFIER_COMMAND_FAILED."""
    amplifier = _RecordingAmplifier(read_exc=OSError("bus down"))
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_READ_TASK_NAME, {"device": DEVICE}), capabilities=_make_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "AMPLIFIER_COMMAND_FAILED"
    assert result.retryable is True
    assert "OSError" in result.error
    assert "bus down" in result.error


_WRITE_CASES = [
    (
        AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME,
        {"device": DEVICE, "resistance_ohm": 1_000_000.0},
        "select_feedback_resistor_calls",
        1_000_000.0,
        {"resistance_ohm": 1_000_000.0},
        True,
    ),
    (
        AMPLIFIER_SET_BOOST_GAIN_TASK_NAME,
        {"device": DEVICE, "gain": 10.0},
        "set_boost_gain_calls",
        10.0,
        {"gain": 10.0},
        False,
    ),
    (
        AMPLIFIER_SET_DC_BIAS_TASK_NAME,
        {"device": DEVICE, "enabled": False},
        "set_dc_bias_calls",
        False,
        {"enabled": False},
        False,
    ),
    (
        AMPLIFIER_SET_DC_BIAS_LEVEL_TASK_NAME,
        {"device": DEVICE, "level": 0.5},
        "set_dc_bias_level_calls",
        0.5,
        {"level": 0.5},
        False,
    ),
    (
        AMPLIFIER_SET_BANDWIDTH_TASK_NAME,
        {"device": DEVICE, "bandwidth_hz": 1_000.0},
        "set_bandwidth_calls",
        1_000.0,
        {"bandwidth_hz": 1_000.0},
        False,
    ),
    (
        AMPLIFIER_SET_FILTER_TASK_NAME,
        {"device": DEVICE, "filter_type": "low_pass", "enabled": True, "low_cutoff_hz": 10.0, "high_cutoff_hz": 1_000.0},
        "set_filter_calls",
        FilterSetting(type=FilterType.LOW_PASS, enabled=True, low_cutoff_hz=10.0, high_cutoff_hz=1_000.0),
        {"filter_type": "low_pass", "enabled": True, "low_cutoff_hz": 10.0, "high_cutoff_hz": 1_000.0},
        False,
    ),
    (
        AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME,
        {"device": DEVICE, "enabled": True},
        "set_input_compensation_calls",
        True,
        {"enabled": True},
        False,
    ),
    (
        AMPLIFIER_SET_INPUT_COMPENSATION_LEVEL_TASK_NAME,
        {"device": DEVICE, "level": 0.25},
        "set_input_compensation_level_calls",
        0.25,
        {"level": 0.25},
        False,
    ),
    (
        AMPLIFIER_SET_INVERTED_TASK_NAME,
        {"device": DEVICE, "inverted": True},
        "set_inverted_calls",
        True,
        {"inverted": True},
        False,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("task", "payload", "recorder", "expected", "applied", "read_back"), _WRITE_CASES)
async def test_write_forwards_the_exact_argument(task, payload, recorder, expected, applied, read_back) -> None:
    """Every ``amplifier:set-*`` task forwards its argument unchanged and echoes it."""
    amplifier = _RecordingAmplifier()
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(_make_task(task, payload), capabilities=_make_capabilities())

    assert result.status == "success"
    response = msgspec.json.decode(result.payload, type=AmplifierCommandResponse)
    assert response.device == DEVICE
    assert response.task == task
    assert response.applied == applied
    assert response.readback_error is None
    assert getattr(amplifier, recorder) == [expected]
    assert type(getattr(amplifier, recorder)[0]) is type(expected)
    assert amplifier.read_calls == (1 if read_back else 0)
    if read_back:
        assert response.state is not None
        assert isinstance(response.state["filters"], list)
    else:
        assert response.state is None


@pytest.mark.asyncio
async def test_feedback_resistor_read_back_failure_still_reports_the_write() -> None:
    """A failed read-back after a successful write is reported as success.

    The write already reached the driver, so retrying would replay the hardware
    transition; the read failure is diagnostic only and never retryable.
    """
    amplifier = _RecordingAmplifier(read_exc=OSError("bus down"))
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME, {"device": DEVICE, "resistance_ohm": 1_000_000.0}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    assert result.error_code == ""
    assert result.retryable is False
    response = msgspec.json.decode(result.payload, type=AmplifierCommandResponse)
    assert response.applied == {"resistance_ohm": 1_000_000.0}
    assert response.state is None
    assert response.readback_error is not None
    assert "OSError" in response.readback_error
    assert "bus down" in response.readback_error
    assert amplifier.select_feedback_resistor_calls == [1_000_000.0]
    assert amplifier.read_calls == 1


@pytest.mark.asyncio
async def test_gateway_target_failed_read_back_still_reports_the_write() -> None:
    """A gateway failure on the read-back never turns a landed write into an error.

    ``GatewayTargetFailed`` on a write is ambiguous, but here the write already
    completed; only the follow-up read failed. The landed-write rule takes
    precedence, so the result stays ``success`` with ``readback_error``.
    """
    amplifier = _RecordingAmplifier(read_exc=GatewayTargetFailed("target unreachable"))
    worker = _worker_for(amplifier)
    worker.connected[DEVICE] = True

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME, {"device": DEVICE, "resistance_ohm": 1_000_000.0}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    assert result.error_code == ""
    assert result.retryable is False
    response = msgspec.json.decode(result.payload, type=AmplifierCommandResponse)
    assert response.applied == {"resistance_ohm": 1_000_000.0}
    assert response.state is None
    assert response.readback_error is not None
    assert "GatewayTargetFailed" in response.readback_error
    assert amplifier.select_feedback_resistor_calls == [1_000_000.0]
    assert amplifier.read_calls == 1
    assert worker.connected[DEVICE] is True


@pytest.mark.asyncio
async def test_relay_transition_read_back_still_reports_the_write() -> None:
    """A wrapped relay failure on the read-back cannot undo a write that landed.

    The EBIC driver reports a failed relay read-back as a
    ``RelayTransitionError``; on the write stage that type is ambiguous, but
    here the write already completed, so it stays diagnostic.
    """
    amplifier = _RecordingAmplifier(read_exc=RelayTransitionError("select feedback resistor", None, (True,), (False,)))
    worker = _worker_for(amplifier)
    worker.connected[DEVICE] = True

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME, {"device": DEVICE, "resistance_ohm": 1_000_000.0}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    assert result.error_code == ""
    assert result.retryable is False
    response = msgspec.json.decode(result.payload, type=AmplifierCommandResponse)
    assert response.applied == {"resistance_ohm": 1_000_000.0}
    assert response.state is None
    assert response.readback_error is not None
    assert "RelayTransitionError" in response.readback_error
    assert worker.connected[DEVICE] is True


def test_gateway_failures_are_sibling_modbus_errors() -> None:
    """``GatewayBusy`` and ``GatewayTargetFailed`` are distinct Modbus failures.

    Both are classified with ``isinstance``, so neither may subclass the other:
    a busy gateway is retryable, while a gateway that cannot reach its target
    marks the board unhealthy. Both must stay catchable as
    ``ModbusOperationError``.
    """
    assert issubclass(GatewayBusy, ModbusOperationError)
    assert issubclass(GatewayTargetFailed, ModbusOperationError)
    assert not issubclass(GatewayBusy, GatewayTargetFailed)
    assert not issubclass(GatewayTargetFailed, GatewayBusy)


_BUS_FAILURE_CASES = [
    ("write", GatewayBusy("gateway busy"), True, False),
    ("read", GatewayBusy("gateway busy"), True, False),
    ("write", GatewayTargetFailed("target unreachable"), False, True),
    ("read", GatewayTargetFailed("target unreachable"), True, True),
    ("write", ModbusOperationError("crc error"), False, True),
    ("read", ModbusOperationError("crc error"), True, True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "exc", "retryable", "unhealthy"), _BUS_FAILURE_CASES)
async def test_bus_failure_taxonomy(stage, exc, retryable, unhealthy) -> None:
    """Bus failures map to the documented retry verdict and board-health mark.

    A busy bus never executed the request, so it stays healthy and retryable.
    Gateway reach and generic Modbus failures prove the board unreachable. A
    failed write is terminal because the request may already have landed, and
    is reported as an unknown outcome; a failed read is safe to retry.

    The EBIC driver raises these exceptions directly from ``read_relays``, so a
    raw instance is the shape a read produces. On a write the driver wraps the
    cause; `test_wrapped_write_failure_is_classified_by_cause` covers that path.
    """
    amplifier = _RecordingAmplifier(control_exc=exc) if stage == "write" else _RecordingAmplifier(read_exc=exc)
    worker = _worker_for(amplifier)
    worker.connected[DEVICE] = True
    task, payload = (
        (AMPLIFIER_SET_DC_BIAS_TASK_NAME, {"device": DEVICE, "enabled": True})
        if stage == "write"
        else (AMPLIFIER_READ_TASK_NAME, {"device": DEVICE})
    )
    ambiguous_write = stage == "write" and not retryable

    result = await _handler(worker).handle(_make_task(task, payload), capabilities=_make_capabilities())

    assert result.status == "error"
    assert result.error_code == (AMPLIFIER_WRITE_OUTCOME_UNKNOWN if ambiguous_write else "AMPLIFIER_COMMAND_FAILED")
    assert result.retryable is retryable
    assert worker.connected[DEVICE] is (not unhealthy)
    assert type(exc).__name__ in result.error

    if ambiguous_write:
        detail = msgspec.json.decode(result.payload, type=AmplifierCommandError)
        assert detail.device == DEVICE
        assert detail.task == task
        assert detail.outcome_unknown is True
    else:
        assert result.payload == b""


_WRAPPED_WRITE_CASES = [
    (GatewayBusy("gateway busy"), True, False),
    (GatewayTargetFailed("target unreachable"), False, True),
    (ModbusOperationError("crc error"), False, True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("cause", "retryable", "unhealthy"), _WRAPPED_WRITE_CASES)
async def test_wrapped_write_failure_is_classified_by_cause(cause, retryable, unhealthy) -> None:
    """A write failure is classified through the driver's wrapper, not by its type.

    The EBIC semantic layer raises ``RelayTransitionError`` from the underlying
    Modbus failure, so the top-level type alone never proves anything about the
    bus. A ``GatewayBusy`` cause keeps the write retryable; every other cause is
    an ambiguous write and must not be replayed.
    """
    exc = RelayTransitionError("set dc bias", 3, (True,), None)
    amplifier = _RecordingAmplifier(control_exc=exc, control_cause=cause)
    worker = _worker_for(amplifier)
    worker.connected[DEVICE] = True

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_DC_BIAS_TASK_NAME, {"device": DEVICE, "enabled": True}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.retryable is retryable
    assert worker.connected[DEVICE] is (not unhealthy)
    assert "RelayTransitionError" in result.error
    if retryable:
        assert result.error_code == "AMPLIFIER_COMMAND_FAILED"
        assert result.payload == b""
    else:
        assert result.error_code == AMPLIFIER_WRITE_OUTCOME_UNKNOWN
        detail = msgspec.json.decode(result.payload, type=AmplifierCommandError)
        assert detail.outcome_unknown is True


_READ_WRAPPED_CASES = [
    (GatewayBusy("gateway busy"), False),
    (GatewayTargetFailed("target unreachable"), True),
    (ModbusOperationError("crc error"), True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("cause", "unhealthy"), _READ_WRAPPED_CASES)
async def test_read_wrapped_failure_keeps_the_read_verdict(cause, unhealthy) -> None:
    """A wrapped read failure keeps the read rows: retryable, health from the cause.

    Reads are never ambiguous, so the chain walk changes only the board-health
    verdict: a busy gateway still proves the board is answering, while a reach
    or bus failure marks it unhealthy.
    """
    exc = RelayTransitionError("read relays", None, (True,), (False,))
    amplifier = _RecordingAmplifier(read_exc=exc, read_cause=cause)
    worker = _worker_for(amplifier)
    worker.connected[DEVICE] = True

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_READ_TASK_NAME, {"device": DEVICE}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "AMPLIFIER_COMMAND_FAILED"
    assert result.retryable is True
    assert result.payload == b""
    assert worker.connected[DEVICE] is (not unhealthy)


_GATE_CASES = [
    (
        AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME,
        {"device": DEVICE, "resistance_ohm": 1_000_000.0},
        "can_select_preamp",
        "select_feedback_resistor_calls",
    ),
    (AMPLIFIER_SET_BOOST_GAIN_TASK_NAME, {"device": DEVICE, "gain": 10.0}, "can_set_boost", "set_boost_gain_calls"),
    (
        AMPLIFIER_SET_DC_BIAS_TASK_NAME,
        {"device": DEVICE, "enabled": True},
        "capabilities.dc_bias.enabled.write",
        "set_dc_bias_calls",
    ),
    (
        AMPLIFIER_SET_DC_BIAS_LEVEL_TASK_NAME,
        {"device": DEVICE, "level": 0.5},
        "capabilities.dc_bias.level.write",
        "set_dc_bias_level_calls",
    ),
    (
        AMPLIFIER_SET_BANDWIDTH_TASK_NAME,
        {"device": DEVICE, "bandwidth_hz": 1_000.0},
        "can_set_bandwidth",
        "set_bandwidth_calls",
    ),
    (
        AMPLIFIER_SET_FILTER_TASK_NAME,
        {"device": DEVICE, "filter_type": "low_pass", "enabled": True},
        "capabilities.filters.enabled.write",
        "set_filter_calls",
    ),
    (
        AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME,
        {"device": DEVICE, "enabled": True},
        "capabilities.input_compensation.enabled.write",
        "set_input_compensation_calls",
    ),
    (
        AMPLIFIER_SET_INPUT_COMPENSATION_LEVEL_TASK_NAME,
        {"device": DEVICE, "level": 0.25},
        "capabilities.input_compensation.level.write",
        "set_input_compensation_level_calls",
    ),
    (AMPLIFIER_SET_INVERTED_TASK_NAME, {"device": DEVICE, "inverted": True}, "can_set_invert_output", "set_inverted_calls"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("task", "payload", "gate", "recorder"), _GATE_CASES)
async def test_write_without_capability_is_rejected(task, payload, gate, recorder) -> None:
    """A read-only capability gate answers UNSUPPORTED before the driver is reached."""
    amplifier = _RecordingAmplifier(capabilities=_read_only(gate))
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(_make_task(task, payload), capabilities=_make_capabilities())

    assert result.status == "error"
    assert result.error_code == "UNSUPPORTED"
    assert result.retryable is False
    assert getattr(amplifier, recorder) == []
    assert amplifier.read_calls == 0


@pytest.mark.asyncio
async def test_dc_bias_toggle_requires_enabled_write_not_level_write() -> None:
    """A board that can only write the level cannot toggle DC bias injection."""
    amplifier = _RecordingAmplifier(capabilities=_read_only("capabilities.dc_bias.enabled.write"))
    worker = _worker_for(amplifier)
    assert amplifier.can_set_dc_bias is True, "the OR property alone would let the stub be dispatched"

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_DC_BIAS_TASK_NAME, {"device": DEVICE, "enabled": True}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "UNSUPPORTED"
    assert result.retryable is False
    assert amplifier.set_dc_bias_calls == []
    assert amplifier.read_calls == 0


@pytest.mark.asyncio
async def test_input_compensation_toggle_requires_enabled_write_not_level_write() -> None:
    """A board that can only write the level cannot switch compensation."""
    amplifier = _RecordingAmplifier(capabilities=_read_only("capabilities.input_compensation.enabled.write"))
    worker = _worker_for(amplifier)
    assert amplifier.can_set_input_compensation is True, "the OR property alone would let the stub be dispatched"

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME, {"device": DEVICE, "enabled": True}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "UNSUPPORTED"
    assert result.retryable is False
    assert amplifier.set_input_compensation_calls == []
    assert amplifier.read_calls == 0


@pytest.mark.asyncio
async def test_filter_toggle_needs_only_enabled_write() -> None:
    """A filter toggle without cutoffs is allowed on a cutoff-read-only board."""
    amplifier = _RecordingAmplifier(capabilities=_read_only("capabilities.filters.cutoff.write"))
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_FILTER_TASK_NAME, {"device": DEVICE, "filter_type": "low_pass", "enabled": True}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    assert amplifier.set_filter_calls == [
        FilterSetting(type=FilterType.LOW_PASS, enabled=True, low_cutoff_hz=None, high_cutoff_hz=None)
    ]


@pytest.mark.asyncio
async def test_filter_with_cutoffs_requires_cutoff_write() -> None:
    """A filter payload that supplies cutoffs is gated on cutoff write access."""
    amplifier = _RecordingAmplifier(capabilities=_read_only("capabilities.filters.cutoff.write"))
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(
            AMPLIFIER_SET_FILTER_TASK_NAME,
            {"device": DEVICE, "filter_type": "low_pass", "enabled": True, "low_cutoff_hz": 10.0},
        ),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "UNSUPPORTED"
    assert result.retryable is False
    assert amplifier.set_filter_calls == []
    assert amplifier.read_calls == 0


@pytest.mark.asyncio
async def test_feedback_resistor_out_of_domain_is_rejected() -> None:
    """A resistance outside the enumerated options is INVALID_PARAMETERS."""
    amplifier = _RecordingAmplifier()
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME, {"device": DEVICE, "resistance_ohm": 500.0}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMETERS"
    assert result.retryable is False
    assert "1000.0" in result.error
    assert "1000000.0" in result.error
    assert "500.0" in result.error
    assert amplifier.select_feedback_resistor_calls == []
    assert amplifier.read_calls == 0


@pytest.mark.asyncio
async def test_boost_gain_out_of_domain_is_rejected() -> None:
    """A gain outside the enumerated options is INVALID_PARAMETERS."""
    amplifier = _RecordingAmplifier()
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_BOOST_GAIN_TASK_NAME, {"device": DEVICE, "gain": 2.0}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMETERS"
    assert "30.0" in result.error
    assert "2.0" in result.error
    assert amplifier.set_boost_gain_calls == []


@pytest.mark.asyncio
async def test_unknown_filter_type_is_rejected() -> None:
    """A filter spelling outside FilterType is INVALID_PARAMETERS."""
    amplifier = _RecordingAmplifier()
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_FILTER_TASK_NAME, {"device": DEVICE, "filter_type": "notch"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMETERS"
    assert "low_pass" in result.error
    assert "notch" in result.error
    assert amplifier.set_filter_calls == []


@pytest.mark.asyncio
async def test_filter_type_outside_the_board_domain_is_rejected() -> None:
    """A valid FilterType the board does not expose is INVALID_PARAMETERS."""
    tree = replace(
        _full_capabilities(),
        filters=FilterCapabilities(types=(FilterType.LOW_PASS,), enabled=_READ_WRITE, cutoff=_READ_WRITE),
    )
    amplifier = _RecordingAmplifier(capabilities=tree)
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_FILTER_TASK_NAME, {"device": DEVICE, "filter_type": "high_pass"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMETERS"
    assert "high_pass" in result.error
    assert amplifier.set_filter_calls == []


_TYPE_ERROR_CASES = [
    (AMPLIFIER_SET_DC_BIAS_TASK_NAME, {"device": DEVICE, "enabled": "yes"}),
    (AMPLIFIER_SET_BOOST_GAIN_TASK_NAME, {"device": DEVICE, "gain": "high"}),
    (AMPLIFIER_READ_TASK_NAME, {"device": 5}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("task", "payload"), _TYPE_ERROR_CASES)
async def test_wrong_argument_types_are_invalid_payload(task, payload) -> None:
    """A payload whose field types do not match the struct is INVALID_PAYLOAD."""
    amplifier = _RecordingAmplifier()
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(_make_task(task, payload), capabilities=_make_capabilities())

    assert result.status == "error"
    assert result.error_code == "INVALID_PAYLOAD"
    assert result.retryable is False
    assert amplifier.read_calls == 0
    assert all(calls == [] for calls in _all_control_calls(amplifier))


_MALFORMED_CASES = [
    (AMPLIFIER_READ_TASK_NAME, b""),
    (AMPLIFIER_READ_TASK_NAME, b"[]"),
    (AMPLIFIER_READ_TASK_NAME, b"null"),
    (AMPLIFIER_READ_TASK_NAME, b"{}"),
    (AMPLIFIER_SET_DC_BIAS_TASK_NAME, b'{"device": "a", "bogus": 1}'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("task", "raw"), _MALFORMED_CASES)
async def test_malformed_payload_is_invalid_payload(task, raw) -> None:
    """A non-object, a missing device or an unknown field is INVALID_PAYLOAD."""
    amplifier = _RecordingAmplifier()
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(_raw_task(task, raw), capabilities=_make_capabilities())

    assert result.status == "error"
    assert result.error_code == "INVALID_PAYLOAD"
    assert result.retryable is False
    assert amplifier.read_calls == 0
    assert all(calls == [] for calls in _all_control_calls(amplifier))


_MISSING_FIELD_CASES = [
    (AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME, "resistance_ohm"),
    (AMPLIFIER_SET_BOOST_GAIN_TASK_NAME, "gain"),
    (AMPLIFIER_SET_DC_BIAS_TASK_NAME, "enabled"),
    (AMPLIFIER_SET_DC_BIAS_LEVEL_TASK_NAME, "level"),
    (AMPLIFIER_SET_BANDWIDTH_TASK_NAME, "bandwidth_hz"),
    (AMPLIFIER_SET_FILTER_TASK_NAME, "filter_type"),
    (AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME, "enabled"),
    (AMPLIFIER_SET_INPUT_COMPENSATION_LEVEL_TASK_NAME, "level"),
    (AMPLIFIER_SET_INVERTED_TASK_NAME, "inverted"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("task", "field"), _MISSING_FIELD_CASES)
async def test_missing_required_write_field_is_invalid_parameters(task, field) -> None:
    """A write payload missing its value is rejected before the driver is called."""
    amplifier = _RecordingAmplifier()
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(_make_task(task, {"device": DEVICE}), capabilities=_make_capabilities())

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMETERS"
    assert result.retryable is False
    assert field in result.error
    assert amplifier.read_calls == 0
    assert all(calls == [] for calls in _all_control_calls(amplifier))


_WRITE_EXCEPTION_CASES = [
    # Raised before any bus traffic: the write outcome is known.
    (NotImplementedError("stub"), "UNSUPPORTED", False, False),
    (ValueError("out of range"), "INVALID_PARAMETERS", False, False),
    # Raised during or after the write: the request may have reached the board.
    (OSError("bus down"), AMPLIFIER_WRITE_OUTCOME_UNKNOWN, False, True),
    (UnexpectedResponse("gain", "bad frame"), AMPLIFIER_WRITE_OUTCOME_UNKNOWN, False, True),
    (RelayTransitionError("set dc bias", 3, (True, False), None), AMPLIFIER_WRITE_OUTCOME_UNKNOWN, False, True),
    (RuntimeError("unexpected"), AMPLIFIER_WRITE_OUTCOME_UNKNOWN, False, True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "error_code", "retryable", "unhealthy"), _WRITE_EXCEPTION_CASES)
async def test_write_exception_mapping(exc, error_code, retryable, unhealthy) -> None:
    """A write exception is terminal unless it proves the request was not executed.

    A ``NotImplementedError`` and a ``ValueError`` are raised before any bus
    traffic. Every other write failure — including one outside the Modbus
    taxonomy — may have landed, so it is non-retryable, reports an unknown
    outcome and clears the board's connected flag.
    """
    amplifier = _RecordingAmplifier(control_exc=exc)
    worker = _worker_for(amplifier)
    worker.connected[DEVICE] = True

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_DC_BIAS_TASK_NAME, {"device": DEVICE, "enabled": True}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == error_code
    assert result.retryable is retryable
    assert worker.connected[DEVICE] is (not unhealthy)
    assert amplifier.set_dc_bias_calls == [True]
    if error_code == AMPLIFIER_WRITE_OUTCOME_UNKNOWN:
        detail = msgspec.json.decode(result.payload, type=AmplifierCommandError)
        assert detail.outcome_unknown is True
        assert "(write outcome unknown; do not retry)" in result.error
    else:
        assert result.payload == b""
