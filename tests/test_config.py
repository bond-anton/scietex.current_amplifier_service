"""Tests for the current amplifier service configuration models and loader."""

from pathlib import Path

import msgspec
import pytest

from scietex.current_amplifier_service.config import (
    CURRENT_AMPLIFIER_CONFIG_FILE,
    CURRENT_AMPLIFIER_CONFIG_SUBDIR,
    CURRENT_AMPLIFIER_SETTINGS_DEFAULTS,
    AmplifierDeviceSettings,
    AmplifierTransportSettings,
    CurrentAmplifierServiceSettings,
    read_current_amplifier_config,
    transport_for_driver,
)


def _config_path(conf_dir: Path) -> Path:
    """The namespaced path the loader reads and writes."""
    return conf_dir / CURRENT_AMPLIFIER_CONFIG_SUBDIR / CURRENT_AMPLIFIER_CONFIG_FILE


def _transport(**overrides: object) -> dict[str, object]:
    """A minimal valid RS485 transport payload with optional overrides."""
    payload: dict[str, object] = {"port": "/dev/ttyUSB0"}
    payload.update(overrides)
    return payload


def _device(**overrides: object) -> dict[str, object]:
    """A minimal valid device payload with optional overrides."""
    payload: dict[str, object] = {"address": 2}
    payload.update(overrides)
    return payload


def _service(*devices: dict[str, object], **overrides: object) -> dict[str, object]:
    """A service payload whose devices map is keyed by position."""
    payload: dict[str, object] = {
        "transport": _transport(),
        "devices": {f"dev{index}": device for index, device in enumerate(devices)},
    }
    payload.update(overrides)
    return payload


# -- defaults ---------------------------------------------------------------


def test_transport_defaults_mirror_the_plugin() -> None:
    """Transport defaults are field-for-field the API plugin's."""
    transport = AmplifierTransportSettings(port="/dev/ttyUSB0")

    assert transport.kind == "rs485"
    assert transport.port == "/dev/ttyUSB0"
    assert transport.baudrate == 9600
    assert transport.bytesize == 8
    assert transport.parity == "N"
    assert transport.stopbits == 1.0
    assert transport.timeout == 1.0
    assert transport.write_timeout is None
    assert transport.inter_byte_timeout is None
    assert transport.framer == "RTU"
    assert transport.host is None
    assert transport.tcp_port == 502
    assert transport.tcp_timeout == 30.0
    assert transport.tcp_retries == 0


def test_device_defaults_mirror_the_plugin() -> None:
    """A device defaults to the plugin's driver, address and poll plan."""
    device = AmplifierDeviceSettings()

    assert device.driver == "ebic_modbus_v1"
    assert device.address == 1
    assert device.label is None
    assert device.poll is True
    assert device.poll_interval == 1.0
    assert device.relay_settle_s is None


def test_service_defaults_mirror_the_plugin() -> None:
    """The service defaults to no devices, polling enabled and the plugin counters."""
    settings = CurrentAmplifierServiceSettings(transport=AmplifierTransportSettings(port="/dev/ttyUSB0"))

    assert settings.name is None
    assert settings.devices == {}
    assert settings.poll_enabled is True
    assert settings.failure_threshold == 3
    assert settings.stream_maxlen == 10000


def test_defaults_constant_is_l0_base() -> None:
    """The exported L0 base is a concrete default struct instance."""
    assert isinstance(CURRENT_AMPLIFIER_SETTINGS_DEFAULTS, CurrentAmplifierServiceSettings)
    assert CURRENT_AMPLIFIER_SETTINGS_DEFAULTS.devices == {}
    assert CURRENT_AMPLIFIER_SETTINGS_DEFAULTS.transport.kind == "rs485"
    assert CURRENT_AMPLIFIER_SETTINGS_DEFAULTS.transport.port == "/dev/ttyUSB0"


