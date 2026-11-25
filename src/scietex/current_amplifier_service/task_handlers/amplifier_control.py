"""``current_amplifier:*`` data-plane tasks: read and control a single board.

The task names and payload spellings are the api156 plugin's
(``CURRENT_AMPLIFIER_TASKS`` / ``CURRENT_AMPLIFIER_COMMAND_FIELDS``), so a
plugin-shaped command body decodes here unchanged. Matching is exact: the
former ``amplifier:*`` namespace is gone, not aliased.

Each task targets one device by the worker device id. Write tasks are gated on
the board's declared capability set before dispatch: an unsupported command is
answered with ``UNSUPPORTED`` instead of reaching a driver method that may fall
back to the protocol's ``NotImplementedError`` stub. Several EBIC v1.0 writes are
exactly that kind of stub while the capability tree still advertises the field,
so the capability check is the gate and a driver ``NotImplementedError`` is only
a second line of defence.

Boost-gain safety: switching gain engages the new relay before releasing the old
one, so a ``10x <-> 30x`` change passes through about ``39x`` for the relay
settle window. Never change gain during a measurement.

``current_amplifier:list`` and ``current_amplifier:status`` answer from the
worker's registry and poll counters only, and
``current_amplifier:select-preamp-range`` is refused before dispatch; none of
the three touches the bus.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

import msgspec
from scietex.hal.current_amplifier.base.capabilities import FilterType
from scietex.hal.current_amplifier.base.data import FilterSetting
from scietex.hal.current_amplifier.scietex.ebic.modbus.v1 import GatewayBusy
from scietex.hal.serial import ModbusOperationError
from scietex.service.task_handler import (
    TaskCapabilities,
    TaskData,
    TaskHandler,
    TaskHandlerContext,
    TaskResult,
)

if TYPE_CHECKING:
    from scietex.hal.current_amplifier.base.data import AmplifierState
    from scietex.hal.current_amplifier.base.protocol import CurrentAmplifierProtocol

    from ..amplifier_worker import CurrentAmplifierWorker
    from ..config import AmplifierTransportSettings

#: Task name that lists the configured boards and their control surface.
AMPLIFIER_LIST_TASK_NAME = "current_amplifier:list"

#: Task name that reports cached poll status for one board or every board.
AMPLIFIER_STATUS_TASK_NAME = "current_amplifier:status"

#: Task name that reads the live state of one board.
AMPLIFIER_READ_TASK_NAME = "current_amplifier:read"

#: Task name that selects the preamp range. Registered so the plugin's verb has
#: an owner; always answers ``UNSUPPORTED`` because the HAL has no such method.
AMPLIFIER_SELECT_PREAMP_RANGE_TASK_NAME = "current_amplifier:select-preamp-range"

#: Task name that selects the feedback (transimpedance) resistor.
AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME = "current_amplifier:select-feedback-resistor"

#: Task name that selects the boost stage gain.
AMPLIFIER_SET_BOOST_GAIN_TASK_NAME = "current_amplifier:set-boost-gain"

#: Task name that switches DC bias injection on or off.
AMPLIFIER_SET_DC_BIAS_TASK_NAME = "current_amplifier:set-dc-bias"

#: Task name that writes the DC bias level.
AMPLIFIER_SET_DC_BIAS_LEVEL_TASK_NAME = "current_amplifier:set-dc-bias-level"

#: Task name that writes the bandwidth.
AMPLIFIER_SET_BANDWIDTH_TASK_NAME = "current_amplifier:set-bandwidth"

#: Task name that writes one filter entry.
AMPLIFIER_SET_FILTER_TASK_NAME = "current_amplifier:set-filter"

#: Task name that switches input-capacitance compensation on or off.
AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME = "current_amplifier:set-input-compensation"

#: Task name that writes the input-compensation level.
AMPLIFIER_SET_INPUT_COMPENSATION_LEVEL_TASK_NAME = "current_amplifier:set-input-compensation-level"

#: Task name that switches output-polarity inversion on or off.
AMPLIFIER_SET_INVERTED_TASK_NAME = "current_amplifier:set-inverted"

#: Gain domain of the EBIC boost stage, used when a driver declares write access
#: without enumerating its options. The eight-resistor preamp is enumerated on
#: every real board, so only the boost stage needs a fallback.
DEFAULT_BOOST_GAINS: frozenset[float] = frozenset({1.0, 3.0, 10.0, 30.0})

#: Error code for a write that failed after its request may have left the host.
#:
#: The framework drops a failed task's payload, so a consumer cannot see
#: ``AmplifierCommandError.outcome_unknown``; this code is the durable signal to
#: branch on instead. Replaying such a write could repeat a hardware transition.
AMPLIFIER_WRITE_OUTCOME_UNKNOWN: str = "AMPLIFIER_WRITE_OUTCOME_UNKNOWN"

#: Why ``current_amplifier:select-preamp-range`` always answers ``UNSUPPORTED``.
#:
#: The HAL exposes ``select_feedback_resistor`` only. A preamp *range* is a
#: distinct volt-domain selector with no HAL method, and inventing a
#: range-to-resistance mapping would guess at board semantics. A future HAL
#: release can add the method and this task becomes one dispatch row.
PREAMP_RANGE_UNSUPPORTED_REASON: str = (
    "the HAL exposes select_feedback_resistor only; a preamp range is a volt-domain "
    "selector with no HAL method, so no range-to-resistance mapping is defined"
)

#: Bound on the cause/context walk. A longer chain is pathological, and a
#: hand-built exception may even point at itself, so the walk must terminate.
_MAX_EXCEPTION_CHAIN: int = 8


class AmplifierStatusRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Request payload for ``current_amplifier:status``.

    Args:
        device: Worker device id to report; ``None`` (or an empty payload)
            reports every configured device.
    """

    device: str | None = None


class AmplifierCommandRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Request payload for ``current_amplifier:read`` and the write tasks.

    Field-for-field the plugin's ``CurrentAmplifierCommandRequest`` minus
    ``task``, which the plugin keeps in the task envelope. Only the fields
    relevant to the task are read; a field the task needs but the payload omits
    is rejected as ``INVALID_PARAMETERS``. One struct keeps the wire surface
    flat — the task name already says which fields apply — and every verb has
    its own spelling, because the plugin spells the argument per verb.

    Args:
        device: Worker device id to target (required).
        preamp_range: Preamp range for ``current_amplifier:select-preamp-range``.
        feedback_resistor_ohm: Feedback resistor for
            ``current_amplifier:select-feedback-resistor``.
        boost_gain: Boost gain for ``current_amplifier:set-boost-gain``.
        dc_bias_enabled: Switch state for ``current_amplifier:set-dc-bias``.
        dc_bias_level: Device-native level for
            ``current_amplifier:set-dc-bias-level``.
        bandwidth_hz: Bandwidth for ``current_amplifier:set-bandwidth``, in Hz.
        filter_type: Filter response for ``current_amplifier:set-filter``,
            spelled as a `FilterType` value (``low_pass``, ``high_pass``,
            ``band_pass``).
        filter_enabled: Switch state of the filter entry.
        filter_low_cutoff_hz: Lower filter corner, in Hz.
        filter_high_cutoff_hz: Upper filter corner, in Hz.
        input_compensation_enabled: Switch state for
            ``current_amplifier:set-input-compensation``.
        input_compensation_level: Device-native level for
            ``current_amplifier:set-input-compensation-level``.
        inverted: Output-polarity state for ``current_amplifier:set-inverted``.
    """

    device: str
    preamp_range: float | None = None
    feedback_resistor_ohm: float | None = None
    boost_gain: float | None = None
    dc_bias_enabled: bool | None = None
    dc_bias_level: float | None = None
    bandwidth_hz: float | None = None
    filter_type: str | None = None
    filter_enabled: bool | None = None
    filter_low_cutoff_hz: float | None = None
    filter_high_cutoff_hz: float | None = None
    input_compensation_enabled: bool | None = None
    input_compensation_level: float | None = None
    inverted: bool | None = None


class AmplifierControlSummary(msgspec.Struct, frozen=True):
    """Which write tasks one board accepts, plus the enumerated write domains.

    Derived from the driver's capability set alone, so a client can render
    disabled controls without issuing a command that would be rejected.

    Args:
        feedback_resistor: Whether the feedback resistor can be selected.
        feedback_resistor_ohm: Selectable resistances in ohm.
        boost_gain: Whether the boost gain can be set.
        boost_gain_options: Accepted boost gains.
        dc_bias: Whether DC bias injection can be switched.
        dc_bias_level: Whether the DC bias level can be written.
        bandwidth: Whether the bandwidth can be written.
        filters: Whether a filter can be written.
        filter_types: Filter responses the board supports.
        input_compensation: Whether compensation can be switched.
        input_compensation_level: Whether the compensation level can be written.
        invert_output: Whether output inversion can be switched.
    """

    feedback_resistor: bool
    feedback_resistor_ohm: list[float]
    boost_gain: bool
    boost_gain_options: list[float]
    dc_bias: bool
    dc_bias_level: bool
    bandwidth: bool
    filters: bool
    filter_types: list[str]
    input_compensation: bool
    input_compensation_level: bool
    invert_output: bool


class AmplifierInfo(msgspec.Struct, frozen=True):
    """One device's registry entry and control surface.

    Args:
        device: Worker device id and stream-key segment.
        driver: Entry point name the device was built from.
        address: Modbus slave address.
        port: Configured endpoint: the serial device for an RS485 segment, or
            ``host:port`` for a Modbus/TCP gateway.
        monitoring: Whether a poll task is currently scheduled.
        connected: Whether the most recent reads succeeded.
        controls: What the device accepts.
        transport: The service-wide transport kind, ``"rs485"`` or ``"tcp"``.
        label: Configured display label, or ``None``. Display only: the identity
            is ``device``.
    """

    device: str
    driver: str
    address: int
    port: str
    monitoring: bool
    connected: bool
    controls: AmplifierControlSummary
    transport: str = "rs485"
    label: str | None = None


class AmplifierListResponse(msgspec.Struct, frozen=True):
    """Result payload of ``current_amplifier:list``.

    Args:
        service: Service name the worker runs as.
        instance_id: Worker instance identifier.
        amplifiers: Configured devices, sorted by device id.
    """

    service: str
    instance_id: str
    amplifiers: list[AmplifierInfo]


class AmplifierStatus(msgspec.Struct, frozen=True):
    """Cached poll status of one device.

    Args:
        device: Worker device id.
        connected: Whether the most recent reads succeeded.
        monitoring: Whether a poll task is currently scheduled.
        reading_count: Successful reads since startup.
        error_count: Failed reads since startup.
        consecutive_failures: Failed reads since the last success.
        last_error: Message of the last failed read, or ``None``.
        last_reading_at: ISO-8601 UTC timestamp of the last good read, or
            ``None``.
        state: Last known state snapshot, or ``None`` before the first read.
    """

    device: str
    connected: bool
    monitoring: bool
    reading_count: int
    error_count: int
    consecutive_failures: int
    last_error: str | None
    last_reading_at: str | None
    state: dict[str, Any] | None


class AmplifierStatusResponse(msgspec.Struct, frozen=True):
    """Result payload of ``current_amplifier:status``.

    Args:
        amplifiers: One entry per requested device, sorted by device id unless a
            single device was requested.
    """

    amplifiers: list[AmplifierStatus]


class AmplifierCommandResponse(msgspec.Struct, frozen=True):
    """Result payload of ``current_amplifier:read`` and the write tasks.

    Args:
        device: Worker device id the command targeted.
        task: Task name that ran.
        applied: Values the command wrote, echoed in request spelling.
        state: State snapshot read back after the command; present for
            ``current_amplifier:read`` and for writes that re-read the board.
        readback_error: ``"<ExcType>: <msg>"`` when a write landed but its
            follow-up read failed; the command still succeeded. ``None``
            otherwise.
    """

    device: str
    task: str
    applied: dict[str, Any] = msgspec.field(default_factory=dict)
    state: dict[str, Any] | None = None
    readback_error: str | None = None


class AmplifierCommandError(msgspec.Struct, frozen=True):
    """Machine-readable detail carried in a failed command's result payload.

    The framework drops a failed task's payload when it builds the status
    record, so this payload only reaches a caller that invokes the handler
    directly. The operator-facing signal is the task's ``error_code``: an
    ambiguous write reports `AMPLIFIER_WRITE_OUTCOME_UNKNOWN`, which a consumer
    branches on without parsing the message. ``outcome_unknown`` stays here for
    direct callers and repeats that verdict.

    Args:
        device: Worker device id the command targeted.
        task: Task name that failed.
        outcome_unknown: Whether the write may have landed despite the failure.
    """

    device: str
    task: str
    outcome_unknown: bool = False


@dataclass(frozen=True, slots=True)
class _Command:
    """One write task: its capability gate and dispatch row.

    Args:
        gates: Dotted paths from the amplifier instance to the capabilities
            that must all be truthy. Instance attributes such as
            ``can_select_preamp`` and the nested
            ``capabilities.dc_bias.enabled.write`` are both plain attribute
            walks, so one resolver serves both spellings.
        apply: Coroutine function that issues the write.
        echo: Request fields reported back in ``applied`` when present.
        required: Request fields that must be present, in error-report order.
        validate: Optional domain check run after the field presence check.
        read_back: Whether to read the state again after a successful write.
        conditional_gates: Extra capability paths keyed by a request field;
            each pair contributes its paths only when the payload supplies that
            field. A task that writes a value only for some request variants
            gates on that value's capability only for those variants.
    """

    gates: tuple[str, ...]
    apply: Callable[[CurrentAmplifierProtocol, AmplifierCommandRequest], Awaitable[None]]
    echo: tuple[str, ...]
    required: tuple[str, ...] = ()
    validate: Callable[[AmplifierCommandRequest, CurrentAmplifierProtocol], TaskResult | None] | None = None
    read_back: bool = False
    conditional_gates: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _capability(amplifier: object, path: str) -> Any:
    """Resolve a dotted attribute path against an amplifier driver instance.

    Args:
        amplifier: The driver instance.
        path: Dotted path such as ``"can_set_boost"`` or
            ``"capabilities.dc_bias.level.write"``.

    Returns:
        The resolved attribute, or ``None`` when any segment is absent.
    """
    node: Any = amplifier
    for part in path.split("."):
        if node is None:
            return None
        node = getattr(node, part, None)
    return node


def _supports(amplifier: object, path: str) -> bool:
    """Whether the amplifier declares the capability at ``path``.

    A missing capability tree or a missing field counts as unsupported.

    Args:
        amplifier: The driver instance.
        path: Dotted capability path.

    Returns:
        bool: Truthiness of the resolved capability.
    """
    return bool(_capability(amplifier, path))


def _required_gates(command: _Command, request: AmplifierCommandRequest) -> tuple[str, ...]:
    """Every capability path this request must satisfy, in declaration order.

    Conditional paths apply only when the request supplies their trigger field,
    so a filter toggle that omits cutoffs is not gated on cutoff write access.
    """
    gates = list(command.gates)
    for field, extra in command.conditional_gates:
        if getattr(request, field) is not None:
            gates.extend(extra)
    return tuple(dict.fromkeys(gates))


def _options(amplifier: object, path: str) -> tuple[float, ...]:
    """The enumerated write domain at ``path``.

    Args:
        amplifier: The driver instance.
        path: Dotted path to a ``SelectableCapabilities.options`` tuple.

    Returns:
        tuple[float, ...]: The options, or an empty tuple when the driver does
        not enumerate its domain.
    """
    return tuple(_capability(amplifier, path) or ())


def _boost_options(amplifier: object) -> tuple[float, ...]:
    """Accepted boost gains: the driver's own options, else the EBIC fallback."""
    options = _options(amplifier, "capabilities.boost.gain.options")
    return options or tuple(sorted(DEFAULT_BOOST_GAINS))


