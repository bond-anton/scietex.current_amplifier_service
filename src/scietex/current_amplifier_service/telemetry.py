"""Telemetry snapshot the current amplifier worker publishes to Valkey.

The worker pushes one ``msgspec`` msgpack snapshot per instance to
``scietex:{service}:{instance_id}:telemetry`` every 2 s with a 6 s TTL, so a
single missed beat does not expire the key. The API reads it with its own
``CurrentAmplifierServiceTelemetry`` struct: the service fields and the
``devices`` map keyed by the worker device id are the wire contract, and unknown
fields are tolerated by that decoder. The per-device entry therefore also
carries the producer-only ``reading_count``, ``error_count``,
``transconductance_v_per_a`` and ``transport`` extras, plus the display
``label`` when one is configured.

Every measurement is taken from the device's last `AmplifierState` and stays
``None`` when the driver has never produced it, matching the HAL's "unknown is
not zero" rule. ``preamp_range`` and ``feedback_resistor_ohm`` carry the same
value (the selected Rf in ohm) so both the older and the newer consumer spelling
decode. Filters are encoded as a list of structs, not a JSON string.

State derivation is deliberately coarse, because the API renders one badge per
device and one for the service:

* device ``"error"`` when the latest poll failed (``consecutive_failures > 0``),
  else ``"monitoring"`` when a poll task is scheduled, else ``"ok"`` when the
  device is connected and has produced a reading, else ``"idle"``.
* service ``"running"`` when any device is monitoring, else ``"idle"`` when
  devices are configured, else ``"unconfigured"``.

Precedence matters: a failing poll wins over the monitoring flag so a broken
board never shows as healthy, and a scheduled poll wins over a stale reading so
a live-but-silent board is still reported as monitoring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import msgspec
from scietex.hal.current_amplifier.base.data import FilterSetting

if TYPE_CHECKING:
    from scietex.hal.current_amplifier.base.data import AmplifierState
    from scietex.hal.current_amplifier.base.protocol import CurrentAmplifierProtocol

    from .amplifier_worker import AmplifierStats, CurrentAmplifierWorker
    from .config import AmplifierDeviceSettings

#: Push cadence and key TTL: 2 s interval, 6 s TTL so a single missed beat does
#: not expire the key.
TELEMETRY_INTERVAL: float = 2.0
TELEMETRY_TTL: int = 6

#: Payload revision published by the worker. Revision 1 is the initial snapshot.
SCHEMA_VERSION: int = 1


class AmplifierTelemetry(msgspec.Struct, frozen=True):
    """One device's decoded state, matching the API read struct field-for-field.

    Field order mirrors ``CurrentAmplifierDeviceTelemetry``. ``reading_count``,
    ``error_count``, ``transconductance_v_per_a``, ``transport`` and ``label``
    are producer-only extras: the API decoder tolerates and ignores them.

    Every measurement and setting is ``None`` when the driver could not produce
    it, which is how the HAL distinguishes "unknown" from ``0``/``False``.

    Args:
        state: Derived device state (``error``, ``monitoring``, ``ok``, ``idle``).
        driver: Entry point name the device was built from.
        connected: Whether the most recent reads succeeded.
        monitoring: Whether a poll task is currently scheduled.
        ts: UTC ISO-8601 timestamp of the last reading, or ``None``.
        output_voltage_v: Output voltage in V, or ``None`` when unreadable.
        output_current_a: Output current in A, or ``None`` when unreadable.
        input_current_a: Input (source) current in A, or ``None`` when unreadable.
        preamp_range: Selected feedback resistor (Rf) in ohm, or ``None``.
        boost_gain: Dimensionless boost stage setting, or ``None``.
        feedback_resistor_ohm: Feedback resistor in ohm, or ``None``.
        dc_bias_enabled: Whether DC bias injection is on, or ``None``.
        dc_bias_level: DC bias level in device-native units, or ``None``.
        bandwidth_hz: Bandwidth in Hz, or ``None``.
        filters: Configured filters in the order the device reports them.
        input_overcurrent: Input-overcurrent flag, or ``None``.
        output_overload: Output-overload flag, or ``None``.
        input_compensation_enabled: Whether compensation is on, or ``None``.
        input_compensation_level: Compensation level in device-native units.
        inverted: Whether the output polarity is inverted, or ``None``.
        last_error: Message of the last failed read, or ``None``.
        last_ok: UTC ISO-8601 timestamp of the last successful read, or ``None``.
        reading_count: Successful reads since startup.
        error_count: Failed reads since startup.
        transconductance_v_per_a: ``Rf * gain`` when both are known.
        transport: The service-wide transport kind, ``"rs485"`` or ``"tcp"``.
        label: Configured display label, or ``None``; the map key is the identity.
    """

    state: str
    driver: str | None = None
    connected: bool = False
    monitoring: bool = False
    ts: str | None = None
    output_voltage_v: float | None = None
    output_current_a: float | None = None
    input_current_a: float | None = None
    preamp_range: float | None = None
    boost_gain: float | None = None
    feedback_resistor_ohm: float | None = None
    dc_bias_enabled: bool | None = None
    dc_bias_level: float | None = None
    bandwidth_hz: float | None = None
    filters: list[FilterSetting] = msgspec.field(default_factory=list)
    input_overcurrent: bool | None = None
    output_overload: bool | None = None
    input_compensation_enabled: bool | None = None
    input_compensation_level: float | None = None
    inverted: bool | None = None
    last_error: str | None = None
    last_ok: str | None = None
    reading_count: int = 0
    error_count: int = 0
    transconductance_v_per_a: float | None = None
    transport: str = "rs485"
    label: str | None = None


class CurrentAmplifierServiceTelemetry(msgspec.Struct, frozen=True):
    """Service telemetry snapshot pushed by the worker.

    ``devices`` is keyed by the worker device id, matching the config mapping and
    the ``{device}`` segment of the readings-stream key. ``poll_enabled`` reports
    whether any device is monitoring, so the API can tell a stopped service from
    a running one.

    Args:
        state: Derived service state (``running``, ``idle``, ``unconfigured``).
        service: Service name the worker runs as.
        instance_id: Worker instance identifier.
        configured: Whether the device map is populated.
        poll_enabled: Whether any device is currently monitoring.
        timestamp: When the payload was built, as UTC ISO-8601.
        devices: One entry per configured device, keyed and sorted by device id.
        schema_version: Payload schema revision.
    """

    state: str
    service: str
    instance_id: str
    configured: bool
    poll_enabled: bool
    timestamp: str
    devices: dict[str, AmplifierTelemetry] = msgspec.field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION


def build_telemetry(worker: CurrentAmplifierWorker) -> CurrentAmplifierServiceTelemetry:
    """Snapshot the worker's device registry into a telemetry payload.

    An unconfigured worker (empty device map) reports ``configured=False`` and
    ``state="unconfigured"``. A configured worker reports one entry per device,
    sorted by device id, and derives the service state from the poll flags.
    """
    configured = bool(worker.amplifier_settings)
    monitoring = any(worker.monitoring.values())
    settings = worker.settings
    transport = settings.transport.kind if settings is not None else "rs485"
    return CurrentAmplifierServiceTelemetry(
        state=_service_state(configured, monitoring),
        service=worker.service_name,
        instance_id=worker.instance_id,
        configured=configured,
        poll_enabled=monitoring,
        timestamp=datetime.now(timezone.utc).isoformat(),
        devices={
            device: _amplifier_telemetry(worker, device, device_settings, transport)
            for device, device_settings in sorted(worker.amplifier_settings.items())
        },
    )


def _service_state(configured: bool, monitoring: bool) -> str:
    """Derive the service badge: ``running`` > ``idle`` > ``unconfigured``."""
    if monitoring:
        return "running"
    if configured:
        return "idle"
    return "unconfigured"


def _device_state(stats: AmplifierStats, monitoring: bool, connected: bool) -> str:
    """Derive the board badge: ``error`` > ``monitoring`` > ``ok`` > ``idle``.

    A failed poll wins over the monitoring flag so a broken board never shows as
    healthy. ``ok`` additionally requires a reading to exist, so a connected
    board that has not answered yet stays ``idle`` rather than claiming health.
    """
    if stats.consecutive_failures > 0:
        return "error"
    if monitoring:
        return "monitoring"
    if connected and stats.last_state is not None:
        return "ok"
    return "idle"


def _amplifier_telemetry(
    worker: CurrentAmplifierWorker, device: str, settings: AmplifierDeviceSettings, transport: str
) -> AmplifierTelemetry:
    """Build one device's telemetry entry from its stats and last state."""
    stats = worker.stats[device]
    state = stats.last_state
    monitoring = worker.monitoring.get(device, False)
    connected = worker.connected.get(device, False)
    # Only successful reads are timestamped, so the last reading and the last
    # successful reading are the same event in this worker.
    last_ok = stats.last_reading_at.isoformat() if stats.last_reading_at is not None else None
    return AmplifierTelemetry(
        state=_device_state(stats, monitoring, connected),
        driver=settings.driver,
        connected=connected,
        monitoring=monitoring,
        ts=last_ok,
        output_voltage_v=state.output_voltage_v if state is not None else None,
        output_current_a=state.output_current_a if state is not None else None,
        input_current_a=state.input_current_a if state is not None else None,
        preamp_range=state.feedback_resistor_ohm if state is not None else None,
        boost_gain=state.boost_gain if state is not None else None,
        feedback_resistor_ohm=state.feedback_resistor_ohm if state is not None else None,
        dc_bias_enabled=state.dc_bias_enabled if state is not None else None,
        dc_bias_level=state.dc_bias_level if state is not None else None,
        bandwidth_hz=state.bandwidth_hz if state is not None else None,
        filters=list(state.filters) if state is not None else [],
        input_overcurrent=state.input_overcurrent if state is not None else None,
        output_overload=state.output_overload if state is not None else None,
        input_compensation_enabled=state.input_compensation_enabled if state is not None else None,
        input_compensation_level=state.input_compensation_level if state is not None else None,
        inverted=state.inverted if state is not None else None,
        last_error=stats.last_error,
        last_ok=last_ok,
        reading_count=stats.reading_count,
        error_count=stats.error_count,
        transconductance_v_per_a=_transconductance(worker.amplifiers.get(device), state),
        transport=transport,
        label=settings.label,
    )


def _transconductance(amplifier: CurrentAmplifierProtocol | None, state: AmplifierState | None) -> float | None:
    """Whole-amplifier transconductance ``Rf * gain``, or ``None`` when unknown.

    A board that reports no feedback resistor or no gain cannot define a
    transconductance, so the value stays ``None`` rather than assuming unity.
    The capability helper validates the resistor and raises `ValueError` on a
    non-positive one; that is reported as unknown too.
    """
    if amplifier is None or state is None:
        return None
    feedback = state.feedback_resistor_ohm
    gain = state.boost_gain
    if feedback is None or gain is None:
        return None
    try:
        return amplifier.capabilities.total_transconductance(feedback, gain)
    except ValueError:
        return None
