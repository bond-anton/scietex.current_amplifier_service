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
    make_amplifier_settings,
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
from scietex.hal.current_amplifier.scietex.ebic.modbus.v1.semantic import RelayTransitionError
from scietex.service.task_handler import TaskData

from scietex.current_amplifier_service.amplifier_worker import AmplifierStats
from scietex.current_amplifier_service.config import AmplifierSettings
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
    AmplifierCommandResponse,
    AmplifierControlHandler,
    AmplifierListResponse,
    AmplifierStatusResponse,
)

if TYPE_CHECKING:
    from scietex.current_amplifier_service.amplifier_worker import CurrentAmplifierWorker

LABEL = "a"

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
    instance. ``control_exc`` makes every setter raise after recording the call;
    ``read_exc`` makes ``read_data`` raise. ``strict`` makes any I/O raise, so a
    registry-only task proves it never touched the driver.
    """

    capabilities = _full_capabilities()

    def __init__(
        self,
        *,
        capabilities: CurrentAmplifierCapabilities | None = None,
        state: AmplifierState | None = None,
        control_exc: Exception | None = None,
        read_exc: Exception | None = None,
        strict: bool = False,
    ) -> None:
        if capabilities is not None:
            self.capabilities = capabilities
        self.state = _make_state() if state is None else state
        self.control_exc = control_exc
        self.read_exc = read_exc
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

    def __init__(self) -> None:
        self.amplifiers: dict[str, _RecordingAmplifier] = {}
        self.amplifier_settings: dict[str, AmplifierSettings] = {}
        self.monitoring: dict[str, bool] = {}
        self.connected: dict[str, bool] = {}
        self.stats: dict[str, AmplifierStats] = {}
        self.service_name = "test"
        self.instance_id = "inst"


def _worker_for(amplifier: _RecordingAmplifier, label: str = LABEL) -> _StubWorker:
    """A stub worker holding one board."""
    worker = _StubWorker()
    worker.amplifiers[label] = amplifier
    worker.amplifier_settings[label] = make_amplifier_settings(label=label)
    worker.monitoring[label] = False
    worker.connected[label] = False
    worker.stats[label] = AmplifierStats()
    return worker


def _worker(*labels: str, strict: bool = False) -> _StubWorker:
    """A stub worker holding one full-capability board per label.

    Registry insertion keeps the argument order; address and port follow the
    sorted label order so the list expectation is order-independent.
    """
    worker = _StubWorker()
    position = {label: index for index, label in enumerate(sorted(labels), start=1)}
    for label in labels:
        index = position[label]
        amplifier = _RecordingAmplifier(strict=strict)
        worker.amplifiers[label] = amplifier
        worker.amplifier_settings[label] = make_amplifier_settings(label=label, port=f"/dev/ttyUSB{index - 1}", address=index)
        worker.monitoring[label] = False
        worker.connected[label] = False
        worker.stats[label] = AmplifierStats()
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
    assert [entry.label for entry in response.amplifiers] == ["a", "b"]
    by_label = {entry.label: entry for entry in response.amplifiers}
    assert by_label["a"].driver == FAKE_DRIVER_NAME
    assert by_label["a"].address == 1
    assert by_label["a"].port == "/dev/ttyUSB0"
    assert by_label["b"].address == 2
    assert by_label["b"].port == "/dev/ttyUSB1"
    for amplifier in worker.amplifiers.values():
        assert amplifier.read_calls == 0
        assert amplifier.connect_calls == 0
        assert amplifier.close_calls == 0
        assert all(calls == [] for calls in _all_control_calls(amplifier))


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
    worker.connected[LABEL] = True
    worker.monitoring[LABEL] = True
    worker.stats[LABEL] = AmplifierStats(
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
    assert entry.label == LABEL
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
    """An empty status payload reports every board, sorted by label."""
    worker = _worker("b", "a", strict=True)

    result = await _handler(worker).handle(_make_task(AMPLIFIER_STATUS_TASK_NAME, None), capabilities=_make_capabilities())

    response = msgspec.json.decode(result.payload, type=AmplifierStatusResponse)
    assert [entry.label for entry in response.amplifiers] == ["a", "b"]
    assert all(entry.state is None for entry in response.amplifiers)
    assert all(entry.connected is False for entry in response.amplifiers)


@pytest.mark.asyncio
async def test_status_unknown_label_is_rejected() -> None:
    """An unknown status label yields UNKNOWN_AMPLIFIER and is not retried."""
    worker = _worker(LABEL)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_STATUS_TASK_NAME, {"label": "nope"}), capabilities=_make_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "UNKNOWN_AMPLIFIER"
    assert result.retryable is False
    assert worker.amplifiers[LABEL].read_calls == 0


@pytest.mark.asyncio
async def test_status_malformed_payload_is_rejected() -> None:
    """A status payload with an unknown field is an INVALID_PAYLOAD."""
    worker = _worker(LABEL)

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
        _make_task(AMPLIFIER_READ_TASK_NAME, {"label": LABEL}), capabilities=_make_capabilities()
    )

    assert result.status == "success"
    response = msgspec.json.decode(result.payload, type=AmplifierCommandResponse)
    assert response.label == LABEL
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
    """An unknown read label yields UNKNOWN_AMPLIFIER without a driver read."""
    amplifier = _RecordingAmplifier()
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_READ_TASK_NAME, {"label": "nope"}), capabilities=_make_capabilities()
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
        _make_task(AMPLIFIER_READ_TASK_NAME, {"label": LABEL}), capabilities=_make_capabilities()
    )

    assert result.status == "error"
    assert result.error_code == "AMPLIFIER_COMMAND_FAILED"
    assert result.retryable is True
    assert "OSError" in result.error
    assert "bus down" in result.error


_WRITE_CASES = [
    (
        AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME,
        {"label": LABEL, "resistance_ohm": 1_000_000.0},
        "select_feedback_resistor_calls",
        1_000_000.0,
        {"resistance_ohm": 1_000_000.0},
        True,
    ),
    (
        AMPLIFIER_SET_BOOST_GAIN_TASK_NAME,
        {"label": LABEL, "gain": 10.0},
        "set_boost_gain_calls",
        10.0,
        {"gain": 10.0},
        False,
    ),
    (
        AMPLIFIER_SET_DC_BIAS_TASK_NAME,
        {"label": LABEL, "enabled": False},
        "set_dc_bias_calls",
        False,
        {"enabled": False},
        False,
    ),
    (
        AMPLIFIER_SET_DC_BIAS_LEVEL_TASK_NAME,
        {"label": LABEL, "level": 0.5},
        "set_dc_bias_level_calls",
        0.5,
        {"level": 0.5},
        False,
    ),
    (
        AMPLIFIER_SET_BANDWIDTH_TASK_NAME,
        {"label": LABEL, "bandwidth_hz": 1_000.0},
        "set_bandwidth_calls",
        1_000.0,
        {"bandwidth_hz": 1_000.0},
        False,
    ),
    (
        AMPLIFIER_SET_FILTER_TASK_NAME,
        {"label": LABEL, "filter_type": "low_pass", "enabled": True, "low_cutoff_hz": 10.0, "high_cutoff_hz": 1_000.0},
        "set_filter_calls",
        FilterSetting(type=FilterType.LOW_PASS, enabled=True, low_cutoff_hz=10.0, high_cutoff_hz=1_000.0),
        {"filter_type": "low_pass", "enabled": True, "low_cutoff_hz": 10.0, "high_cutoff_hz": 1_000.0},
        False,
    ),
    (
        AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME,
        {"label": LABEL, "enabled": True},
        "set_input_compensation_calls",
        True,
        {"enabled": True},
        False,
    ),
    (
        AMPLIFIER_SET_INPUT_COMPENSATION_LEVEL_TASK_NAME,
        {"label": LABEL, "level": 0.25},
        "set_input_compensation_level_calls",
        0.25,
        {"level": 0.25},
        False,
    ),
    (
        AMPLIFIER_SET_INVERTED_TASK_NAME,
        {"label": LABEL, "inverted": True},
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
    assert response.label == LABEL
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
        _make_task(AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME, {"label": LABEL, "resistance_ohm": 1_000_000.0}),
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


_GATE_CASES = [
    (
        AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME,
        {"label": LABEL, "resistance_ohm": 1_000_000.0},
        "can_select_preamp",
        "select_feedback_resistor_calls",
    ),
    (AMPLIFIER_SET_BOOST_GAIN_TASK_NAME, {"label": LABEL, "gain": 10.0}, "can_set_boost", "set_boost_gain_calls"),
    (
        AMPLIFIER_SET_DC_BIAS_TASK_NAME,
        {"label": LABEL, "enabled": True},
        "capabilities.dc_bias.enabled.write",
        "set_dc_bias_calls",
    ),
    (
        AMPLIFIER_SET_DC_BIAS_LEVEL_TASK_NAME,
        {"label": LABEL, "level": 0.5},
        "capabilities.dc_bias.level.write",
        "set_dc_bias_level_calls",
    ),
    (AMPLIFIER_SET_BANDWIDTH_TASK_NAME, {"label": LABEL, "bandwidth_hz": 1_000.0}, "can_set_bandwidth", "set_bandwidth_calls"),
    (
        AMPLIFIER_SET_FILTER_TASK_NAME,
        {"label": LABEL, "filter_type": "low_pass", "enabled": True},
        "capabilities.filters.enabled.write",
        "set_filter_calls",
    ),
    (
        AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME,
        {"label": LABEL, "enabled": True},
        "capabilities.input_compensation.enabled.write",
        "set_input_compensation_calls",
    ),
    (
        AMPLIFIER_SET_INPUT_COMPENSATION_LEVEL_TASK_NAME,
        {"label": LABEL, "level": 0.25},
        "capabilities.input_compensation.level.write",
        "set_input_compensation_level_calls",
    ),
    (AMPLIFIER_SET_INVERTED_TASK_NAME, {"label": LABEL, "inverted": True}, "can_set_invert_output", "set_inverted_calls"),
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
        _make_task(AMPLIFIER_SET_DC_BIAS_TASK_NAME, {"label": LABEL, "enabled": True}),
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
        _make_task(AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME, {"label": LABEL, "enabled": True}),
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
        _make_task(AMPLIFIER_SET_FILTER_TASK_NAME, {"label": LABEL, "filter_type": "low_pass", "enabled": True}),
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
            {"label": LABEL, "filter_type": "low_pass", "enabled": True, "low_cutoff_hz": 10.0},
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
        _make_task(AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME, {"label": LABEL, "resistance_ohm": 500.0}),
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
        _make_task(AMPLIFIER_SET_BOOST_GAIN_TASK_NAME, {"label": LABEL, "gain": 2.0}),
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
        _make_task(AMPLIFIER_SET_FILTER_TASK_NAME, {"label": LABEL, "filter_type": "notch"}),
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
        _make_task(AMPLIFIER_SET_FILTER_TASK_NAME, {"label": LABEL, "filter_type": "high_pass"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMETERS"
    assert "high_pass" in result.error
    assert amplifier.set_filter_calls == []


_TYPE_ERROR_CASES = [
    (AMPLIFIER_SET_DC_BIAS_TASK_NAME, {"label": LABEL, "enabled": "yes"}),
    (AMPLIFIER_SET_BOOST_GAIN_TASK_NAME, {"label": LABEL, "gain": "high"}),
    (AMPLIFIER_READ_TASK_NAME, {"label": 5}),
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
    (AMPLIFIER_SET_DC_BIAS_TASK_NAME, b'{"label": "a", "bogus": 1}'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("task", "raw"), _MALFORMED_CASES)
async def test_malformed_payload_is_invalid_payload(task, raw) -> None:
    """A non-object, a missing label or an unknown field is INVALID_PAYLOAD."""
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

    result = await _handler(worker).handle(_make_task(task, {"label": LABEL}), capabilities=_make_capabilities())

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMETERS"
    assert result.retryable is False
    assert field in result.error
    assert amplifier.read_calls == 0
    assert all(calls == [] for calls in _all_control_calls(amplifier))


_EXCEPTION_CASES = [
    (NotImplementedError("stub"), "UNSUPPORTED", False),
    (ValueError("out of range"), "INVALID_PARAMETERS", False),
    (OSError("bus down"), "AMPLIFIER_COMMAND_FAILED", True),
    (UnexpectedResponse("gain", "bad frame"), "AMPLIFIER_COMMAND_FAILED", True),
    (RelayTransitionError("set dc bias", 3, (True, False), None), "AMPLIFIER_COMMAND_FAILED", True),
    (RuntimeError("unexpected"), "AMPLIFIER_COMMAND_FAILED", True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "error_code", "retryable"), _EXCEPTION_CASES)
async def test_driver_exception_mapping(exc, error_code, retryable) -> None:
    """Driver exceptions are classified by type into the task error taxonomy."""
    amplifier = _RecordingAmplifier(control_exc=exc)
    worker = _worker_for(amplifier)

    result = await _handler(worker).handle(
        _make_task(AMPLIFIER_SET_DC_BIAS_TASK_NAME, {"label": LABEL, "enabled": True}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == error_code
    assert result.retryable is retryable
    assert amplifier.set_dc_bias_calls == [True]
    if error_code == "AMPLIFIER_COMMAND_FAILED":
        assert type(exc).__name__ in result.error