def _filter_types(amplifier: object) -> tuple[FilterType, ...]:
    """Filter responses the driver supports."""
    return tuple(_capability(amplifier, "capabilities.filters.types") or ())


def _control_summary(amplifier: object) -> AmplifierControlSummary:
    """Snapshot the write surface of one board from its capability set."""
    return AmplifierControlSummary(
        feedback_resistor=_supports(amplifier, "can_select_preamp"),
        feedback_resistor_ohm=list(_options(amplifier, "capabilities.preamp.feedback_resistor.options")),
        boost_gain=_supports(amplifier, "can_set_boost"),
        boost_gain_options=list(_boost_options(amplifier)),
        dc_bias=_supports(amplifier, "capabilities.dc_bias.enabled.write"),
        dc_bias_level=_supports(amplifier, "capabilities.dc_bias.level.write"),
        bandwidth=_supports(amplifier, "can_set_bandwidth"),
        filters=_supports(amplifier, "capabilities.filters.enabled.write"),
        filter_types=[filter_type.value for filter_type in _filter_types(amplifier)],
        input_compensation=_supports(amplifier, "capabilities.input_compensation.enabled.write"),
        input_compensation_level=_supports(amplifier, "capabilities.input_compensation.level.write"),
        invert_output=_supports(amplifier, "can_set_invert_output"),
    )


