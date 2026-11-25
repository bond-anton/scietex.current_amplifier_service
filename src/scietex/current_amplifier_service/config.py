"""Configuration models and YAML loader for the current amplifier service.

`CurrentAmplifierServiceSettings` is the concrete constructor (L0) base and the
validation target for the service's remote configuration. Devices are a map
keyed by the worker device id; the map is merged onto L0 field-by-field and
overlaid by the remote `current_amplifier` section (L3).

This module is deliberately **not** registered as an L1 bootstrap provider. The
worker reads `current_amplifier.yml` directly at startup (before
`super().initialize()`) and uses it only to seed the initial in-memory device
map; the config section is registered with `bootstrap=None`, so L1 never enters
the layer stack and the remote L3 config cleanly replaces it. Trade-off:
`config:show` will not display it as a layer.

A service reaches its segment either over one RS485 port (``kind: rs485``, the
default) or through one Modbus/TCP gateway (``kind: tcp``). The transport is
service-wide, not per-device: one row describes one segment, and the Modbus
slave ``address`` selects the board behind it. Field domains are validated here
as well as by `ModbusSerialConnectionConfig` at build time, so a bad
parity/framer/bytesize fails at config load instead of during registry
construction. Validation raises `ValueError`, which `msgspec.convert` reports as
a `msgspec.ValidationError`.
"""

from pathlib import Path

import msgspec

#: Remote-config section name the settings are registered under.
CURRENT_AMPLIFIER_SECTION: str = "current_amplifier"

#: Subdirectory under the shared config dir that namespaces this service's
#: files. The service-owned ``current_amplifier.yml`` lives here, so services
#: sharing one config dir cannot collide.
CURRENT_AMPLIFIER_CONFIG_SUBDIR: str = "current_amplifier"

#: Filename of the service-owned YAML config file in the config subdirectory.
CURRENT_AMPLIFIER_CONFIG_FILE: str = "current_amplifier.yml"


class AmplifierTransportSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """How the service reaches its segment: one RS485 port or one Modbus/TCP gateway.

    Field-for-field the API plugin's ``CurrentAmplifierWorkerTransport``.
    ``kind`` defaults to ``"rs485"`` so a payload that predates the
    discriminator still decodes as a direct serial segment. ``port`` is the
    serial device path and never a TCP port; the gateway port is ``tcp_port``.

    ``tcp_timeout`` is a separate field from ``timeout`` on purpose. ``timeout``
    is the serial per-response wait (default 1.0 s), while the TCP client
    timeout must *exceed* the gateway's published ``client_timeout_floor`` — it
    may hold the RS485 bus through reconnect and retry work before it answers or
    gives up. With ``t = serial.timeout``, ``r = bus_retries`` and
    ``q = bus_queue_depth`` the gateway sizes its timeouts as::

        B = t + 0.1 + (r + 1) * t          # bus_timeout
        s = 2 * t + 0.1                    # resync overhead
        request_timeout      = (B + s) * (q + 1) + t
        client_timeout_floor = request_timeout + B + s

    The 30.0 s default is sized for ``serial.timeout: 1.0``, ``bus_retries: 0``
    and ``bus_queue_depth: 4`` (floor 26.2 s), leaving margin. Sharing
    ``timeout`` would silently install a 1.0 s client timeout against that
    26.2 s floor and report normal bus contention as an unreachable board.

    ``tcp_retries`` is pymodbus's per-request retry count. Zero keeps an
    ambiguous write from being sent twice: a retry after a timeout re-sends a
    request whose first copy may still be executing.
    """

    kind: str = "rs485"
    # serial
    port: str | None = None
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    timeout: float = 1.0
    write_timeout: float | None = None
    inter_byte_timeout: float | None = None
    framer: str = "RTU"
    # tcp
    host: str | None = None
    tcp_port: int = 502
    tcp_timeout: float = 30.0
    tcp_retries: int = 0

    def __post_init__(self) -> None:
        if self.kind not in TRANSPORTS:
            raise ValueError(f"transport.kind must be one of {', '.join(TRANSPORTS)}, got {self.kind!r}")
        if self.baudrate <= 0:
            raise ValueError(f"transport.baudrate must be positive, got {self.baudrate}")
        if self.bytesize not in (5, 6, 7, 8):
            raise ValueError(f"transport.bytesize must be one of 5, 6, 7, 8, got {self.bytesize}")
        if self.parity not in ("N", "E", "O"):
            raise ValueError(f"transport.parity must be one of N, E, O, got {self.parity!r}")
        if self.stopbits not in (1, 1.5, 2):
            raise ValueError(f"transport.stopbits must be one of 1, 1.5, 2, got {self.stopbits}")
        if self.timeout < 0:
            raise ValueError(f"transport.timeout must be non-negative, got {self.timeout}")
        if self.write_timeout is not None and self.write_timeout < 0:
            raise ValueError(f"transport.write_timeout must be non-negative, got {self.write_timeout}")
        if self.inter_byte_timeout is not None and self.inter_byte_timeout < 0:
            raise ValueError(f"transport.inter_byte_timeout must be non-negative, got {self.inter_byte_timeout}")
        if self.framer not in ("RTU", "ASCII"):
            raise ValueError(f"transport.framer must be RTU or ASCII, got {self.framer!r}")
        if self.kind == "rs485":
            if not self.port:
                raise ValueError("transport 'rs485' requires 'port'")
            if self.host is not None:
                raise ValueError("transport 'rs485' must not set 'host'")
        else:
            if not self.host:
                raise ValueError("transport 'tcp' requires 'host'")
            if self.port is not None:
                raise ValueError("transport 'tcp' must not set 'port'")
            if not 1 <= self.tcp_port <= 65535:
                raise ValueError(f"transport.tcp_port must be within 1..65535, got {self.tcp_port}")
            if self.tcp_timeout <= 0:
                raise ValueError(f"transport.tcp_timeout must be positive, got {self.tcp_timeout}")
            if self.tcp_retries < 0:
                raise ValueError(f"transport.tcp_retries must be non-negative, got {self.tcp_retries}")