def test_defaults_roundtrip_through_msgspec() -> None:
    """The generated default validates and decodes back to itself.

    The framework generates ``current_amplifier.yml`` from the defaults, so the
    encoded form must be a valid payload for the settings struct it came from.
    """
    builtins = msgspec.to_builtins(CURRENT_AMPLIFIER_SETTINGS_DEFAULTS)

    assert msgspec.convert(builtins, type=CurrentAmplifierServiceSettings) == CURRENT_AMPLIFIER_SETTINGS_DEFAULTS
    assert (
        msgspec.yaml.decode(msgspec.yaml.encode(CURRENT_AMPLIFIER_SETTINGS_DEFAULTS), type=CurrentAmplifierServiceSettings)
        == CURRENT_AMPLIFIER_SETTINGS_DEFAULTS
    )


# -- decoding ---------------------------------------------------------------


def test_yaml_roundtrip_preserves_the_device_map() -> None:
    """Transport and device entries survive a YAML round-trip preserving every field."""
    settings = CurrentAmplifierServiceSettings(
        transport=AmplifierTransportSettings(
            port="/dev/ttyUSB0", baudrate=19200, parity="E", stopbits=1.5, write_timeout=0.5, inter_byte_timeout=0.01
        ),
        devices={
            "photodiode_a": AmplifierDeviceSettings(
                address=5,
                label="Photodiode A",
                poll=False,
                poll_interval=0.5,
                relay_settle_s=0.008,
            )
        },
        failure_threshold=2,
        stream_maxlen=500,
    )

    decoded = msgspec.yaml.decode(msgspec.yaml.encode(settings), type=CurrentAmplifierServiceSettings)

    assert decoded == settings
    assert decoded.transport.baudrate == 19200
    assert decoded.transport.parity == "E"
    assert decoded.transport.stopbits == 1.5
    assert decoded.transport.write_timeout == 0.5
    assert decoded.transport.inter_byte_timeout == 0.01
    device = decoded.devices["photodiode_a"]
    assert device.address == 5
    assert device.label == "Photodiode A"
    assert device.poll is False
    assert device.poll_interval == 0.5
    assert device.relay_settle_s == 0.008
    assert decoded.failure_threshold == 2
    assert decoded.stream_maxlen == 500


def test_explicit_yaml_parses_transport_and_two_devices() -> None:
    """An explicit YAML payload parses the shared transport and keyed devices."""
    yaml_text = """
transport:
  kind: rs485
  port: /dev/ttyUSB0
  baudrate: 19200
  bytesize: 8
  parity: N
  stopbits: 1
  timeout: 1.0
  framer: RTU
devices:
  photodiode_a:
    driver: ebic_modbus_v1
    address: 2
    poll_interval: 1.0
  ion_collector_b:
    address: 3
    poll_interval: 2.0
    poll: false
failure_threshold: 2
stream_maxlen: 500
"""

    settings = msgspec.yaml.decode(yaml_text, type=CurrentAmplifierServiceSettings)

    assert settings.transport.baudrate == 19200
    assert settings.transport.framer == "RTU"
    assert list(settings.devices) == ["photodiode_a", "ion_collector_b"]
    first = settings.devices["photodiode_a"]
    assert first.address == 2
    assert first.poll_interval == 1.0
    second = settings.devices["ion_collector_b"]
    assert second.address == 3
    assert second.poll_interval == 2.0
    assert second.poll is False
    assert second.relay_settle_s is None
    assert settings.failure_threshold == 2
    assert settings.stream_maxlen == 500


def test_tcp_transport_parses_with_gateway_defaults() -> None:
    """``kind: tcp`` plus a host parses; the gateway port and timeout default."""
    transport = msgspec.convert({"kind": "tcp", "host": "127.0.0.1"}, AmplifierTransportSettings)

    assert transport.kind == "tcp"
    assert transport.port is None
    assert transport.host == "127.0.0.1"
    assert transport.tcp_port == 502
    assert transport.tcp_timeout == 30.0
    assert transport.tcp_retries == 0


def test_name_is_accepted_and_ignored() -> None:
    """The plugin carries a ``name``; the framework owns the instance name."""
    settings = msgspec.convert(_service(name="main"), CurrentAmplifierServiceSettings)

    assert settings.name == "main"