def _state_dict(state: AmplifierState) -> dict[str, Any]:
    """Render a state snapshot as JSON-ready primitives.

    ``filters`` is a nested struct tuple, so it is rendered one entry at a time
    into a list of dicts; every other field is a scalar or ``None``.

    Args:
        state: The snapshot to render.

    Returns:
        A dict with one key per `AmplifierState` field.
    """
    data: dict[str, Any] = msgspec.to_builtins(state)
    data["filters"] = [msgspec.to_builtins(entry) for entry in state.filters]
    return data


def _invalid_parameters(message: str) -> TaskResult:
    """Build the non-retryable result for a parameter-validation failure."""
    return TaskResult(status="error", error=message, error_code="INVALID_PARAMETERS", retryable=False)


@dataclass(frozen=True, slots=True)
class _Failure:
    """A classified command failure and its verdict on board health.

    Args:
        result: The task result returned to the framework.
        unhealthy: Whether the failure proves the board unreachable, in which
            case the handler clears the board's connected flag. A contended bus
            is the one bus failure that leaves the board healthy.
    """

    result: TaskResult
    unhealthy: bool


def _command_result(
    message: str,
    *,
    retryable: bool,
    error_code: str = "AMPLIFIER_COMMAND_FAILED",
    payload: bytes = b"",
) -> TaskResult:
    """Build a failed command result under the shared command error code."""
    return TaskResult(
        status="error",
        error=message,
        error_code=error_code,
        retryable=retryable,
        payload=payload,
    )


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    """Return ``exc`` and its cause/context links, nearest first.

    The EBIC semantic layer wraps every relay-write failure in
    ``RelayTransitionError`` raised ``from exc``, so the Modbus cause is the
    ``__cause__`` of the exception the handler sees, never its top-level type.
    The walk is bounded and cycle-safe: a self-referential ``__cause__`` must
    not hang the handler.
    """
    links: list[BaseException] = []
    current: BaseException | None = exc
    for _ in range(_MAX_EXCEPTION_CHAIN):
        if current is None or any(current is link for link in links):
            break
        links.append(current)
        current = current.__cause__ or current.__context__
    return tuple(links)