#: Transport each known driver family speaks, matched by entry point prefix.
#: A driver outside this map declares no transport, so it may be paired with
#: either; only a known family that contradicts the configured transport is an
#: error. The TCP prefix is listed first: it does not share a prefix with the
#: RTU family, but an explicit order keeps the lookup unambiguous.
DRIVER_TRANSPORT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ebic_modbus_tcp_v1", "tcp"),
    ("ebic_modbus_v1", "rs485"),
)

#: The transports a service may use.
TRANSPORTS: tuple[str, ...] = ("rs485", "tcp")


def transport_for_driver(driver: str) -> str | None:
    """The transport a driver's entry point name belongs to, or `None` if unknown.

    Args:
        driver: Entry point name of a `scietex.hal.current_amplifier` driver.

    Returns:
        str | None: ``"rs485"`` or ``"tcp"`` for a known family; `None` when the
        name matches no known family, in which case it cannot contradict the
        configured transport.
    """
    for prefix, transport in DRIVER_TRANSPORT_PREFIXES:
        if driver.startswith(prefix):
            return transport
    return None


class AmplifierDeviceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Settings for one current amplifier board driven by the service.

    The map key is the device identity and the key segment of the board's
    readings stream; ``label`` is a display string only. ``driver`` is the entry
    point name of a `scietex.hal.current_amplifier` driver resolved at runtime,
    e.g. ``ebic_modbus_v1``; it must speak the service-wide transport.

    ``address`` is the Modbus slave address and identifies the board on the
    shared segment, so it defaults to the plugin's value and no two devices may
    share one.

    ``poll`` excludes a device from the poll plan; it stays readable on demand.
    ``relay_settle_s`` is worker-only and constructor-only: it is forwarded to
    the driver only when set and only if the driver accepts the keyword. ``None``
    leaves the driver's own default (8 ms) in place.
    """

    driver: str = "ebic_modbus_v1"
    address: int = 1
    label: str | None = None
    poll: bool = True
    poll_interval: float = 1.0
    relay_settle_s: float | None = None


class CurrentAmplifierServiceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Top-level current amplifier service settings.

    ``name`` is carried by the plugin's payload and accepted for compatibility;
    the instance name is framework-owned and the field is ignored.

    ``devices`` is keyed by the worker device id. The transport is service-wide,
    so the Modbus ``address`` alone identifies a board and a duplicate is
    rejected. A device id must be non-empty: it is a stream-key segment.

    ``poll_enabled`` gates the poll plan as a whole; ``failure_threshold`` and
    ``stream_maxlen`` are service-wide because the readings streams share one
    retention policy and the noise tolerance is a property of the service.
    """

    transport: AmplifierTransportSettings
    name: str | None = None
    devices: dict[str, AmplifierDeviceSettings] = msgspec.field(default_factory=dict)
    #: Whether any device is polled at all. A device is additionally excluded
    #: by its own ``poll`` flag.
    poll_enabled: bool = True
    #: Consecutive failed reads before a board is reported as disconnected.
    #: One failed read is common on an RS485 segment, so a single glitch must
    #: not flip the status icon.
    failure_threshold: int = 3
    #: Entries retained per device's readings stream.
    stream_maxlen: int = 10000

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError(f"failure_threshold must be at least 1, got {self.failure_threshold}")
        if self.stream_maxlen < 1:
            raise ValueError(f"stream_maxlen must be at least 1, got {self.stream_maxlen}")
        self._validate_devices()

    def _validate_devices(self) -> None:
        """Validate device ids and reject two devices addressing the same board.

        Raises:
            ValueError: On an empty device id, an address outside 1..247, a
                non-positive ``poll_interval``, a negative ``relay_settle_s``, or
                two devices sharing one Modbus address.
        """
        owners: dict[int, str] = {}
        for device_id, device in self.devices.items():
            if not device_id:
                raise ValueError("device id must not be empty")
            if not 1 <= device.address <= 247:
                raise ValueError(f"device {device_id!r}: address must be within 1..247, got {device.address}")
            if device.poll_interval <= 0:
                raise ValueError(f"device {device_id!r}: poll_interval must be positive, got {device.poll_interval}")
            if device.relay_settle_s is not None and device.relay_settle_s < 0:
                raise ValueError(f"device {device_id!r}: relay_settle_s must be non-negative, got {device.relay_settle_s}")
            owner = owners.get(device.address)
            if owner is not None:
                raise ValueError(
                    f"devices {owner!r} and {device_id!r} share Modbus address {device.address} on the same transport"
                )
            owners[device.address] = device_id


