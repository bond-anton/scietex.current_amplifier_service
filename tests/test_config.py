"""Tests for the current amplifier service configuration models and loader."""

from pathlib import Path

import msgspec
import pytest

from scietex.current_amplifier_service.config import (
    CURRENT_AMPLIFIER_CONFIG_FILE,
    CURRENT_AMPLIFIER_CONFIG_SUBDIR,
    CURRENT_AMPLIFIER_SETTINGS_DEFAULTS,
    AmplifierSerialSettings,
    AmplifierSettings,
    CurrentAmplifierServiceSettings,
    read_current_amplifier_config,
)


def _config_path(conf_dir: Path) -> Path:
    """The namespaced path the loader reads and writes."""
    return conf_dir / CURRENT_AMPLIFIER_CONFIG_SUBDIR / CURRENT_AMPLIFIER_CONFIG_FILE


def _board(**overrides: object) -> dict[str, object]:
    """A minimal valid board payload with optional top-level overrides."""
    payload: dict[str, object] = {"label": "board", "serial": {"port": "/dev/ttyUSB0"}}
    payload.update(overrides)
    return payload


def test_yaml_roundtrip_preserves_amplifier_list() -> None:
    """Board entries survive a YAML round-trip preserving every field."""
    settings = CurrentAmplifierServiceSettings(
        amplifiers=[
            AmplifierSettings(
                label="photodiode_a",
                serial=AmplifierSerialSettings(port="/dev/ttyUSB0", baudrate=19200, parity="E", stopbits=1.5),
                address=5,
                poll_interval=0.5,
                relay_settle_s=0.008,
            )
        ],
        failure_threshold=2,
        stream_maxlen=500,
    )

    decoded = msgspec.yaml.decode(msgspec.yaml.encode(settings), type=CurrentAmplifierServiceSettings)

    board = decoded.amplifiers[0]
    assert board.label == "photodiode_a"
    assert board.driver == "ebic_modbus_v1"
    assert board.address == 5
    assert board.poll_interval == 0.5
    assert board.relay_settle_s == 0.008
    assert board.serial.port == "/dev/ttyUSB0"
    assert board.serial.baudrate == 19200
    assert board.serial.parity == "E"
    assert board.serial.stopbits == 1.5
    assert decoded.failure_threshold == 2
    assert decoded.stream_maxlen == 500


def test_explicit_yaml_parses_two_boards() -> None:
    """Two boards on different ports and addresses parse with their own values."""
    yaml_text = """
amplifiers:
  - label: photodiode_a
    driver: ebic_modbus_v1
    address: 2
    poll_interval: 1.0
    serial:
      port: /dev/ttyUSB0
      baudrate: 19200
      bytesize: 8
      parity: N
      stopbits: 1
      timeout: 1.0
      framer: RTU
  - label: ion_collector_b
    address: 3
    poll_interval: 2.0
    serial:
      port: /dev/ttyUSB1
failure_threshold: 2
stream_maxlen: 500
"""

    settings = msgspec.yaml.decode(yaml_text, type=CurrentAmplifierServiceSettings)

    first, second = settings.amplifiers
    assert first.label == "photodiode_a"
    assert first.address == 2
    assert first.serial.port == "/dev/ttyUSB0"
    assert first.serial.framer == "RTU"
    assert second.label == "ion_collector_b"
    assert second.address == 3
    assert second.poll_interval == 2.0
    assert second.serial.port == "/dev/ttyUSB1"
    assert second.serial.baudrate == 19200
    assert second.relay_settle_s is None
    assert settings.failure_threshold == 2
    assert settings.stream_maxlen == 500


def test_defaults_constant_is_l0_base() -> None:
    """The exported L0 base is a concrete default struct instance."""
    assert isinstance(CURRENT_AMPLIFIER_SETTINGS_DEFAULTS, CurrentAmplifierServiceSettings)
    assert CURRENT_AMPLIFIER_SETTINGS_DEFAULTS == CurrentAmplifierServiceSettings()


def test_serial_defaults() -> None:
    """Serial settings default to the EBIC firmware 19200 8N1 RTU line."""
    serial = AmplifierSerialSettings(port="/dev/ttyUSB0")
    assert serial.baudrate == 19200
    assert serial.bytesize == 8
    assert serial.parity == "N"
    assert serial.stopbits == 1
    assert serial.timeout == 1.0
    assert serial.framer == "RTU"


def test_amplifier_defaults() -> None:
    """A board defaults to the EBIC driver, address 2, a 1 s poll and no settle override."""
    board = AmplifierSettings(label="photodiode_a", serial=AmplifierSerialSettings(port="/dev/ttyUSB0"))
    assert board.driver == "ebic_modbus_v1"
    assert board.address == 2
    assert board.poll_interval == 1.0
    assert board.relay_settle_s is None