def _command_failure(task: str, device: str, exc: Exception, *, stage: Literal["read", "write"]) -> _Failure:
    """Classify an exception raised by the driver while executing ``task``.

    A ``NotImplementedError`` means the driver stub was reached despite a truthy
    capability and a ``ValueError`` means the driver rejected the value; both are
    raised before any bus traffic, so they are terminal, say nothing about the
    board's health and leave the write outcome known.

    Bus failures are matched through the exception chain rather than the
    top-level type, because the EBIC semantic layer catches every ``set_relay``
    failure and re-raises ``RelayTransitionError`` with the original error as
    ``__cause__``. A ``GatewayBusy`` (Modbus 0x06) anywhere in the chain is
    retryable on both stages and leaves the board healthy: 0x06 is an answer
    that the request was not executed, and ``_transition`` re-reads the coil
    vector and drives toward the same absolute target, so a re-run cannot apply
    a second, different transition. The trade-off is that a board stuck busy is
    never cleared by this path — its ``connected`` flag stays set while the poll
    counters and telemetry report ``error`` — which is accepted because 0x06
    itself proves the gateway is answering.

    Every other write failure is terminal: the request may have reached the
    board even though the response did not come back, and replaying it would
    risk a duplicate hardware transition. Such a write is reported with
    `AMPLIFIER_WRITE_OUTCOME_UNKNOWN`, carries ``outcome_unknown`` for direct
    callers, and clears the board's connected flag.

    Reads keep their previous rows: a bus failure is retryable, a
    ``ModbusOperationError`` (including ``GatewayTargetFailed``, 0x0B) also
    marks the board unhealthy, and anything outside the Modbus taxonomy is
    retryable and says nothing about board health.
    """
    if isinstance(exc, NotImplementedError):
        return _Failure(
            TaskResult(status="error", error=f"Driver does not implement {task}", error_code="UNSUPPORTED", retryable=False),
            unhealthy=False,
        )
    if isinstance(exc, ValueError):
        return _Failure(
            TaskResult(status="error", error=str(exc), error_code="INVALID_PARAMETERS", retryable=False),
            unhealthy=False,
        )

    chain = _exception_chain(exc)
    message = f"{task} failed: {type(exc).__name__}: {exc}"
    if any(isinstance(link, GatewayBusy) for link in chain):
        return _Failure(_command_result(message, retryable=True), unhealthy=False)
    if stage == "write":
        # WHY: the top-level RelayTransitionError (or an exception outside the
        # Modbus taxonomy) may have been raised after the request left the host.
        # Only 0x06 proves non-execution, so report the ambiguity rather than
        # invite a replay.
        detail = AmplifierCommandError(device=device, task=task, outcome_unknown=True)
        return _Failure(
            _command_result(
                f"{message} (write outcome unknown; do not retry)",
                retryable=False,
                error_code=AMPLIFIER_WRITE_OUTCOME_UNKNOWN,
                payload=msgspec.json.encode(detail),
            ),
            unhealthy=True,
        )
    if any(isinstance(link, ModbusOperationError) for link in chain):
        return _Failure(_command_result(message, retryable=True), unhealthy=True)
    return _Failure(_command_result(message, retryable=True), unhealthy=False)


def _host_port(host: str, port: int) -> str:
    """Render ``host:port``, bracketing an IPv6 literal.

    A literal IPv6 address already contains colons, so an unbracketed
    ``::1:502`` cannot be split back into host and port. An already-bracketed
    literal is left as configured.
    """
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _endpoint(transport: AmplifierTransportSettings) -> str:
    """Render the configured endpoint for the list payload.

    An ``rs485`` segment reports its serial device. A ``tcp`` segment reports the
    gateway as ``host:tcp_port``: that is the connection every device on the
    segment is reached through, while each device's Modbus address distinguishes
    them. The transport is service-wide, so every entry renders the same
    endpoint.
    """
    if transport.kind == "tcp":
        return _host_port(cast(str, transport.host), transport.tcp_port)
    return cast(str, transport.port)


async def _apply_feedback_resistor(amplifier: CurrentAmplifierProtocol, request: AmplifierCommandRequest) -> None:
    """Select the feedback resistor; the driver keeps the loop closed while switching."""
    await amplifier.select_feedback_resistor(cast(float, request.feedback_resistor_ohm))