def test_unknown_fields_are_rejected() -> None:
    """``forbid_unknown_fields`` rejects a typo at every level."""
    with pytest.raises(msgspec.ValidationError, match="bogus"):
        msgspec.convert(_service(bogus=1), CurrentAmplifierServiceSettings)
    with pytest.raises(msgspec.ValidationError, match="bogus"):
        msgspec.convert(_transport(bogus=1), AmplifierTransportSettings)
    with pytest.raises(msgspec.ValidationError, match="bogus"):
        msgspec.convert(_device(bogus=1), AmplifierDeviceSettings)


# -- transport validation ---------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"kind": "udp"}, "transport.kind"),
        ({"baudrate": 0}, "transport.baudrate"),
        ({"bytesize": 9}, "transport.bytesize"),
        ({"parity": "X"}, "transport.parity"),
        ({"stopbits": 3}, "transport.stopbits"),
        ({"timeout": -1.0}, "transport.timeout"),
        ({"write_timeout": -1.0}, "transport.write_timeout"),
        ({"inter_byte_timeout": -1.0}, "transport.inter_byte_timeout"),
        ({"framer": "TCP"}, "transport.framer"),
    ],
)
def test_invalid_transport_settings_are_rejected(overrides: dict[str, object], match: str) -> None:
    """Serial field domains are checked before the HAL config is ever built."""
    with pytest.raises(msgspec.ValidationError, match=match):
        msgspec.convert(_transport(**overrides), AmplifierTransportSettings)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({}, "transport 'rs485' requires 'port'"),
        ({"port": ""}, "transport 'rs485' requires 'port'"),
        ({"port": "/dev/ttyUSB0", "host": "127.0.0.1"}, "transport 'rs485' must not set 'host'"),
        ({"kind": "tcp"}, "transport 'tcp' requires 'host'"),
        ({"kind": "tcp", "host": ""}, "transport 'tcp' requires 'host'"),
        ({"kind": "tcp", "host": "127.0.0.1", "port": "/dev/ttyUSB0"}, "transport 'tcp' must not set 'port'"),
    ],
)
def test_transport_kind_and_endpoint_must_agree(payload: dict[str, object], match: str) -> None:
    """Each kind requires its own endpoint field and forbids the other's."""
    with pytest.raises(msgspec.ValidationError, match=match):
        msgspec.convert(payload, AmplifierTransportSettings)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"tcp_port": 0}, "transport.tcp_port"),
        ({"tcp_port": 70000}, "transport.tcp_port"),
        ({"tcp_timeout": 0}, "transport.tcp_timeout"),
        ({"tcp_timeout": -1.0}, "transport.tcp_timeout"),
        ({"tcp_retries": -1}, "transport.tcp_retries"),
    ],
)
def test_invalid_tcp_settings_are_rejected(overrides: dict[str, object], match: str) -> None:
    """TCP endpoint domains are checked before the HAL client is ever built."""
    with pytest.raises(msgspec.ValidationError, match=match):
        msgspec.convert({"kind": "tcp", "host": "127.0.0.1", **overrides}, AmplifierTransportSettings)


def test_stopbits_accepts_the_full_domain() -> None:
    """One, 1.5 and two stop bits are all valid."""
    for stopbits in (1, 1.5, 2):
        assert AmplifierTransportSettings(port="/dev/ttyUSB0", stopbits=stopbits).stopbits == stopbits


# -- device and service validation ------------------------------------------


@pytest.mark.parametrize("address", [0, 248, -1])
def test_device_address_is_range_checked(address: int) -> None:
    """The Modbus address must be within 1..247."""
    with pytest.raises(msgspec.ValidationError, match="address must be within 1..247"):
        msgspec.convert(_service(_device(address=address)), CurrentAmplifierServiceSettings)