def test_service_defaults() -> None:
    """The service defaults to no boards, a 3-failure threshold and 10000 entries."""
    settings = CurrentAmplifierServiceSettings()
    assert settings.amplifiers == []
    assert settings.failure_threshold == 3
    assert settings.stream_maxlen == 10000


def test_unknown_field_rejected() -> None:
    """``forbid_unknown_fields`` rejects a typo instead of silently ignoring it."""
    with pytest.raises(msgspec.ValidationError, match="bogus"):
        msgspec.convert(
            {"amplifiers": [{"label": "a", "serial": {"port": "/dev/ttyUSB0"}, "bogus": 1}]},
            CurrentAmplifierServiceSettings,
        )


def test_invalid_framer_rejected() -> None:
    """An unsupported Modbus framer fails at config-load time, not registry time."""
    with pytest.raises(msgspec.ValidationError, match="serial.framer"):
        msgspec.convert(_board(serial={"port": "/dev/ttyUSB0", "framer": "TCP"}), AmplifierSettings)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"address": 0}, "address"),
        ({"address": 248}, "address"),
        ({"poll_interval": 0}, "poll_interval"),
        ({"poll_interval": -1.0}, "poll_interval"),
        ({"relay_settle_s": -0.001}, "relay_settle_s"),
    ],
)
def test_invalid_amplifier_settings_rejected(overrides: dict[str, object], match: str) -> None:
    """Board address, poll interval and relay settle time are range-checked."""
    with pytest.raises(msgspec.ValidationError, match=match):
        msgspec.convert(_board(**overrides), AmplifierSettings)


@pytest.mark.parametrize(
    ("serial", "match"),
    [
        ({"baudrate": 0}, "baudrate"),
        ({"bytesize": 9}, "bytesize"),
        ({"parity": "X"}, "parity"),
        ({"stopbits": 3}, "stopbits"),
        ({"timeout": -1.0}, "timeout"),
    ],
)
def test_invalid_serial_settings_rejected(serial: dict[str, object], match: str) -> None:
    """Serial field domains are checked before the HAL config is ever built."""
    with pytest.raises(msgspec.ValidationError, match=match):
        msgspec.convert(_board(serial={"port": "/dev/ttyUSB0", **serial}), AmplifierSettings)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"failure_threshold": 0}, "failure_threshold"),
        ({"stream_maxlen": 0}, "stream_maxlen"),
    ],
)
def test_invalid_service_settings_rejected(overrides: dict[str, object], match: str) -> None:
    """Service-wide counters must be positive."""
    with pytest.raises(msgspec.ValidationError, match=match):
        msgspec.convert(overrides, CurrentAmplifierServiceSettings)


def test_direct_construction_raises_value_error() -> None:
    """Direct struct construction (registry path) raises a plain ValueError."""
    with pytest.raises(ValueError, match="address"):
        AmplifierSettings(label="board", serial=AmplifierSerialSettings(port="/dev/ttyUSB0"), address=300)


def test_loader_creates_default_file(tmp_path: Path) -> None:
    """A missing file is created with the default settings and returned as a patch."""
    patch = read_current_amplifier_config(tmp_path)

    assert _config_path(tmp_path).exists()
    assert patch == msgspec.to_builtins(CurrentAmplifierServiceSettings())


def test_loader_reads_existing_file(tmp_path: Path) -> None:
    """An existing file is parsed and returned as a raw mapping."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("amplifiers:\n  - label: photodiode_a\n    serial:\n      port: /dev/ttyUSB0\n")

    patch = read_current_amplifier_config(tmp_path)

    assert patch["amplifiers"] == [{"label": "photodiode_a", "serial": {"port": "/dev/ttyUSB0"}}]


def test_loader_missing_file_without_create_raises(tmp_path: Path) -> None:
    """``create_default=False`` on a missing file raises instead of writing one."""
    with pytest.raises(RuntimeError, match="does not exist"):
        read_current_amplifier_config(tmp_path, create_default=False)


def test_loader_unparseable_file_raises(tmp_path: Path) -> None:
    """An unparseable file raises RuntimeError and is left untouched."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("unterminated: [flow\n")

    with pytest.raises(RuntimeError, match="Failed to parse"):
        read_current_amplifier_config(tmp_path)

    assert path.read_text() == "unterminated: [flow\n"


def test_loader_none_conf_dir_raises() -> None:
    """A ``None`` config dir raises rather than resolving to the cwd."""
    with pytest.raises(RuntimeError, match="was not set"):
        read_current_amplifier_config(None)


def test_loader_non_directory_raises(tmp_path: Path) -> None:
    """A service subdir that is a file, not a directory, raises."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / CURRENT_AMPLIFIER_CONFIG_SUBDIR).write_text("not a directory")

    with pytest.raises(RuntimeError, match="not a directory"):
        read_current_amplifier_config(conf_dir)
