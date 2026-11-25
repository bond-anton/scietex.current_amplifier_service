"""Shared helpers for the current amplifier service tests.

This is not a test module: the filename does not match pytest's default
collection patterns (``test_*.py`` / ``*_test.py``), so pytest will not collect
it.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import msgspec
from scietex.hal.current_amplifier.base.capabilities import (
    Access,
    BoostCapabilities,
    CompensationCapabilities,
    CurrentAmplifierCapabilities,
    DcBiasCapabilities,
    PreampCapabilities,
    SelectableCapabilities,
)
from scietex.hal.current_amplifier.base.data import AmplifierState
from scietex.hal.current_amplifier.base.protocol import CurrentAmplifierProtocol
from scietex.service.task_handler import TaskCapabilities, TaskData, TaskHandlerContext

from scietex.current_amplifier_service.config import (
    AmplifierDeviceSettings,
    AmplifierTransportSettings,
    CurrentAmplifierServiceSettings,
)

#: Entry point name the fake driver is advertised under by default, so a board
#: configured with the default driver resolves to `_FakeAmplifier`.
FAKE_DRIVER_NAME = "ebic_modbus_v1"

#: Entry point name of the fake Modbus/TCP driver, so a TCP board resolves to
#: `_FakeTcpAmplifier` through the same registry path as the serial fake.
FAKE_TCP_DRIVER_NAME = "ebic_modbus_tcp_v1"

#: The capability tree of the EBIC v1.0 board: the four implemented writes
#: (feedback resistor, boost gain, DC bias, input compensation), no filters, no
#: selectable bandwidth and no output inversion. `dc_bias.level` and
#: `input_compensation.level` stay read/write-off, matching the board.
FULL_CAPABILITIES = CurrentAmplifierCapabilities(
    preamp=PreampCapabilities(
        feedback_resistor=SelectableCapabilities(
            access=Access(read=True, write=True),
            options=(
                1_000.0,
                10_000.0,
                100_000.0,
                1_000_000.0,
                10_000_000.0,
                100_000_000.0,
                1_000_000_000.0,
                10_000_000_000.0,
            ),
        ),
        max_output_v=5.0,
    ),
    boost=BoostCapabilities(
        gain=SelectableCapabilities(
            access=Access(read=True, write=True),
            options=(1.0, 3.0, 10.0, 30.0),
        ),
        default_gain=1.0,
    ),
    dc_bias=DcBiasCapabilities(enabled=Access(read=True, write=True)),
    input_compensation=CompensationCapabilities(enabled=Access(read=True, write=True)),
)

#: The default tree: nothing is supported or readable, so every capability gate
#: is closed and every reported value stays unknown.
EMPTY_CAPABILITIES = CurrentAmplifierCapabilities()


class _FakeEntryPoint:
    """An entry point stand-in whose ``load`` returns a live object.

    A real `importlib.metadata.EntryPoint` resolves a dotted path, which cannot
    express an arbitrary factory object; the registry only calls ``name`` and
    ``load()``, so a stub covers both.
    """

    def __init__(self, name: str, factory: object) -> None:
        self.name = name
        self._factory = factory

    def load(self) -> object:
        return self._factory


def register_fake_driver(
    monkeypatch,
    name: str = FAKE_DRIVER_NAME,
    factory: object | None = None,
) -> None:
    """Advertise the fake drivers as the entry points in the HAL registry.

    With no ``factory`` both transports are servable: ``ebic_modbus_v1``
    resolves to `_FakeAmplifier` and ``ebic_modbus_tcp_v1`` to
    `_FakeTcpAmplifier`. Passing ``factory`` advertises that single class under
    ``name`` instead, for tests that need one specific constructor.

    The worker resolves drivers from installed distribution metadata, which a
    test module cannot contribute to. Patching the registry's ``entry_points``
    keeps the real resolution path under test without installing a throwaway
    distribution: the worker's imported ``resolve_driver`` looks ``entry_points``
    up in the registry module at call time, so it sees the patch.
    """
    from scietex.hal.current_amplifier import registry

    if factory is None:
        entries = [
            _FakeEntryPoint(name=FAKE_DRIVER_NAME, factory=_FakeAmplifier),
            _FakeEntryPoint(name=FAKE_TCP_DRIVER_NAME, factory=_FakeTcpAmplifier),
        ]
    else:
        entries = [_FakeEntryPoint(name=name, factory=factory)]

    def fake_entry_points(**_: object) -> list[_FakeEntryPoint]:
        return entries

    monkeypatch.setattr(registry, "entry_points", fake_entry_points)


class _FakeAmplifier(CurrentAmplifierProtocol):
    """A `CurrentAmplifierProtocol` fake recording every control call.

    Subclassing the protocol keeps the capability properties and the
    ``NotImplementedError`` stubs of the controls the EBIC board does not
    implement; only the four implemented writes are overridden. ``capabilities``
    defaults to `FULL_CAPABILITIES` and may be shadowed per instance through the
    ``capabilities`` keyword. ``state`` is the snapshot `read_data` returns;
    ``read_exc`` makes it raise and ``control_exc`` makes every setter raise.
    """

    capabilities = FULL_CAPABILITIES
    display_name = "Fake current amplifier"

    def __init__(
        self,
        con_params=None,
        address: int = 2,
        label: str = "EBIC",
        logger: logging.Logger | None = None,
        *,
        relay_driver=None,
        relay_settle_s: float = 0.008,
        capabilities: CurrentAmplifierCapabilities | None = None,
    ) -> None:
        self.con_params = con_params
        self.address = address
        self.label = label
        self.logger = logger
        self.relay_driver = relay_driver
        self.relay_settle_s = relay_settle_s
        if capabilities is not None:
            self.capabilities = capabilities
        self.connect_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self.state = AmplifierState(
            output_voltage_v=2.5,
            output_current_a=2.5e-3,
            input_current_a=2.5e-6,
            boost_gain=1.0,
            feedback_resistor_ohm=1_000_000.0,
            dc_bias_enabled=False,
            dc_bias_level=0.0,
            bandwidth_hz=None,
            filters=(),
            input_overcurrent=False,
            output_overload=False,
            input_compensation_enabled=False,
            input_compensation_level=0.0,
            inverted=False,
        )
        self.read_exc: Exception | None = None
        self.control_exc: Exception | None = None
        self.feedback_resistor_ohm = 1_000_000.0
        self.boost_gain = 1.0
        self.dc_bias_enabled = False
        self.input_compensation_enabled = False
        self.select_feedback_resistor_calls: list[float] = []
        self.set_boost_gain_calls: list[float] = []
        self.set_dc_bias_calls: list[bool] = []
        self.set_input_compensation_calls: list[bool] = []

    def _raise_control(self) -> None:
        if self.control_exc is not None:
            raise self.control_exc

    async def connect(self) -> None:
        self.connect_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def read_data(self) -> AmplifierState:
        self.read_calls += 1
        if self.read_exc is not None:
            raise self.read_exc
        return self.state

    async def get_feedback_resistor(self) -> float:
        return self.feedback_resistor_ohm

    async def select_feedback_resistor(self, resistance_ohm: float) -> None:
        self.select_feedback_resistor_calls.append(resistance_ohm)
        self._raise_control()
        self.feedback_resistor_ohm = resistance_ohm

    async def get_boost_gain(self) -> float:
        return self.boost_gain

    async def set_boost_gain(self, gain: float) -> None:
        self.set_boost_gain_calls.append(gain)
        self._raise_control()
        self.boost_gain = gain

    async def get_dc_bias(self) -> bool:
        return self.dc_bias_enabled

    async def set_dc_bias(self, enabled: bool) -> None:
        self.set_dc_bias_calls.append(enabled)
        self._raise_control()
        self.dc_bias_enabled = enabled

    async def get_input_compensation(self) -> bool:
        return self.input_compensation_enabled

    async def set_input_compensation(self, enabled: bool) -> None:
        self.set_input_compensation_calls.append(enabled)
        self._raise_control()
        self.input_compensation_enabled = enabled


class _FakeTcpAmplifier(_FakeAmplifier):
    """A `CurrentAmplifierProtocol` fake for a board behind a Modbus/TCP gateway.

    Mirrors the HAL TCP driver's constructor: the endpoint and the per-request
    timeout/retry knobs replace the serial connection config, which stays
    `None`. Behaviour is inherited, so a TCP board polls exactly like a serial
    one.
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        address: int = 2,
        label: str = "EBIC",
        timeout: float = 6.0,
        retries: int = 0,
        logger: logging.Logger | None = None,
        *,
        relay_settle_s: float = 0.008,
    ) -> None:
        super().__init__(
            con_params=None,
            address=address,
            label=label,
            logger=logger,
            relay_settle_s=relay_settle_s,
        )
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = retries


