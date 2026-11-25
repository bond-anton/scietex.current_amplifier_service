"""Current amplifier worker: a Valkey-backed service that polls and controls
current amplifier boards.

`CurrentAmplifierWorker` extends `ValkeyWorker` and drives one or more EBIC
v1.0 current-amplifier boards, each over its own RS485 segment. A board is
driven by a `scietex.hal.current_amplifier` class selected by entry point name;
the worker seeds its in-memory board registry from the service-owned
``current_amplifier.yml`` at startup and reconciles it against the framework's
merged remote-config section (the L3 ``current_amplifier`` section replaces the
YAML list wholesale, it is never merged by label). A ``Telemetry`` manager
publishes a service snapshot every 2 s so the UI can observe the configured/live
state, and one long-lived poll task per board publishes readings to a per-board
Valkey stream.

Unlike `scietex.vfd_service.vfd_worker`, this worker has no persistent transport
to open: the EBIC driver connects the serial port once per operation, so
``connect()`` is a documented no-op. There is therefore no transport-open flag,
no connect stage, and no reconnect branch in the poll loop — a failed read is
only a failed read, and ``_connected`` means "the recent reads succeeded".
Protocol failures are also ``OSError`` subclasses in this HAL, so inferring a
dead bus from the exception type would be wrong.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, cast

import msgspec
from glide import ConnectionError as GlideConnectionError
from glide import ExpirySet, ExpiryType, RequestError
from glide import TimeoutError as GlideTimeoutError
from scietex.hal.current_amplifier.base.data import AmplifierState
from scietex.hal.current_amplifier.base.protocol import CurrentAmplifierProtocol
from scietex.hal.current_amplifier.registry import ENTRY_POINT_GROUP, resolve_driver
from scietex.hal.serial import ModbusSerialConnectionConfig
from scietex.service import Manager, ValkeyWorker, ValkeyWorkerConfig
from scietex.service.valkey._glide import StreamAddOptions, TrimByMaxLen

from .config import (
    CURRENT_AMPLIFIER_SECTION,
    CURRENT_AMPLIFIER_SETTINGS_DEFAULTS,
    AmplifierSettings,
    CurrentAmplifierServiceSettings,
    read_current_amplifier_config,
)
from .task_handlers import AmplifierControlHandler, MonitoringHandler
from .telemetry import TELEMETRY_INTERVAL, TELEMETRY_TTL, build_telemetry

#: Entry point group under which ``scietex.hal.current_amplifier`` advertises
#: its drivers. A board configuration stores the entry point name.
DRIVER_ENTRY_POINT_GROUP: str = ENTRY_POINT_GROUP

#: Default cap on the number of entries retained per board's readings stream.
#: The effective value comes from ``CurrentAmplifierServiceSettings.stream_maxlen``.
STREAM_MAXLEN: int = 10000

#: Default number of consecutive failed reads before a board is reported as
#: disconnected. The effective value comes from
#: ``CurrentAmplifierServiceSettings.failure_threshold``.
CONNECTED_FAILURE_THRESHOLD: int = 3


class AmplifierStats(msgspec.Struct):
    """Per-board counters accumulated by the poll loop.

    Mutable (no ``frozen=True``): the poll loop updates the fields in place.
    """

    reading_count: int = 0
    error_count: int = 0
    last_state: AmplifierState | None = None
    last_reading_at: datetime | None = None
    last_error: str | None = None
    # Consecutive failed reads since the last success. A single bus glitch must
    # not flip the board to disconnected, so the poll loop only clears
    # `_connected` once this reaches the configured failure threshold.
    consecutive_failures: int = 0


def _accepts_keyword(driver: type, name: str) -> bool:
    """Whether ``driver.__init__`` accepts a keyword argument called ``name``.

    Drivers differ in what they take: the EBIC v1.0 driver accepts
    ``relay_settle_s`` as a constructor-only knob, while a future driver may
    not. Passing it unconditionally would make that driver's constructor raise
    ``TypeError`` and the whole board registry fail to build, so the worker
    asks first.

    A driver that declares ``**kwargs`` accepts anything, which is why the
    variadic check comes before the named-parameter check.

    Args:
        driver: The resolved driver class.
        name: The keyword argument name to look for.

    Returns:
        bool: ``True`` when the constructor can be called with ``name=...``.
    """
    try:
        parameters = inspect.signature(driver.__init__).parameters.values()
    except (TypeError, ValueError):
        # A C-level or otherwise uninspectable constructor: assume it does not
        # take the optional keyword rather than risk a TypeError at build time.
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (parameter.name == name and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY)
        for parameter in parameters
    )


class CurrentAmplifierWorker(ValkeyWorker):
    """A `ValkeyWorker` that polls and controls one or more current amplifier boards.

    Settings resolve through the framework's four-layer merge: constructor
    default (L0) < ``current_amplifier.yml`` bootstrap patch (L1, read directly
    here) < framework ``config.yml`` snapshot (L2) < remote
    ``current_amplifier`` section (L3). The section is registered with
    ``bootstrap=None``, so L1 never enters the framework's layer stack; the
    worker reads the YAML itself to seed the initial registry and then
    reconciles against the merged section after ``super().initialize()``. The
    merged ``amplifiers`` list replaces the YAML list entirely — it is not
    merged by label.

    A board is polled by one long-lived task; it is never connected during
    startup, so an offline board does not block the service. A reconfig at
    runtime is restart-required (the registry is not live-rebuilt).
    """

    def __init__(self, config: ValkeyWorkerConfig | None = None, *, client_factory=None, theme=None) -> None:
        super().__init__(config, client_factory=client_factory, theme=theme)
        self._settings: CurrentAmplifierServiceSettings | None = None
        self._amplifiers: dict[str, CurrentAmplifierProtocol] = {}
        self._amplifier_settings: dict[str, AmplifierSettings] = {}
        self._monitoring: dict[str, bool] = {}
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._connected: dict[str, bool] = {}
        self._stats: dict[str, AmplifierStats] = {}
        # Startup guard: the config apply hook runs both during
        # ``super().initialize()`` (L2/L3 delivery, where ``initialize`` itself
        # builds the registry) and at runtime (where a change is restart-
        # required). False until ``initialize`` returns.
        self._startup_complete: bool = False
        self._restart_required: bool = False
        # Telemetry encoding is done with an encoder owned here (the framework's
        # ``__encoder`` is private), and the key is resolved once at
        # construction.
        self._telemetry_encoder = msgspec.msgpack.Encoder()
        self._telemetry_key = f"scietex:{self.service_name}:{self.instance_id}:telemetry"
        self.register_config_settings(
            CURRENT_AMPLIFIER_SECTION,
            CurrentAmplifierServiceSettings,
            apply=self._apply_settings,
            defaults=CURRENT_AMPLIFIER_SETTINGS_DEFAULTS,
            # bootstrap=None is deliberate: the worker reads the YAML itself and
            # seeds L0 from it, so L1 never enters the layer stack and the
            # remote L3 config cleanly replaces it (see config.py module
            # docstring).
            bootstrap=None,
        )
        # Control-lane monitoring switch and data-lane board commands. Both
        # handlers receive the worker by constructor injection: the handler
        # context carries no worker reference.
        self.add_task_handler(MonitoringHandler, worker=self)
        self.add_task_handler(AmplifierControlHandler, worker=self)

    @property
    def amplifiers(self) -> dict[str, CurrentAmplifierProtocol]:
        """The live board registry keyed by label (the dict itself, not a copy).

        Mutating callers share the worker's registry; read-only consumers should
        iterate without changing it.
        """
        return self._amplifiers

    @property
    def amplifier_settings(self) -> dict[str, AmplifierSettings]:
        """The per-label board settings the registry was built from."""
        return self._amplifier_settings

    @property
    def monitoring(self) -> dict[str, bool]:
        """Per-label poll-loop activity: True while a poll task is scheduled."""
        return self._monitoring

    @property
    def connected(self) -> dict[str, bool]:
        """Per-label read state tracked by the poll loop."""
        return self._connected

    @property
    def stats(self) -> dict[str, AmplifierStats]:
        """Per-label counters accumulated by the poll loop."""
        return self._stats

    @property
    def restart_required(self) -> bool:
        """Whether a post-startup settings apply requires a restart to rebuild the registry."""
        return self._restart_required

    def _failure_threshold(self) -> int:
        """Consecutive failed reads before a board is reported as disconnected.

        Falls back to the module default before settings are applied, so a poll
        task that starts early still has a usable bound.
        """
        settings = self._settings
        return settings.failure_threshold if settings is not None else CONNECTED_FAILURE_THRESHOLD

    def _stream_maxlen(self) -> int:
        """Entries retained per board's readings stream.

        Falls back to the module default before settings are applied.
        """
        settings = self._settings
        return settings.stream_maxlen if settings is not None else STREAM_MAXLEN

    async def initialize(self) -> bool:
        """Seed the registry from the YAML, then reconcile against the merged settings.

        The service-owned YAML is read before ``super().initialize()`` so the
        registry is populated even when no remote config exists; the merged
        section read back afterwards replaces it wholesale when it differs. A
        bad YAML or a board that fails to resolve/construct fails startup: a
        half-working registry must not run.

        Returns:
            `True` if the framework initialized and the registry is built;
            `False` on any configuration or startup failure.
        """
        self._startup_complete = False
        try:
            raw = read_current_amplifier_config(self.conf_dir)
        except RuntimeError as exc:
            self.logger.error("Failed to read current amplifier configuration: %s", exc)
            return False
        try:
            settings = msgspec.convert(raw, CurrentAmplifierServiceSettings)
        except (msgspec.ValidationError, ValueError) as exc:
            self.logger.error("Invalid current amplifier configuration: %s", exc)
            return False
        try:
            await self._build_amplifiers(settings)
        except ValueError as exc:
            self.logger.error("Failed to build current amplifier registry: %s", exc)
            return False

        if not await super().initialize():
            return False

        resolved = self.current_config_settings(CURRENT_AMPLIFIER_SECTION)
        if resolved is not None:
            resolved_settings = cast(CurrentAmplifierServiceSettings, resolved)
            if resolved_settings != settings:
                # L3 wins wholesale: the merged ``amplifiers`` list replaces the
                # YAML-seeded list entirely, it is not merged by label.
                self._apply_settings(resolved_settings)
                try:
                    await self._build_amplifiers(resolved_settings)
                except ValueError as exc:
                    self.logger.error("Failed to rebuild current amplifier registry from merged settings: %s", exc)
                    return False

        self._startup_complete = True
        self.logger.info("Current amplifier service started: %d board(s)", len(self._amplifiers))
        return True

    async def cleanup(self) -> None:
        """Cancel poll tasks and close boards, then the framework resources."""
        await self.close_amplifiers()
        await super().cleanup()

    async def _build_amplifiers(self, settings: CurrentAmplifierServiceSettings) -> None:
        """(Re)build the board registry from the given settings.

        Idempotent: existing poll tasks are cancelled and existing boards closed
        before the registry is rebuilt. A board is instantiated but not
        connected here — the driver opens its port per read operation, so an
        offline board does not block startup.

        Args:
            settings: The authoritative settings to build from.

        Raises:
            ValueError: On a duplicate label, invalid serial settings, an
                unresolvable driver, or an instance that does not satisfy
                `CurrentAmplifierProtocol`.
        """
        await self.close_amplifiers()
        # The registry is built from these settings, so they are the effective
        # ones: the poll loop reads the failure threshold and the stream
        # retention from here rather than from the module defaults.
        self._settings = settings
        self._amplifiers = {}
        self._amplifier_settings = {}
        self._monitoring = {}
        self._connected = {}
        self._stats = {}

        seen: set[str] = set()
        for board_settings in settings.amplifiers:
            label = board_settings.label
            if label in seen:
                self.logger.error("Duplicate amplifier label %r in configuration", label)
                raise ValueError(f"Duplicate amplifier label {label!r}")
            seen.add(label)

            driver = resolve_driver(board_settings.driver)
            # The protocol declares no constructor, so cast to `Any` before the
            # dynamic instantiation — the concrete driver's `__init__` signature
            # is unknown statically and validated by the isinstance check below.
            kwargs: dict[str, Any] = {
                "con_params": self._connection_config(board_settings),
                "address": board_settings.address,
                "label": label,
                "logger": self.logger,
            }
            # Only pass the settle delay when configured and when the driver
            # takes it: a driver that declares no such parameter would raise
            # TypeError on construction.
            if board_settings.relay_settle_s is not None:
                if _accepts_keyword(driver, "relay_settle_s"):
                    kwargs["relay_settle_s"] = board_settings.relay_settle_s
                else:
                    self.logger.warning(
                        "Driver %r for amplifier %r does not accept relay_settle_s; "
                        "the configured %s s is ignored and the driver keeps its own default.",
                        board_settings.driver,
                        label,
                        board_settings.relay_settle_s,
                    )
            amplifier = cast(Any, driver)(**kwargs)
            if not isinstance(amplifier, CurrentAmplifierProtocol):
                self.logger.error(
                    "Driver %r for amplifier %r is not a CurrentAmplifierProtocol",
                    board_settings.driver,
                    label,
                )
                raise ValueError(f"Driver {board_settings.driver!r} for amplifier {label!r} is not a CurrentAmplifierProtocol")

            self._amplifiers[label] = amplifier
            self._amplifier_settings[label] = board_settings
            self._monitoring[label] = False
            self._connected[label] = False
            self._stats[label] = AmplifierStats()

    def _connection_config(self, board_settings: AmplifierSettings) -> ModbusSerialConnectionConfig:
        """Build the serial connection config for one board.

        The serial model validates its own field domains (baud rate, parity,
        framer, stop bits) and raises `ValueError` for an unsupported value;
        that error is re-raised with the board label so a bad config names the
        board instead of failing anonymously. `AmplifierSerialSettings.stopbits`
        is a float, while the serial model accepts only integer stop-bit counts
        and rejects an integral float as the wrong type, so an integral value is
        narrowed back to `int` first.
        """
        serial = board_settings.serial
        stopbits = int(serial.stopbits) if float(serial.stopbits).is_integer() else serial.stopbits
        try:
            return ModbusSerialConnectionConfig(
                port=serial.port,
                baudrate=serial.baudrate,
                bytesize=serial.bytesize,
                parity=serial.parity,
                stopbits=stopbits,
                timeout=serial.timeout,
                framer=serial.framer,
            )
        except ValueError as exc:
            raise ValueError(f"amplifier {board_settings.label!r}: invalid serial settings: {exc}") from exc

    async def close_amplifiers(self) -> None:
        """Cancel all poll tasks and close every board, swallowing close errors.

        Used by both ``cleanup`` and the idempotent rebuild in
        ``_build_amplifiers``. A close error is logged and swallowed so a
        teardown failure never aborts shutdown or a rebuild.
        """
        for label, task in list(self._poll_tasks.items()):
            task.cancel()
            self._monitoring[label] = False
            self._connected[label] = False
        self._poll_tasks.clear()
        for label, amplifier in list(self._amplifiers.items()):
            try:
                await amplifier.close()
            except Exception as exc:
                self.logger.warning("Failed to close amplifier %r: %s", label, exc)

    def _apply_settings(self, settings: CurrentAmplifierServiceSettings) -> None:
        """Apply merged current amplifier settings (sync hook, invoked inline).

        During startup the hook only stores: `initialize` builds the registry
        from the final merged settings. At runtime a change is only stored and
        flagged restart-required — the registry is not live-rebuilt because the
        framework's ``_restart_required_fields`` only inspects core settings, so
        this service tracks its own restart-required state.
        """
        self._settings = settings
        if not self._startup_complete:
            self.logger.info("Stored current amplifier settings for startup")
            return
        self.logger.warning(
            "Current amplifier settings applied; restart required to rebuild the amplifier registry "
            "(boards are not live-rebuilt)"
        )
        self._restart_required = True

    def _labels(self, label: str | None) -> list[str]:
        """Resolve a label selector to the list of affected labels.

        ``None`` selects all boards; an unknown label raises `KeyError`.
        """
        if label is None:
            return list(self._amplifiers)
        if label not in self._amplifiers:
            raise KeyError(f"Unknown amplifier label {label!r}")
        return [label]

    def start_monitoring(self, label: str | None = None) -> list[str]:
        """Start a poll task for the selected board(s), returning affected labels.

        ``label=None`` starts every board. A label that is already monitoring is
        a no-op and is not returned. Each started board gets one long-lived poll
        task scheduled on the running loop.
        """
        affected: list[str] = []
        for candidate in self._labels(label):
            if self._monitoring[candidate]:
                continue
            self._monitoring[candidate] = True
            self._poll_tasks[candidate] = asyncio.get_running_loop().create_task(self._poll_amplifier(candidate))
            affected.append(candidate)
        return affected

    def stop_monitoring(self, label: str | None = None) -> list[str]:
        """Cancel the poll task for the selected board(s), returning affected labels.

        ``label=None`` stops every board. The task is cancelled (sync — the loop
        is not awaited), removed from ``_poll_tasks``, and the label marked not
        monitoring. Stopping a board that is not monitoring is a no-op.
        """
        affected: list[str] = []
        for candidate in self._labels(label):
            task = self._poll_tasks.pop(candidate, None)
            if task is not None:
                task.cancel()
            if self._monitoring.get(candidate):
                self._monitoring[candidate] = False
                self._connected[candidate] = False
                affected.append(candidate)
        return affected

    async def _sleep(self, delay: float) -> None:
        """Sleep for ``delay``; a separate method so tests can short-circuit the poll loop."""
        await asyncio.sleep(delay)

    async def _poll_amplifier(self, label: str) -> None:
        """Poll one board forever: read, publish, repeat.

        The loop never dies on a board error: a read failure increments the
        board's error counters, logs at WARNING, and continues after the poll
        interval. A successful reading updates the stats and publishes a stream
        entry. The task is expected to be stopped by cancellation
        (``stop_monitoring``/``cleanup``), not by returning.

        There is no connect stage: the EBIC driver opens its serial port once
        per operation, so the loop reads directly.
        """
        amplifier = self._amplifiers.get(label)
        amplifier_settings = self._amplifier_settings.get(label)
        if amplifier is None or amplifier_settings is None:
            return
        while True:
            try:
                state = await amplifier.read_data()
            except Exception as exc:
                # `connected` means "the board is answering". A single failed
                # read is not enough to report the board as gone — an RS485
                # segment drops the occasional frame — so the flag clears only
                # after the configured number of failures in a row.
                stats = self._stats[label]
                stats.consecutive_failures += 1
                if stats.consecutive_failures >= self._failure_threshold():
                    self._connected[label] = False
                self._record_error(label, exc, "read")
                await self._sleep(amplifier_settings.poll_interval)
                continue

            self._connected[label] = True
            stats = self._stats[label]
            stats.consecutive_failures = 0
            stats.reading_count += 1
            now = datetime.now(timezone.utc)
            stats.last_state = state
            stats.last_reading_at = now
            stats.last_error = None
            await self._publish_reading(label, state, now)

            await self._sleep(amplifier_settings.poll_interval)

    def _record_error(self, label: str, exc: Exception, stage: str) -> None:
        """Increment a board's error counters and log a WARNING."""
        stats = self._stats[label]
        stats.error_count += 1
        stats.last_error = str(exc)
        self.logger.log(logging.WARNING, "Failed to %s amplifier %r: %s", stage, label, exc)

    async def _publish_reading(self, label: str, state: AmplifierState, now: datetime) -> None:
        """Publish one reading to the board's stream.

        The write is guarded on `self.client`; a glide error is logged at
        WARNING and reported to the transport health, mirroring the telemetry
        loop's error handling. ``filters`` is a tuple of structs, which ``xadd``
        cannot take as a scalar, so it is published as one JSON string.

        ``preamp_range`` and ``feedback_resistor_ohm`` carry the same value (the
        selected Rf in ohm): the API charts neither, but both the older and the
        newer tooltip spelling must decode.
        """
        client = self.client
        if client is None:
            return
        key = f"scietex:{self.service_name}:{self.instance_id}:current_amplifier:{label}:readings"
        try:
            await client.xadd(
                key,
                [
                    (b"ts", now.isoformat().encode()),
                    (b"output_voltage_v", encode_value(state.output_voltage_v)),
                    (b"output_current_a", encode_value(state.output_current_a)),
                    (b"input_current_a", encode_value(state.input_current_a)),
                    (b"boost_gain", encode_value(state.boost_gain)),
                    (b"feedback_resistor_ohm", encode_value(state.feedback_resistor_ohm)),
                    (b"preamp_range", encode_value(state.feedback_resistor_ohm)),
                    (b"dc_bias_level", encode_value(state.dc_bias_level)),
                    (b"bandwidth_hz", encode_value(state.bandwidth_hz)),
                    (b"input_compensation_level", encode_value(state.input_compensation_level)),
                    (b"dc_bias_enabled", encode_flag(state.dc_bias_enabled)),
                    (b"input_compensation_enabled", encode_flag(state.input_compensation_enabled)),
                    (b"input_overcurrent", encode_flag(state.input_overcurrent)),
                    (b"output_overload", encode_flag(state.output_overload)),
                    (b"inverted", encode_flag(state.inverted)),
                    (b"filters", msgspec.json.encode(list(state.filters))),
                    (b"transconductance_v_per_a", encode_value(self._transconductance(label, state))),
                ],
                StreamAddOptions(trim=TrimByMaxLen(exact=False, threshold=self._stream_maxlen())),
            )
        except (GlideConnectionError, RequestError, GlideTimeoutError) as exc:
            self.logger.log(logging.WARNING, "Failed to publish reading for amplifier %r: %s", label, exc)
            self._health.report_failure(exc)

    def _transconductance(self, label: str, state: AmplifierState) -> float | None:
        """``Rf * gain`` for one reading, or ``None`` when either is unknown."""
        feedback = state.feedback_resistor_ohm
        gain = state.boost_gain
        if feedback is None or gain is None:
            return None
        amplifier = self._amplifiers.get(label)
        if amplifier is None:
            return None
        try:
            return amplifier.capabilities.total_transconductance(feedback, gain)
        except ValueError:
            return None

    @Manager(name="Telemetry")
    async def telemetry_loop(self) -> None:
        """Publish service telemetry to Valkey once per iteration.

        The manager runtime repeats this body forever with auto-restart on error.
        The write is guarded on `self.client` and runs even when the worker is
        unconfigured, so the UI observes ``configured=false``. Glide errors are
        logged at WARNING and reported to the transport health, matching the
        heartbeat's error handling.
        """
        client = self.client
        if client is not None:
            payload = build_telemetry(self)
            try:
                await client.set(
                    self._telemetry_key,
                    value=self._telemetry_encoder.encode(payload),
                    expiry=ExpirySet(ExpiryType.SEC, TELEMETRY_TTL),
                )
            except (GlideConnectionError, RequestError, GlideTimeoutError) as exc:
                self.logger.log(logging.WARNING, "Failed to publish current amplifier telemetry: %s", exc)
                self._health.report_failure(exc)
        await asyncio.sleep(TELEMETRY_INTERVAL)


def encode_value(value: float | None) -> bytes:
    """Encode a reading value for the stream: ``b""`` for `None`, otherwise ``repr``.

    ``repr(float)`` round-trips exactly; the API re-parses and computes min/max,
    so truncation (e.g. ``.12g``) would bias every bucket. ``None`` encodes as an
    empty string so a board that does not report a field is distinguishable from
    one reporting zero.
    """
    return b"" if value is None else repr(value).encode()


def encode_flag(value: bool | None) -> bytes:
    """Encode a tri-state flag: ``b"1"``/``b"0"``, or ``b""`` for unknown.

    ``None`` means the driver cannot report the flag, which must stay
    distinguishable from a reported ``False``.
    """
    if value is None:
        return b""
    return b"1" if value else b"0"