#: Concrete L0 base for the service's configuration merge. A field absent from
#: every layer, or explicitly cleared with ``null``, resolves to the value here.
#: The placeholder port is the documented HAL example.
CURRENT_AMPLIFIER_SETTINGS_DEFAULTS: CurrentAmplifierServiceSettings = CurrentAmplifierServiceSettings(
    transport=AmplifierTransportSettings(port="/dev/ttyUSB0")
)


def read_current_amplifier_config(conf_dir: Path | None, *, create_default: bool = True) -> dict[str, object]:
    """Read the service config from ``current_amplifier/current_amplifier.yml``.

    The service's files are namespaced in a ``current_amplifier/`` subdirectory
    so they do not collide with other services sharing the framework's single
    config dir. The file (and, when missing, its directory) is only created when
    ``create_default=True`` (the bootstrap path). A ``None`` or non-directory
    ``conf_dir``, a missing file/directory with ``create_default=False``, or an
    unparseable file each raise `RuntimeError`. An existing-but-invalid file is
    left untouched regardless of ``create_default``.

    The return value is the raw ``current_amplifier.yml`` mapping (an
    L1-style patch). When the file is just created, the builtins of the default
    struct are returned so the patch is equivalent to an empty override.

    Args:
        conf_dir: Path to the configuration directory.
        create_default: Whether to create the directory and write a default
            ``current_amplifier.yml`` when missing. Default ``True``.

    Returns:
        The parsed ``current_amplifier.yml`` mapping as a dict, or the builtins
        of the default `CurrentAmplifierServiceSettings` when the file was just
        created.

    Raises:
        RuntimeError: If ``conf_dir`` is ``None`` or not a directory, the file
            is missing with ``create_default=False``, or the file cannot be
            parsed.
    """
    if not isinstance(conf_dir, Path):
        raise RuntimeError("Configuration dir was not set!")
    service_dir = conf_dir / CURRENT_AMPLIFIER_CONFIG_SUBDIR
    if not service_dir.exists():
        if create_default:
            try:
                service_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                raise RuntimeError(f"Failed to create configuration directory {service_dir}!") from exc
        else:
            raise RuntimeError(
                f"Configuration directory {service_dir} does not exist and create_default=False (no default generated)."
            )
    elif not service_dir.is_dir():
        raise RuntimeError(f"Provided configuration directory path {service_dir} is not a directory!")
    amplifier_yml = service_dir.joinpath(CURRENT_AMPLIFIER_CONFIG_FILE)
    if not amplifier_yml.exists():
        if create_default:
            settings = CURRENT_AMPLIFIER_SETTINGS_DEFAULTS
            with open(amplifier_yml, "wb") as f:
                f.write(msgspec.yaml.encode(settings))
            return msgspec.to_builtins(settings)
        raise RuntimeError(
            f"Current amplifier configuration file {amplifier_yml} does not exist and create_default=False "
            "(pass create_default=True to generate defaults)."
        )
    try:
        with open(amplifier_yml, "rb") as f:
            patch = msgspec.yaml.decode(f.read(), type=dict)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to parse current amplifier configuration file {amplifier_yml}. "
            "Fix the file or remove it to regenerate defaults."
        ) from exc
    return patch