async def _apply_boost_gain(amplifier: CurrentAmplifierProtocol, request: AmplifierCommandRequest) -> None:
    """Select the boost gain; never call this during a measurement."""
    await amplifier.set_boost_gain(cast(float, request.boost_gain))


async def _apply_dc_bias(amplifier: CurrentAmplifierProtocol, request: AmplifierCommandRequest) -> None:
    """Switch DC bias injection."""
    await amplifier.set_dc_bias(cast(bool, request.dc_bias_enabled))


async def _apply_dc_bias_level(amplifier: CurrentAmplifierProtocol, request: AmplifierCommandRequest) -> None:
    """Write the DC bias level in device-native units."""
    await amplifier.set_dc_bias_level(cast(float, request.dc_bias_level))


async def _apply_bandwidth(amplifier: CurrentAmplifierProtocol, request: AmplifierCommandRequest) -> None:
    """Write the bandwidth in Hz."""
    await amplifier.set_bandwidth(cast(float, request.bandwidth_hz))


async def _apply_filter(amplifier: CurrentAmplifierProtocol, request: AmplifierCommandRequest) -> None:
    """Write one filter entry; an omitted ``filter_enabled`` leaves the filter off."""
    setting = FilterSetting(
        type=FilterType(cast(str, request.filter_type)),
        enabled=bool(request.filter_enabled),
        low_cutoff_hz=request.filter_low_cutoff_hz,
        high_cutoff_hz=request.filter_high_cutoff_hz,
    )
    await amplifier.set_filter(setting)


async def _apply_input_compensation(amplifier: CurrentAmplifierProtocol, request: AmplifierCommandRequest) -> None:
    """Switch input-capacitance compensation."""
    await amplifier.set_input_compensation(cast(bool, request.input_compensation_enabled))


async def _apply_input_compensation_level(amplifier: CurrentAmplifierProtocol, request: AmplifierCommandRequest) -> None:
    """Write the compensation level in device-native units."""
    await amplifier.set_input_compensation_level(cast(float, request.input_compensation_level))


async def _apply_inverted(amplifier: CurrentAmplifierProtocol, request: AmplifierCommandRequest) -> None:
    """Switch output-polarity inversion."""
    await amplifier.set_inverted(cast(bool, request.inverted))


def _validate_feedback_resistor(request: AmplifierCommandRequest, amplifier: CurrentAmplifierProtocol) -> TaskResult | None:
    """Reject a resistance that is not one of the board's enumerated resistors."""
    options = _options(amplifier, "capabilities.preamp.feedback_resistor.options")
    if request.feedback_resistor_ohm not in options:
        return _invalid_parameters(
            f"feedback_resistor_ohm must be one of {sorted(options)}, got {request.feedback_resistor_ohm!r}"
        )
    return None


def _validate_boost_gain(request: AmplifierCommandRequest, amplifier: CurrentAmplifierProtocol) -> TaskResult | None:
    """Reject a gain outside the board's enumerated boost gains."""
    options = _boost_options(amplifier)
    if request.boost_gain not in options:
        return _invalid_parameters(f"boost_gain must be one of {sorted(options)}, got {request.boost_gain!r}")
    return None


def _validate_filter(request: AmplifierCommandRequest, amplifier: CurrentAmplifierProtocol) -> TaskResult | None:
    """Reject an unknown filter spelling or a filter the board does not expose."""
    try:
        filter_type = FilterType(cast(str, request.filter_type))
    except ValueError:
        allowed = [member.value for member in FilterType]
        return _invalid_parameters(f"filter_type must be one of {allowed}, got {request.filter_type!r}")
    supported = _filter_types(amplifier)
    if filter_type not in supported:
        allowed = [member.value for member in supported]
        return _invalid_parameters(f"filter_type must be one of {allowed}, got {request.filter_type!r}")
    return None