@pytest.mark.parametrize("poll_interval", [0, -1.0])
def test_device_poll_interval_must_be_positive(poll_interval: float) -> None:
    """A zero or negative poll interval would spin the loop."""
    with pytest.raises(msgspec.ValidationError, match="poll_interval must be positive"):
        msgspec.convert(_service(_device(poll_interval=poll_interval)), CurrentAmplifierServiceSettings)


def test_device_relay_settle_must_be_non_negative() -> None:
    """A negative relay settle window is meaningless."""
    with pytest.raises(msgspec.ValidationError, match="relay_settle_s must be non-negative"):
        msgspec.convert(_service(_device(relay_settle_s=-0.001)), CurrentAmplifierServiceSettings)


def test_empty_device_id_is_rejected() -> None:
    """The device id is a stream-key segment, so it cannot be empty."""
    payload = _service()
    payload["devices"] = {"": _device()}

    with pytest.raises(msgspec.ValidationError, match="device id must not be empty"):
        msgspec.convert(payload, CurrentAmplifierServiceSettings)


def test_duplicate_address_across_devices_is_rejected() -> None:
    """One transport cannot hold two devices on the same Modbus address."""
    with pytest.raises(msgspec.ValidationError, match="share Modbus address 2"):
        msgspec.convert(_service(_device(address=2), _device(address=2)), CurrentAmplifierServiceSettings)


def test_distinct_addresses_on_one_transport_are_valid() -> None:
    """A shared RS485 segment stays valid as long as addresses differ."""
    settings = msgspec.convert(_service(_device(address=2), _device(address=3)), CurrentAmplifierServiceSettings)

    assert [device.address for device in settings.devices.values()] == [2, 3]


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"failure_threshold": 0}, "failure_threshold"),
        ({"stream_maxlen": 0}, "stream_maxlen"),
    ],
)
def test_invalid_service_counters_are_rejected(overrides: dict[str, object], match: str) -> None:
    """Service-wide counters must be positive."""
    with pytest.raises(msgspec.ValidationError, match=match):
        msgspec.convert(_service(**overrides), CurrentAmplifierServiceSettings)


def test_direct_construction_raises_value_error() -> None:
    """Direct struct construction (registry path) raises a plain ValueError."""
    with pytest.raises(ValueError, match="address must be within 1..247"):
        CurrentAmplifierServiceSettings(
            transport=AmplifierTransportSettings(port="/dev/ttyUSB0"),
            devices={"board": AmplifierDeviceSettings(address=300)},
        )


# -- driver transport map ---------------------------------------------------


@pytest.mark.parametrize(
    ("driver", "transport"),
    [
        ("ebic_modbus_v1", "rs485"),
        ("ebic_modbus_v1_preamp", "rs485"),
        ("ebic_modbus_tcp_v1", "tcp"),
        ("ebic_modbus_tcp_v1_preamp", "tcp"),
        ("vendor_specific_v2", None),
    ],
)
def test_transport_for_driver(driver: str, transport: str | None) -> None:
    """Only known driver families declare a transport."""
    assert transport_for_driver(driver) == transport


# -- loader -----------------------------------------------------------------


def test_loader_creates_default_file(tmp_path: Path) -> None:
    """A missing file is created with the default settings and returned as a patch."""
    patch = read_current_amplifier_config(tmp_path)

    assert _config_path(tmp_path).exists()
    assert patch == msgspec.to_builtins(CURRENT_AMPLIFIER_SETTINGS_DEFAULTS)
    created = msgspec.yaml.decode(_config_path(tmp_path).read_bytes(), type=CurrentAmplifierServiceSettings)
    assert created == CURRENT_AMPLIFIER_SETTINGS_DEFAULTS


def test_loader_reads_existing_file(tmp_path: Path) -> None:
    """An existing file is parsed and returned as a raw mapping."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("transport:\n  port: /dev/ttyUSB1\ndevices:\n  photodiode_a:\n    address: 2\n")

    patch = read_current_amplifier_config(tmp_path)

    assert patch == {
        "transport": {"port": "/dev/ttyUSB1"},
        "devices": {"photodiode_a": {"address": 2}},
    }


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