def make_serial_transport(port: str = "/dev/ttyUSB0", **overrides: object) -> AmplifierTransportSettings:
    """Build a valid RS485 transport; the defaults are the plugin's EBIC ones."""
    return AmplifierTransportSettings(port=port, **overrides)


def make_tcp_transport(host: str = "127.0.0.1", **overrides: object) -> AmplifierTransportSettings:
    """Build a valid Modbus/TCP transport pointing at a gateway."""
    return AmplifierTransportSettings(kind="tcp", host=host, **overrides)


def make_device_settings(
    *,
    driver: str = FAKE_DRIVER_NAME,
    address: int = 1,
    label: str | None = None,
    poll: bool = True,
    poll_interval: float = 1.0,
    relay_settle_s: float | None = None,
) -> AmplifierDeviceSettings:
    """Build one device entry; the address defaults to the plugin's value."""
    return AmplifierDeviceSettings(
        driver=driver,
        address=address,
        label=label,
        poll=poll,
        poll_interval=poll_interval,
        relay_settle_s=relay_settle_s,
    )


def make_settings(
    devices: dict[str, AmplifierDeviceSettings] | None = None,
    *,
    transport: AmplifierTransportSettings | None = None,
    poll_enabled: bool = True,
    failure_threshold: int = 3,
    stream_maxlen: int = 10000,
) -> CurrentAmplifierServiceSettings:
    """Build service settings around the given device map (empty by default)."""
    return CurrentAmplifierServiceSettings(
        transport=transport if transport is not None else make_serial_transport(),
        devices=dict(devices) if devices is not None else {},
        poll_enabled=poll_enabled,
        failure_threshold=failure_threshold,
        stream_maxlen=stream_maxlen,
    )


def _make_context() -> TaskHandlerContext:
    """A narrow handler context with fixed service identity and a test logger."""
    return TaskHandlerContext(service_name="test", instance_id="inst", logger=logging.getLogger("test"))


def _make_capabilities() -> TaskCapabilities:
    """Per-call capabilities with a no-op progress writer (mirrors modbus tests)."""

    async def _noop(_task_id, _value) -> None:
        return None

    return TaskCapabilities(task_id=uuid4(), _write_progress=_noop)


def _make_task(task_name: str, payload: dict | None) -> TaskData:
    """A ``TaskData`` whose payload is JSON-encoded from ``payload`` (``None`` -> empty)."""
    return TaskData(
        task_id=str(uuid4()),
        task=task_name,
        payload=msgspec.json.encode(payload) if payload is not None else b"",
    )
