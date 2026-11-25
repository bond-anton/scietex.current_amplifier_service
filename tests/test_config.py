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
    AmplifierTcpSettings,
    CurrentAmplifierServiceSettings,
    read_current_amplifier_config,
    transport_for_driver,
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


def test_unknown_nested_field_rejected() -> None:
    """A typo inside the ``serial`` block is rejected, not just at board level."""
    with pytest.raises(msgspec.ValidationError, match="bogus"):
        msgspec.convert(_board(serial={"port": "/dev/ttyUSB0", "bogus": 1}), AmplifierSettings)


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


def _tcp_board(**overrides: object) -> dict[str, object]:
    """A minimal valid Modbus/TCP board payload with optional overrides."""
    payload: dict[str, object] = {
        "label": "preamp_1",
        "driver": "ebic_modbus_tcp_v1_preamp",
        "transport": "tcp",
        "tcp": {"host": "127.0.0.1"},
    }
    payload.update(overrides)
    return payload


def test_legacy_board_without_transport_stays_serial() -> None:
    """A board entry written before the TCP transport existed parses unchanged."""
    yaml_text = """
amplifiers:
  - label: photodiode_a
    driver: ebic_modbus_v1
    address: 2
    serial:
      port: /dev/ttyUSB0
"""

    board = msgspec.yaml.decode(yaml_text, type=CurrentAmplifierServiceSettings).amplifiers[0]

    assert board.transport == "rs485"
    assert board.tcp is None
    assert board.serial is not None
    assert board.serial.port == "/dev/ttyUSB0"


def test_tcp_board_parses_with_gateway_defaults() -> None:
    """``transport: tcp`` plus a ``tcp`` block parses; the serial block stays unset."""
    board = msgspec.convert(
        _tcp_board(label="preamp_1", address=3, poll_interval=1.0, relay_settle_s=0.008),
        AmplifierSettings,
    )

    assert board.transport == "tcp"
    assert board.serial is None
    assert board.tcp is not None
    assert board.tcp.host == "127.0.0.1"
    assert board.tcp.port == 502
    assert board.tcp.timeout == 30.0
    assert board.tcp.retries == 0
    assert board.address == 3


def test_tcp_explicit_timeout_overrides_default() -> None:
    """An explicit ``tcp.timeout`` replaces the 30.0 s gateway default."""
    board = msgspec.convert(_tcp_board(tcp={"host": "127.0.0.1", "timeout": 45.0}), AmplifierSettings)

    assert board.tcp.timeout == 45.0


def test_tcp_block_roundtrips_through_yaml() -> None:
    """The TCP endpoint survives a YAML round-trip with its configured values."""
    settings = CurrentAmplifierServiceSettings(
        amplifiers=[
            AmplifierSettings(
                label="preamp_1",
                tcp=AmplifierTcpSettings(host="gateway.local", port=1502, timeout=8.0, retries=1),
                transport="tcp",
                driver="ebic_modbus_tcp_v1",
                address=5,
            )
        ]
    )

    decoded = msgspec.yaml.decode(msgspec.yaml.encode(settings), type=CurrentAmplifierServiceSettings)
    board = decoded.amplifiers[0]

    assert board.transport == "tcp"
    assert board.tcp == AmplifierTcpSettings(host="gateway.local", port=1502, timeout=8.0, retries=1)
    assert board.serial is None


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "label": "board",
                "serial": {"port": "/dev/ttyUSB0"},
                "tcp": {"host": "127.0.0.1"},
            },
            "exactly one of",
        ),
        ({"label": "board"}, "exactly one of"),
        (
            {"label": "board", "driver": "ebic_modbus_v1", "transport": "tcp", "serial": {"port": "/dev/ttyUSB0"}},
            "requires the 'tcp' block",
        ),
        (
            {"label": "board", "driver": "ebic_modbus_tcp_v1", "transport": "rs485", "tcp": {"host": "127.0.0.1"}},
            "requires the 'serial' block",
        ),
        (
            {"label": "board", "serial": {"port": "/dev/ttyUSB0"}, "transport": "udp"},
            "transport must be one of",
        ),
    ],
)
def test_transport_and_connection_block_must_agree(payload: dict[str, object], match: str) -> None:
    """Exactly one block is set and it must be the one the transport names."""
    with pytest.raises(msgspec.ValidationError, match=match):
        msgspec.convert(payload, AmplifierSettings)


@pytest.mark.parametrize(
    ("driver", "transport", "block", "match"),
    [
        ("ebic_modbus_v1_preamp", "tcp", {"host": "127.0.0.1"}, "is a rs485 driver"),
        ("ebic_modbus_v1", "tcp", {"host": "127.0.0.1"}, "is a rs485 driver"),
        ("ebic_modbus_tcp_v1_preamp", "rs485", {"port": "/dev/ttyUSB0"}, "is a tcp driver"),
        ("ebic_modbus_tcp_v1", "rs485", {"port": "/dev/ttyUSB0"}, "is a tcp driver"),
    ],
)
def test_driver_family_must_match_transport(driver: str, transport: str, block: dict[str, object], match: str) -> None:
    """A serial driver on a TCP board (and vice versa) is rejected at load time."""
    key = "tcp" if transport == "tcp" else "serial"
    with pytest.raises(msgspec.ValidationError, match=match):
        msgspec.convert(
            {"label": "board", "driver": driver, "transport": transport, key: block},
            AmplifierSettings,
        )


