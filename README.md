# scietex.current_amplifier_service

Scietex microservice daemon for current amplifier monitoring and control.

The service drives one or more EBIC v1.0 current-amplifier boards over Modbus
RTU. Each board is handled by a driver from
[`scietex.hal.current_amplifier`](https://github.com/bond-anton/scietex.hal.current_amplifier),
selected by entry point name in the service configuration. The service polls
each board on its own long-lived task, publishes readings to a per-board Valkey
stream, and exposes control tasks (`amplifier:read`, `amplifier:status`,
`amplifier:set-feedback-resistor`, …) on the data lane.

## Supported boards

| Board | Vendor | MCU | Bus | Driver entry points |
| --- | --- | --- | --- | --- |
| EBIC Preamp v1.0 | SCIETEX.RU | STM32F411 | Modbus RTU over RS485 | `ebic_modbus_v1`, `ebic_modbus_v1_preamp` |

`ebic_modbus_v1` is the semantic driver: it maps the relay coils onto feedback
resistor, boost gain, DC-bias enable and compensation enable, and decodes the
coil pattern into an `AmplifierState`. `ebic_modbus_v1_preamp` is the raw relay
driver underneath it: it exposes the coils and the communication settings, but
its `read_data()` returns an empty snapshot, so a board configured with it
publishes rows of `None`. Use `ebic_modbus_v1` unless you are driving the
relays directly.

## Configuration

The service reads `current_amplifier/current_amplifier.yml` under the framework
config directory (`SCIETEX_CONFIG_DIR`, default `/config` in the container). The
file is created with defaults on first start.

```yaml
amplifiers:
  - label: photodiode_a
    driver: ebic_modbus_v1
    address: 2
    poll_interval: 1.0
    # relay_settle_s: 0.008
    serial:
      port: /dev/ttyUSB0
      baudrate: 19200
      bytesize: 8
      parity: N
      stopbits: 1
      timeout: 1.0
      framer: RTU
  - label: ion_collector_b
    driver: ebic_modbus_v1
    address: 3
    poll_interval: 2.0
    serial:
      port: /dev/ttyUSB1
failure_threshold: 3
stream_maxlen: 10000
```

| Field | Default | Meaning |
| --- | --- | --- |
| `label` | required | Unique label; the registry key and the `{label}` segment of the stream key. |
| `serial.port` | required | Serial device the board is wired to, e.g. `/dev/ttyUSB0`. Each board entry carries its own connection settings. |
| `baudrate` | `19200` | RS485 baud rate; must be positive. The EBIC firmware default is 19200. |
| `bytesize` | `8` | Data bits, one of 5–8. |
| `parity` | `N` | Parity, one of `N`, `E`, `O`. |
| `stopbits` | `1` | Stop bits, one of `1`, `1.5`, `2`. |
| `timeout` | `1.0` | Per-operation serial timeout in seconds; must be non-negative. |
| `framer` | `RTU` | Modbus framing, `RTU` or `ASCII`. |
| `driver` | `ebic_modbus_v1` | HAL driver entry point name (see the table above). |
| `address` | `2` | Modbus slave address, 1–247. Every board on one segment needs a distinct one; the EBIC default is 2. |
| `poll_interval` | `1.0` | Seconds between reads; must be positive. |
| `relay_settle_s` | driver default (`0.008`) | Optional. Delay after a make write before the dependent relays move. Forwarded only when set and only if the driver accepts the keyword. |
| `failure_threshold` | `3` | Consecutive failed reads before a board is reported as disconnected. One dropped frame on an RS485 segment must not flip the status. |
| `stream_maxlen` | `10000` | Entries retained per board's readings stream. |

A configuration change at runtime is **restart-required**: the board registry is
not live-rebuilt.

## Tasks

Control lane:

| Task | Lane | Required capability | Arguments | Result |
| --- | --- | --- | --- | --- |
| `monitoring:start` | control | none | `{"label": "photodiode_a"}` or empty for every board | `MonitoringResponse{action, labels, monitoring}` |
| `monitoring:stop` | control | none | `{"label": "photodiode_a"}` or empty for every board | `MonitoringResponse{action, labels, monitoring}` |

Data lane:

| Task | Lane | Required capability | Arguments | Result |
| --- | --- | --- | --- | --- |
| `amplifier:list` | data | none | `{}` | `AmplifierListResponse{service, instance_id, amplifiers[]}` with each board's `controls` summary |
| `amplifier:status` | data | none | `{"label": "photodiode_a"}` or empty for every board | `AmplifierStatusResponse{amplifiers[]}` from cached poll counters; no bus I/O |
| `amplifier:read` | data | none | `{"label": "photodiode_a"}` | `AmplifierCommandResponse{label, task, state}` from a live `read_data()` |
| `amplifier:set-feedback-resistor` | data | `can_select_preamp` and `resistance_ohm` in the board's enumerated Rf options | `{"label": ..., "resistance_ohm": 1000000}` | echo `resistance_ohm` plus the state read back after the switch (`readback_error` if the read failed after the write landed) |
| `amplifier:set-boost-gain` | data | `can_set_boost` and `gain` in the board's accepted gains | `{"label": ..., "gain": 10}` | echo `gain` |
| `amplifier:set-dc-bias` | data | `capabilities.dc_bias.enabled.write` | `{"label": ..., "enabled": true}` | echo `enabled` |
| `amplifier:set-dc-bias-level` | data | `capabilities.dc_bias.level.write` | `{"label": ..., "level": 0.5}` | echo `level` |
| `amplifier:set-bandwidth` | data | `can_set_bandwidth` | `{"label": ..., "bandwidth_hz": 100000}` | echo `bandwidth_hz` |
| `amplifier:set-filter` | data | `capabilities.filters.enabled.write` and `filter_type` in `capabilities.filters.types`; `capabilities.filters.cutoff.write` when `low_cutoff_hz` or `high_cutoff_hz` is given | `{"label": ..., "filter_type": "low_pass", "enabled": true, "low_cutoff_hz": 10, "high_cutoff_hz": null}` | echo `filter_type`, `enabled`, `low_cutoff_hz`, `high_cutoff_hz` |
| `amplifier:set-input-compensation` | data | `capabilities.input_compensation.enabled.write` | `{"label": ..., "enabled": true}` | echo `enabled` |
| `amplifier:set-input-compensation-level` | data | `capabilities.input_compensation.level.write` | `{"label": ..., "level": 1.0}` | echo `level` |
| `amplifier:set-inverted` | data | `can_set_invert_output` | `{"label": ..., "inverted": true}` | echo `inverted` |

Every write task is gated on the board's declared capabilities, so an
unsupported command is rejected as `UNSUPPORTED` before the driver is called.
`amplifier:read`, `amplifier:status` and `amplifier:list` are ungated. Error
codes are `INVALID_PAYLOAD`, `UNKNOWN_AMPLIFIER`, `UNSUPPORTED`,
`INVALID_PARAMETERS`, `AMPLIFIER_COMMAND_FAILED` (retryable for bus and relay
failures), and `MONITORING_FAILED`.

## Valkey keys

- Readings stream:
  `scietex:{service}:{instance_id}:current_amplifier:{label}:readings` with
  fields `ts` (ISO-8601 UTC), `output_voltage_v`, `output_current_a`,
  `input_current_a`, `boost_gain`, `feedback_resistor_ohm`, `preamp_range`
  (same value as `feedback_resistor_ohm`; both spellings decode), `dc_bias_level`,
  `bandwidth_hz`, `input_compensation_level`, `dc_bias_enabled`,
  `input_compensation_enabled`, `input_overcurrent`, `output_overload`,
  `inverted`, `filters` (JSON array of filter settings), and
  `transconductance_v_per_a` (`Rf × gain` when both are known). A field the
  board does not report is written as an empty value, which is distinguishable
  from a reported `0.0`; a tri-state flag uses `1`/`0`, with empty for unknown.
- Telemetry: `scietex:{service}:{instance_id}:telemetry`, msgpack, pushed every
  2 s with a 6 s TTL. Top level: `state` (`running`, `idle`, `unconfigured`),
  `service`, `instance_id`, `configured`, `poll_enabled`, `timestamp`,
  `devices`, `schema_version` (`1`). `devices` is keyed by board label and
  carries `state` (`error`, `monitoring`, `ok`, `idle`), `driver`, `connected`,
  `monitoring`, `ts`, every `AmplifierState` field, `reading_count`,
  `error_count`, `last_error`, `last_ok`, and `transconductance_v_per_a`.

## Domain notes

- **Rf must be one of the eight board resistors** — 1 kΩ, 10 kΩ, 100 kΩ, 1 MΩ,
  10 MΩ, 100 MΩ, 1 GΩ, 10 GΩ. The handler rejects anything else as
  `INVALID_PARAMETERS` from the board's own option list, so a typo never reaches
  the bus; the driver repeats the check before touching hardware.
- **Rf switching is make-before-break.** The target resistor engages before the
  old one releases and the driver waits `relay_settle_s` (8 ms by default)
  before the channel relays move, so the loop stays closed. It is safe to change
  while measuring; expect a brief parallel-Rf transient.
- **Boost gains are exactly 1, 3, 10, 30.** A gain change also engages the new
  relay before releasing the old one, so a `×10 ↔ ×30` switch passes through
  about `×39` for the settle window. **Never change gain during a
  measurement.**
- **Five capability-gated writes are unimplemented on EBIC.** The board exposes
  Rf, boost gain, DC-bias enable and compensation enable; the capability tree
  reports bandwidth, filters, DC-bias level, compensation level and output
  inversion as unsupported, so `amplifier:set-bandwidth`, `amplifier:set-filter`,
  `amplifier:set-dc-bias-level`, `amplifier:set-input-compensation-level` and
  `amplifier:set-inverted` always answer `UNSUPPORTED` on this board.
- **One serial bus per port.** Boards that share a port share one RS485
  segment, and every poll is a separate Modbus transaction on that bus, so N
  boards on one segment see readings roughly N × `poll_interval` apart. Give a
  board its own port when the timing matters.
- **Readings are per board, not per channel.** One entry in `amplifiers` is one
  amplifier channel; the stream key and every task address it by `label`.

## Running

```bash
start-current-amplifier-service --conf-dir /etc/scietex
```

Flags: `--conf-dir` (auto-resolved when omitted), `--service-name` (default
`$SCIETEX_SERVICE_NAME` or `CURRENT_AMPLIFIER`), `--logging-level` (default
`$SCIETEX_LOGGING_LEVEL` or `INFO`). It runs in the foreground until `SIGINT` or
`SIGTERM`.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ../scietex.hal.current_amplifier -e ../scietex.hal.serial
pip install -e ".[dev,test,lint]"
```

The HAL packages are installed from their checkouts, so an editable install is
the normal case; a plain `pip install -e ".[dev,test,lint]"` pulls the released
HAL versions from PyPI instead. `pytest.ini` and `pyproject.toml` put the
checkout on the import path, so the suite does not depend on an installed copy
of the service package.

```bash
pytest
ruff check src tests
tox -l
```

## Container

```bash
./build_image.sh [--latest]
```

Builds and pushes `registry.buro-nts.ru/scietex-current-amplifier-service:<version>`
for `linux/amd64` and `linux/arm64`. The image installs the released wheel from
PyPI, so publish the version first. The rootfs is read-only-safe: only the
mounted config volume is ever written.

## Deployment

The board is reached through an RS485 USB adapter, so the container needs the
device and the adapter's group:

```bash
podman run -d \
    --name current-amplifier-service \
    --device=/dev/ttyUSB0 \
    --group-add dialout \
    -v /etc/scietex:/config:Z \
    --read-only \
    registry.buro-nts.ru/scietex-current-amplifier-service:0.1.0
```

`--device` exposes the adapter; `--group-add dialout` puts the container's
non-root user in the group that owns `/dev/ttyUSB*` on the host. Adjust the
group name when the host uses a different one (`ls -l /dev/ttyUSB0`). The config
file is created under `/config/current_amplifier/` on first start.