#: Write tasks and their dispatch row. Adding a control means adding one row.
_COMMAND_BY_TASK: dict[str, _Command] = {
    AMPLIFIER_SET_FEEDBACK_RESISTOR_TASK_NAME: _Command(
        gates=("can_select_preamp",),
        apply=_apply_feedback_resistor,
        echo=("feedback_resistor_ohm",),
        required=("feedback_resistor_ohm",),
        validate=_validate_feedback_resistor,
        read_back=True,
    ),
    AMPLIFIER_SET_BOOST_GAIN_TASK_NAME: _Command(
        gates=("can_set_boost",),
        apply=_apply_boost_gain,
        echo=("boost_gain",),
        required=("boost_gain",),
        validate=_validate_boost_gain,
    ),
    AMPLIFIER_SET_DC_BIAS_TASK_NAME: _Command(
        gates=("capabilities.dc_bias.enabled.write",),
        apply=_apply_dc_bias,
        echo=("dc_bias_enabled",),
        required=("dc_bias_enabled",),
    ),
    AMPLIFIER_SET_DC_BIAS_LEVEL_TASK_NAME: _Command(
        gates=("capabilities.dc_bias.level.write",),
        apply=_apply_dc_bias_level,
        echo=("dc_bias_level",),
        required=("dc_bias_level",),
    ),
    AMPLIFIER_SET_BANDWIDTH_TASK_NAME: _Command(
        gates=("can_set_bandwidth",),
        apply=_apply_bandwidth,
        echo=("bandwidth_hz",),
        required=("bandwidth_hz",),
    ),
    AMPLIFIER_SET_FILTER_TASK_NAME: _Command(
        gates=("capabilities.filters.enabled.write",),
        apply=_apply_filter,
        echo=("filter_type", "filter_enabled", "filter_low_cutoff_hz", "filter_high_cutoff_hz"),
        required=("filter_type",),
        validate=_validate_filter,
        conditional_gates=(
            ("filter_low_cutoff_hz", ("capabilities.filters.cutoff.write",)),
            ("filter_high_cutoff_hz", ("capabilities.filters.cutoff.write",)),
        ),
    ),
    AMPLIFIER_SET_INPUT_COMPENSATION_TASK_NAME: _Command(
        gates=("capabilities.input_compensation.enabled.write",),
        apply=_apply_input_compensation,
        echo=("input_compensation_enabled",),
        required=("input_compensation_enabled",),
    ),
    AMPLIFIER_SET_INPUT_COMPENSATION_LEVEL_TASK_NAME: _Command(
        gates=("capabilities.input_compensation.level.write",),
        apply=_apply_input_compensation_level,
        echo=("input_compensation_level",),
        required=("input_compensation_level",),
    ),
    AMPLIFIER_SET_INVERTED_TASK_NAME: _Command(
        gates=("can_set_invert_output",),
        apply=_apply_inverted,
        echo=("inverted",),
        required=("inverted",),
    ),
}