def test_unknown_driver_family_is_not_transport_checked() -> None:
    """A driver outside the prefix map declares no transport, so it is allowed."""
    board = msgspec.convert(_board(driver="vendor_specific_v2"), AmplifierSettings)
    assert board.transport == "rs485"
    assert transport_for_driver("vendor_specific_v2") is None


@pytest.mark.parametrize(
    ("tcp", "match"),
    [
        ({"host": ""}, "tcp.host"),
        ({"host": "127.0.0.1", "port": 0}, "tcp.port"),
        ({"host": "127.0.0.1", "port": 70000}, "tcp.port"),
        ({"host": "127.0.0.1", "timeout": 0}, "tcp.timeout"),
        ({"host": "127.0.0.1", "retries": -1}, "tcp.retries"),
    ],
)
def test_invalid_tcp_settings_rejected(tcp: dict[str, object], match: str) -> None:
    """TCP endpoint domains are checked before the HAL client is ever built."""
    with pytest.raises(msgspec.ValidationError, match=match):
        msgspec.convert(_tcp_board(tcp=tcp), AmplifierSettings)


def test_two_tcp_boards_may_share_one_gateway() -> None:
    """Boards behind one gateway differ by address, so a shared endpoint is valid."""
    settings = msgspec.convert(
        {
            "amplifiers": [
                _tcp_board(label="preamp_1", address=3),
                _tcp_board(label="preamp_2", address=4),
            ]
        },
        CurrentAmplifierServiceSettings,
    )

    assert [board.address for board in settings.amplifiers] == [3, 4]
    assert settings.amplifiers[0].tcp.host == settings.amplifiers[1].tcp.host


def test_duplicate_tcp_endpoint_and_address_rejected() -> None:
    """The same device behind a gateway cannot be configured twice."""
    with pytest.raises(msgspec.ValidationError, match="share the same Modbus/TCP endpoint"):
        msgspec.convert(
            {
                "amplifiers": [
                    _tcp_board(label="preamp_1", address=3),
                    _tcp_board(label="preamp_2", address=3),
                ]
            },
            CurrentAmplifierServiceSettings,
        )


def test_duplicate_tcp_endpoint_ignores_host_case() -> None:
    """DNS names are case-insensitive, so a case variant is the same gateway."""
    with pytest.raises(msgspec.ValidationError, match="share the same Modbus/TCP endpoint"):
        msgspec.convert(
            {
                "amplifiers": [
                    _tcp_board(label="preamp_1", address=3, tcp={"host": "Gateway.Local"}),
                    _tcp_board(label="preamp_2", address=3, tcp={"host": "gateway.local"}),
                ]
            },
            CurrentAmplifierServiceSettings,
        )


def test_duplicate_serial_endpoint_ignores_surrounding_space() -> None:
    """A padded device path is a typo for the same port, not a second segment."""
    with pytest.raises(msgspec.ValidationError, match="share serial port"):
        msgspec.convert(
            {
                "amplifiers": [
                    _board(label="a", address=2, serial={"port": " /dev/ttyUSB0 "}),
                    _board(label="b", address=2, serial={"port": "/dev/ttyUSB0"}),
                ]
            },
            CurrentAmplifierServiceSettings,
        )


def test_distinct_tcp_ports_are_distinct_endpoints() -> None:
    """One host on two ports is two gateways, so the same address is allowed."""
    settings = msgspec.convert(
        {
            "amplifiers": [
                _tcp_board(label="preamp_1", address=3, tcp={"host": "127.0.0.1", "port": 502}),
                _tcp_board(label="preamp_2", address=3, tcp={"host": "127.0.0.1", "port": 1502}),
            ]
        },
        CurrentAmplifierServiceSettings,
    )

    assert len(settings.amplifiers) == 2


def test_serial_boards_on_one_port_need_distinct_addresses() -> None:
    """A shared RS485 segment stays valid; a repeated device does not."""
    settings = msgspec.convert(
        {
            "amplifiers": [
                _board(label="a", address=2),
                _board(label="b", address=3),
            ]
        },
        CurrentAmplifierServiceSettings,
    )
    assert len(settings.amplifiers) == 2

    with pytest.raises(msgspec.ValidationError, match="share serial port"):
        msgspec.convert(
            {
                "amplifiers": [
                    _board(label="a", address=2),
                    _board(label="b", address=2),
                ]
            },
            CurrentAmplifierServiceSettings,
        )


def test_serial_and_tcp_boards_are_tracked_separately() -> None:
    """A TCP board does not collide with a serial board on the same address."""
    settings = msgspec.convert(
        {"amplifiers": [_board(label="a", address=2), _tcp_board(label="b", address=2)]},
        CurrentAmplifierServiceSettings,
    )

    assert [board.transport for board in settings.amplifiers] == ["rs485", "tcp"]


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
