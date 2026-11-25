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

Every board owns its own RS485 port, so the serial connection settings live on
the board rather than on a shared gateway. Field domains are validated here as
well as by `ModbusSerialConnectionConfig` at build time: the duplicate check
turns a bad parity/framer/bytesize into a clean config-load error instead of a
registry construction failure. Validation raises `ValueError`, which
`msgspec.convert` reports as a `msgspec.ValidationError`.
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


class AmplifierSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Settings for a single current amplifier board driven by the service.

    ``label`` is the unique stream-key segment for the board and ``serial`` is
    its own RS485 connection. ``driver`` is the entry point name of a
    `scietex.hal.current_amplifier` driver resolved at runtime, e.g.
    ``ebic_modbus_v1``. ``address`` is the Modbus slave address (EBIC default
    2); every board on one segment needs a distinct one.

    ``relay_settle_s`` is optional and constructor-only: it is forwarded to the
    driver only when set and only if the driver accepts the keyword. ``None``
    leaves the driver's own default (8 ms) in place.
    """

    label: str
    serial: AmplifierSerialSettings
    driver: str = "ebic_modbus_v1"
    address: int = 2
    poll_interval: float = 1.0
    relay_settle_s: float | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.address <= 247:
            raise ValueError(f"amplifier {self.label!r}: address must be within 1..247, got {self.address}")
        if self.poll_interval <= 0:
            raise ValueError(f"amplifier {self.label!r}: poll_interval must be positive, got {self.poll_interval}")
        if self.relay_settle_s is not None and self.relay_settle_s < 0:
            raise ValueError(f"amplifier {self.label!r}: relay_settle_s must be non-negative, got {self.relay_settle_s}")


class CurrentAmplifierServiceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Top-level current amplifier service settings.

    ``failure_threshold`` and ``stream_maxlen`` are service-wide rather than
    per-board: the readings streams share one retention policy, and the noise
    tolerance is a property of the service rather than of a single board.
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