class AmplifierControlHandler(TaskHandler):
    """Handle the ``current_amplifier:*`` tasks on the data lane.

    The worker is injected as a constructor keyword argument (``worker=``) via
    ``add_task_handler`` — the handler context carries no worker reference.
    """

    control: ClassVar[bool] = False

    def __init__(self, name: str, context: TaskHandlerContext, *, worker: CurrentAmplifierWorker) -> None:
        """Initialize the handler.

        Args:
            name: Human-readable name for this handler instance.
            context: Narrow context provided by the owning processor.
            worker: The current amplifier worker whose registry is controlled.
        """
        super().__init__(name, context)
        self._worker: CurrentAmplifierWorker = worker

    @property
    def supported_tasks(self) -> list[str]:
        """Task types this handler serves."""
        return [
            AMPLIFIER_LIST_TASK_NAME,
            AMPLIFIER_STATUS_TASK_NAME,
            AMPLIFIER_READ_TASK_NAME,
            AMPLIFIER_SELECT_PREAMP_RANGE_TASK_NAME,
            *_COMMAND_BY_TASK,
        ]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Execute one ``current_amplifier:*`` command against the targeted device.

        Checks, in order: payload decode, device lookup, capability gate,
        parameter presence and domain, then dispatch. A write exception is
        classified by `_command_failure`; a failed read-back after a write that
        landed is reported on the successful result, never as a retryable error.
        """
        task = task_data.task
        if task == AMPLIFIER_LIST_TASK_NAME:
            return self._list_amplifiers()
        if task == AMPLIFIER_STATUS_TASK_NAME:
            return self._status(task_data)

        try:
            request = msgspec.json.decode(task_data.payload, type=AmplifierCommandRequest)
        except msgspec.DecodeError as exc:
            return TaskResult(status="error", error=str(exc), error_code="INVALID_PAYLOAD", retryable=False)

        device = request.device
        amplifier = self._worker.amplifiers.get(device)
        if amplifier is None:
            return TaskResult(
                status="error", error=f"Unknown amplifier device {device!r}", error_code="UNKNOWN_AMPLIFIER", retryable=False
            )

        if task == AMPLIFIER_READ_TASK_NAME:
            return await self._read(task, device, amplifier)

        if task == AMPLIFIER_SELECT_PREAMP_RANGE_TASK_NAME:
            # WHY: the verb has an owner so the plugin's whole catalogue lands
            # here, but a range cannot be mapped to a feedback resistance
            # without inventing board semantics. Answer the terminal verdict
            # instead of dispatching to a driver method that does not exist.
            return TaskResult(
                status="error",
                error=f"{task} is unsupported: {PREAMP_RANGE_UNSUPPORTED_REASON}",
                error_code="UNSUPPORTED",
                retryable=False,
            )

        command = _COMMAND_BY_TASK[task]
        unsupported_gate = next(
            (gate for gate in _required_gates(command, request) if not _supports(amplifier, gate)),
            None,
        )
        if unsupported_gate is not None:
            return TaskResult(
                status="error",
                error=f"Amplifier {device!r} does not support {task}",
                error_code="UNSUPPORTED",
                retryable=False,
            )

        missing = next((field for field in command.required if getattr(request, field) is None), None)
        if missing is not None:
            return _invalid_parameters(f"{task} requires {missing}")
        if command.validate is not None:
            invalid = command.validate(request, amplifier)
            if invalid is not None:
                return invalid

        try:
            await command.apply(amplifier, request)
        except Exception as exc:
            return self._fail(device, _command_failure(task, device, exc, stage="write"))

        state: AmplifierState | None = None
        readback_error: str | None = None
        if command.read_back:
            try:
                state = await amplifier.read_data()
            except Exception as exc:
                # WHY: the write already landed, so retrying the task would
                # replay a duplicate hardware transition. Report the write as
                # applied and keep the read failure as diagnostic data.
                readback_error = f"{type(exc).__name__}: {exc}"
                self.logger.warning(
                    "%s on amplifier %r wrote successfully but the read-back failed: %s",
                    task,
                    device,
                    readback_error,
                )

        response = AmplifierCommandResponse(
            device=device,
            task=task,
            applied={field: value for field in command.echo if (value := getattr(request, field)) is not None},
            state=_state_dict(state) if state is not None else None,
            readback_error=readback_error,
        )
        return TaskResult(status="success", payload=msgspec.json.encode(response))

    def _list_amplifiers(self) -> TaskResult:
        """Report the registry and each device's control surface."""
        amplifiers = [self._info(device, amplifier) for device, amplifier in sorted(self._worker.amplifiers.items())]
        response = AmplifierListResponse(
            service=self._worker.service_name,
            instance_id=self._worker.instance_id,
            amplifiers=amplifiers,
        )
        return TaskResult(status="success", payload=msgspec.json.encode(response))

    def _info(self, device: str, amplifier: CurrentAmplifierProtocol) -> AmplifierInfo:
        """Build one list entry from the registry and its settings."""
        device_settings = self._worker.amplifier_settings[device]
        service_settings = self._worker.settings
        transport = service_settings.transport if service_settings is not None else None
        return AmplifierInfo(
            device=device,
            driver=device_settings.driver,
            address=device_settings.address,
            port=_endpoint(transport) if transport is not None else "",
            monitoring=self._worker.monitoring.get(device, False),
            connected=self._worker.connected.get(device, False),
            controls=_control_summary(amplifier),
            transport=transport.kind if transport is not None else "rs485",
            label=device_settings.label,
        )

    def _fail(self, device: str, failure: _Failure) -> TaskResult:
        """Return a classified failure, marking the device unhealthy when proven."""
        if failure.unhealthy:
            self._worker.connected[device] = False
        return failure.result

    def _status(self, task_data: TaskData) -> TaskResult:
        """Report cached status for one device or every device."""
        if task_data.payload:
            try:
                request = msgspec.json.decode(task_data.payload, type=AmplifierStatusRequest)
            except msgspec.DecodeError as exc:
                return TaskResult(status="error", error=str(exc), error_code="INVALID_PAYLOAD", retryable=False)
        else:
            request = AmplifierStatusRequest()

        if request.device is not None:
            if request.device not in self._worker.amplifiers:
                return TaskResult(
                    status="error",
                    error=f"Unknown amplifier device {request.device!r}",
                    error_code="UNKNOWN_AMPLIFIER",
                    retryable=False,
                )
            devices = [request.device]
        else:
            devices = sorted(self._worker.amplifiers)

        response = AmplifierStatusResponse(amplifiers=[self._status_entry(device) for device in devices])
        return TaskResult(status="success", payload=msgspec.json.encode(response))

    def _status_entry(self, device: str) -> AmplifierStatus:
        """Build one status entry from the poll counters and last good snapshot."""
        stats = self._worker.stats[device]
        last_reading_at = stats.last_reading_at
        return AmplifierStatus(
            device=device,
            connected=self._worker.connected.get(device, False),
            monitoring=self._worker.monitoring.get(device, False),
            reading_count=stats.reading_count,
            error_count=stats.error_count,
            consecutive_failures=stats.consecutive_failures,
            last_error=stats.last_error,
            last_reading_at=last_reading_at.isoformat() if last_reading_at is not None else None,
            state=_state_dict(stats.last_state) if stats.last_state is not None else None,
        )

    async def _read(self, task: str, device: str, amplifier: CurrentAmplifierProtocol) -> TaskResult:
        """Read the live state of one device."""
        try:
            state = await amplifier.read_data()
        except Exception as exc:
            return self._fail(device, _command_failure(task, device, exc, stage="read"))
        response = AmplifierCommandResponse(device=device, task=task, state=_state_dict(state))
        return TaskResult(status="success", payload=msgspec.json.encode(response))
