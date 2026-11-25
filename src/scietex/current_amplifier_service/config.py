"""Configuration models and YAML loader for the current amplifier service.

`CurrentAmplifierServiceSettings` is the concrete constructor (L0) base and the
validation target for the service's remote configuration. The amplifier list is
a field-level map merged onto L0 and overlaid by the remote `current_amplifier`
section (L3).

This module is deliberately **not** registered as an L1 bootstrap provider. The
worker reads `current_amplifier.yml` directly at startup (before
`super().initialize()`) and uses it only to seed the initial in-memory amplifier
list; the config section is registered with `bootstrap=None`, so L1 never enters
the layer stack and the remote L3 config cleanly replaces it. Trade-off:
`config:show` will not display it as a layer.

A board is reached either over its own RS485 port (``transport: rs485``, the
default) or through a Modbus/TCP gateway (``transport: tcp``), so the
transport-specific connection settings live on the board. Exactly one of the
``serial`` / ``tcp`` blocks is set, and it must agree with ``transport`` and with
the driver family. Field domains are validated here as well as by
`ModbusSerialConnectionConfig` at build time: the duplicate check turns a bad
parity/framer/bytesize into a clean config-load error instead of a registry
construction failure. Validation raises `ValueError`, which `msgspec.convert`
reports as a `msgspec.ValidationError`.
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


class AmplifierSerialSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Serial connection settings for one Modbus RTU segment.

    Maps onto `scietex.hal.serial.ModbusSerialConnectionConfig` at registry
    construction time. Defaults follow the EBIC v1.0 firmware: 19200 baud, 8N1
    with RTU framing.
    """

    port: str
    baudrate: int = 19200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    timeout: float = 1.0
    framer: str = "RTU"

    def __post_init__(self) -> None:
        if self.baudrate <= 0:
            raise ValueError(f"serial.baudrate must be positive, got {self.baudrate}")
        if self.bytesize not in (5, 6, 7, 8):
            raise ValueError(f"serial.bytesize must be one of 5, 6, 7, 8, got {self.bytesize}")
        if self.parity not in ("N", "E", "O"):
            raise ValueError(f"serial.parity must be one of N, E, O, got {self.parity!r}")
        if self.stopbits not in (1, 1.5, 2):
            raise ValueError(f"serial.stopbits must be one of 1, 1.5, 2, got {self.stopbits}")
        if self.timeout < 0:
            raise ValueError(f"serial.timeout must be non-negative, got {self.timeout}")
        if self.framer not in ("RTU", "ASCII"):
            raise ValueError(f"serial.framer must be RTU or ASCII, got {self.framer!r}")


class AmplifierTcpSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Modbus/TCP gateway endpoint for one board.

    ``host`` is the gateway; the Modbus ``address`` still selects the board
    behind it, so several boards may share one ``host:port``.

    ``timeout`` is the per-request client timeout. It must *exceed* the
    gateway's published ``client_timeout_floor``, not merely the device response
    time: the gateway may hold the RS485 bus through reconnect and retry work
    before it answers or gives up. The old reasoning — a budget of
    ``lock_wait + hold`` — was insufficient because it ignored the gateway's
    reconnect, per-retry and resync overhead.

    With ``t = serial.timeout``, ``r = bus_retries`` and ``q = bus_queue_depth``
    the gateway sizes its timeouts as::

        B = t + 0.1 + (r + 1) * t          # bus_timeout
        s = 2 * t + 0.1                    # resync overhead
        request_timeout      = (B + s) * (q + 1) + t
        client_timeout_floor = request_timeout + B + s

    The 30.0 s default is sized for ``serial.timeout: 1.0``, ``bus_retries: 0``
    and ``bus_queue_depth: 4`` (floor 26.2 s), leaving margin. A gateway left at
    the 3.0 s serial fallback (``q=4`` → floor 76.2 s) needs roughly 80 s, which
    is why the README recommends an explicit ``serial.timeout``; a value below
    the floor reports normal bus contention as an unreachable board.

    ``retries`` is pymodbus's per-request retry count. Zero keeps an ambiguous
    write from being sent twice: a retry after a timeout re-sends a request whose
    first copy may still be executing.
    """

    host: str
    port: int = 502
    timeout: float = 30.0
    retries: int = 0

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("tcp.host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError(f"tcp.port must be within 1..65535, got {self.port}")
        if self.timeout <= 0:
            raise ValueError(f"tcp.timeout must be positive, got {self.timeout}")
        if self.retries < 0:
            raise ValueError(f"tcp.retries must be non-negative, got {self.retries}")


#: Transport each known driver family speaks, matched by entry point prefix.
#: A driver outside this map declares no transport, so it may be paired with
#: either; only a known family that contradicts the configured transport is an
#: error. The TCP prefix is listed first: it does not share a prefix with the
#: RTU family, but an explicit order keeps the lookup unambiguous.
DRIVER_TRANSPORT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ebic_modbus_tcp_v1", "tcp"),
    ("ebic_modbus_v1", "rs485"),
)

#: The transports a board may use.
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


class AmplifierSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Settings for a single current amplifier board driven by the service.

    ``label`` is the unique stream-key segment for the board. ``transport``
    selects how the board is reached and decides which connection block is
    required: ``rs485`` needs ``serial`` (the board's own RS485 port), ``tcp``
    needs ``tcp`` (a Modbus/TCP gateway endpoint). Exactly one block is set, and
    it must agree with ``transport`` and with the driver family — see
    ``DRIVER_TRANSPORT_PREFIXES``.

    ``driver`` is the entry point name of a `scietex.hal.current_amplifier`
    driver resolved at runtime, e.g. ``ebic_modbus_v1``. ``address`` is the
    Modbus slave address (EBIC default 2); every board on one segment — or
    behind one gateway — needs a distinct one.

    ``relay_settle_s`` is optional and constructor-only: it is forwarded to the
    driver only when set and only if the driver accepts the keyword. ``None``
    leaves the driver's own default (8 ms) in place.
    """

    label: str
    serial: AmplifierSerialSettings | None = None
    tcp: AmplifierTcpSettings | None = None
    transport: str = "rs485"
    driver: str = "ebic_modbus_v1"
    address: int = 2
    poll_interval: float = 1.0
    relay_settle_s: float | None = None

    def __post_init__(self) -> None:
        if self.transport not in TRANSPORTS:
            raise ValueError(
                f"amplifier {self.label!r}: transport must be one of {', '.join(TRANSPORTS)}, got {self.transport!r}"
            )
        if (self.serial is None) == (self.tcp is None):
            raise ValueError(
                f"amplifier {self.label!r}: exactly one of 'serial' or 'tcp' must be set, "
                f"got serial={'set' if self.serial is not None else 'unset'}, "
                f"tcp={'set' if self.tcp is not None else 'unset'}"
            )
        if self.transport == "tcp":
            if self.tcp is None:
                raise ValueError(f"amplifier {self.label!r}: transport 'tcp' requires the 'tcp' block, not 'serial'")
        elif self.serial is None:
            raise ValueError(f"amplifier {self.label!r}: transport 'rs485' requires the 'serial' block, not 'tcp'")
        driver_transport = transport_for_driver(self.driver)
        if driver_transport is not None and driver_transport != self.transport:
            raise ValueError(
                f"amplifier {self.label!r}: driver {self.driver!r} is a {driver_transport} driver "
                f"but transport is {self.transport!r}"
            )
        if not 1 <= self.address <= 247:
            raise ValueError(f"amplifier {self.label!r}: address must be within 1..247, got {self.address}")
        if self.poll_interval <= 0:
            raise ValueError(f"amplifier {self.label!r}: poll_interval must be positive, got {self.poll_interval}")
        if self.relay_settle_s is not None and self.relay_settle_s < 0:
            raise ValueError(f"amplifier {self.label!r}: relay_settle_s must be non-negative, got {self.relay_settle_s}")

    def serial_block(self) -> AmplifierSerialSettings:
        """The RS485 block, narrowed to non-`None`.

        Raises:
            ValueError: If the board is not configured with a ``serial`` block.
        """
        if self.serial is None:
            raise ValueError(f"amplifier {self.label!r}: no 'serial' block configured")
        return self.serial

    def tcp_block(self) -> AmplifierTcpSettings:
        """The Modbus/TCP block, narrowed to non-`None`.

        Raises:
            ValueError: If the board is not configured with a ``tcp`` block.
        """
        if self.tcp is None:
            raise ValueError(f"amplifier {self.label!r}: no 'tcp' block configured")
        return self.tcp


class CurrentAmplifierServiceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Top-level current amplifier service settings.

    ``failure_threshold`` and ``stream_maxlen`` are service-wide rather than
    per-board: the readings streams share one retention policy, and the noise
    tolerance is a property of the service rather than of a single board.

    A board's identity is its endpoint plus its Modbus address. For RS485 that
    is ``(serial.port, address)``: a port may carry several boards as long as
    each has its own address. For TCP it is ``(tcp.host, tcp.port, address)``:
    several boards legitimately share one gateway and differ only by address.
    A duplicate pair is rejected, duplicate endpoints are not.
    """

    amplifiers: list[AmplifierSettings] = msgspec.field(default_factory=list)
    #: Consecutive failed reads before a board is reported as disconnected.
    #: One failed read is common on an RS485 segment, so a single glitch must
    #: not flip the status icon.
    failure_threshold: int = 3
    #: Entries retained per board's readings stream.
    stream_maxlen: int = 10000

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError(f"failure_threshold must be at least 1, got {self.failure_threshold}")
        if self.stream_maxlen < 1:
            raise ValueError(f"stream_maxlen must be at least 1, got {self.stream_maxlen}")
        self._validate_endpoints()

    def _validate_endpoints(self) -> None:
        """Reject two boards that address the same device.

        The comparison trims the host and the serial path and case-folds the
        host, because DNS names are case-insensitive and a padded path is a
        typo, not a second device. Deeper equivalence is deliberately not
        attempted: a host alias, an IPv4 literal versus its name, and a serial
        symlink (``/dev/serial/by-id/...``) versus its target each still pass,
        since resolving them would put DNS or filesystem I/O in config
        validation.

        Raises:
            ValueError: On a duplicate ``(port, address)`` for an RS485 board or
                a duplicate ``(host, port, address)`` for a TCP board.
        """
        serial_claims: dict[tuple[str, int], str] = {}
        tcp_claims: dict[tuple[str, int, int], str] = {}
        for board in self.amplifiers:
            if board.transport == "tcp":
                tcp = board.tcp_block()
                key_tcp = (tcp.host.strip().casefold(), tcp.port, board.address)
                owner = tcp_claims.get(key_tcp)
                if owner is not None:
                    raise ValueError(
                        f"amplifiers {owner!r} and {board.label!r} share the same Modbus/TCP endpoint "
                        f"{tcp.host}:{tcp.port} with address {board.address}"
                    )
                tcp_claims[key_tcp] = board.label
            else:
                serial = board.serial_block()
                key_serial = (serial.port.strip(), board.address)
                owner = serial_claims.get(key_serial)
                if owner is not None:
                    raise ValueError(
                        f"amplifiers {owner!r} and {board.label!r} share serial port {serial.port!r} "
                        f"with address {board.address}"
                    )
                serial_claims[key_serial] = board.label


#: Concrete L0 base for the service's configuration merge. A field absent from
#: every layer, or explicitly cleared with ``null``, resolves to the value here.
CURRENT_AMPLIFIER_SETTINGS_DEFAULTS: CurrentAmplifierServiceSettings = CurrentAmplifierServiceSettings()


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
            settings = CurrentAmplifierServiceSettings()
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
