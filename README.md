# scietex.current_amplifier_service

Scietex microservice daemon for current amplifier monitoring and control.

The service drives one or more EBIC v1.0 current-amplifier boards over Modbus
RTU (directly on RS485, or through a Modbus/TCP gateway). Each board is handled
by a driver from
[`scietex.hal.current_amplifier`](https://github.com/bond-anton/scietex.hal.current_amplifier),
selected by entry point name in the service configuration. The service polls
each board on its own long-lived task, publishes readings to a per-board Valkey
stream, and exposes control tasks (`current_amplifier:read`,
`current_amplifier:status`, `current_amplifier:select-feedback-resistor`, …) on
the data lane. One service instance reaches one segment — one RS485 port or one
Modbus/TCP gateway.

## Supported boards

| Board | Vendor | MCU | Bus | Driver entry points |
| --- | --- | --- | --- | --- |
| EBIC Preamp v1.0 | SCIETEX.RU | STM32F411 | Modbus RTU over RS485, or Modbus/TCP through a gateway | `ebic_modbus_v1`, `ebic_modbus_v1_preamp`, `ebic_modbus_tcp_v1`, `ebic_modbus_tcp_v1_preamp` |

The `*_tcp_*` entry points speak the same register/coil surface to a Modbus/TCP
gateway that routes the requests onto the RS485 bus; the board is selected by
its Modbus `address` behind the gateway. `ebic_modbus_v1` is the semantic
driver: it maps the relay coils onto feedback resistor, boost gain, DC-bias
enable and compensation enable, and decodes the coil pattern into an
`AmplifierState`. `ebic_modbus_v1_preamp` is the raw relay driver underneath it:
it exposes the coils and the communication settings, but its `read_data()`
returns an empty snapshot, so a board configured with it publishes rows of
`None`. Use `ebic_modbus_v1` unless you are driving the relays directly.

## Configuration

The service reads `current_amplifier/current_amplifier.yml` under the framework
config directory (`SCIETEX_CONFIG_DIR`, default `/config` in the container). The
file is created with defaults on first start.

```yaml
transport:
  kind: rs485
  port: /dev/ttyUSB0
  baudrate: 19200
  bytesize: 8
  parity: N
  stopbits: 1.0
  timeout: 1.0
  framer: RTU
devices:
  amp0:
    driver: ebic_modbus_v1
    address: 2
    label: Photodiode preamp
    poll: true
    poll_interval: 1.0
  amp1:
    driver: ebic_modbus_v1
    address: 3
    poll_interval: 2.0
    relay_settle_s: 0.008
poll_enabled: true
failure_threshold: 3
stream_maxlen: 10000
```

The transport is service-wide and the boards are a map keyed by device id. A
gateway segment uses the same shape with the TCP fields:

```yaml
transport:
  kind: tcp
  host: 127.0.0.1
  tcp_port: 502
  tcp_timeout: 30.0
  tcp_retries: 0
devices:
  preamp_1:
    driver: ebic_modbus_tcp_v1_preamp
    address: 3
```

`kind` is the discriminator: `rs485` (the default) requires `port` and forbids
`host`; `tcp` requires `host` and forbids `port`. The driver family must match
the transport too (`ebic_modbus_v1*` is RS485, `ebic_modbus_tcp_v1*` is TCP).
Every board on the segment is told apart by its Modbus `address`, so two devices
may not share one. The struct rejects unknown fields, so a stray key fails the
config load instead of being ignored.

The device id — the `devices` map key — is the board's identity everywhere: it
is the `{device}` segment of the readings-stream key and the key in the
telemetry `devices` map, and every task targets it. `label` is a display string
only and is never an address.

| Field | Default | Meaning |
| --- | --- | --- |
| `transport.kind` | `rs485` | `rs485` or `tcp`; selects which transport fields apply. |
| `transport.port` | required for `rs485` | Serial device the segment is wired to, e.g. `/dev/ttyUSB0`. Never a TCP port. |
| `transport.baudrate` | `9600` | RS485 baud rate; must be positive. EBIC firmware runs 19200, so set it explicitly. |
| `transport.bytesize` | `8` | Data bits, one of 5–8. |
| `transport.parity` | `N` | Parity, one of `N`, `E`, `O`. |
| `transport.stopbits` | `1.0` | Stop bits, one of `1`, `1.5`, `2`. |
| `transport.timeout` | `1.0` | Per-response serial wait in seconds; must be non-negative. Not the gateway client timeout. |
| `transport.write_timeout` | `None` | Per-write serial timeout; must be non-negative when set. |
| `transport.inter_byte_timeout` | `None` | Inter-byte serial gap; must be non-negative when set. |
| `transport.framer` | `RTU` | Modbus framing, `RTU` or `ASCII`. |
| `transport.host` | required for `tcp` | Gateway host name or address. |
| `transport.tcp_port` | `502` | Gateway TCP port, 1–65535. |
| `transport.tcp_timeout` | `30.0` | Gateway client timeout, positive. It must *exceed* the gateway's published `client_timeout_floor`, not merely the device response time, because the gateway holds the RS485 bus through reconnect and retry work before it answers or gives up. 30.0 is sized for `serial.timeout: 1.0`, `bus_retries: 0`, `bus_queue_depth: 4` (floor 26.2 s); a smaller value reports normal bus contention as an unreachable board. |
| `transport.tcp_retries` | `0` | pymodbus retries per request; non-negative. Zero keeps an ambiguous write from being sent twice. |
| `devices` | `{}` | Map of device id to board settings; the key is the identity. |
| `devices.<id>.driver` | `ebic_modbus_v1` | HAL driver entry point name (see the table above). |
| `devices.<id>.address` | `1` | Modbus slave address, 1–247; unique per segment. |
| `devices.<id>.label` | `None` | Display string only; not an address and not a key. |
| `devices.<id>.poll` | `true` | `false` keeps the board out of the poll plan; it stays readable on demand. |
| `devices.<id>.poll_interval` | `1.0` | Seconds between reads; must be positive. |
| `devices.<id>.relay_settle_s` | driver default (`0.008`) | Optional. Delay after a make write before the dependent relays move. Forwarded only when set and only if the driver accepts the keyword. |
| `name` | `None` | Carried by the plugin payload and accepted for compatibility; the instance name is framework-owned, so it is ignored. |
| `poll_enabled` | `true` | Gates the poll plan as a whole; `false` starts nothing. |
| `failure_threshold` | `3` | Consecutive failed reads before a board is reported as disconnected. One dropped frame on an RS485 segment must not flip the status. |
| `stream_maxlen` | `10000` | Entries retained per board's readings stream. |

One transport per instance: boards on two serial ports, or behind two
gateways, need two service instances — the same way `vfd_service` treats its
gateway.

### Gateway configuration for EBIC boards

Recommended gateway settings: `serial.timeout: 1.0`, `bus_retries: 0`,
`bus_queue_depth` ≥ number of boards minus one, `allow_unknown_devices: false`,
`circuit_breaker.failure_threshold: 2`, `cooldown: 30.0`, and one `devices:`
entry per unit id with `framer: RTU`.

With that layout `client_timeout_floor` is 17.8 s (`q=2`) to 26.2 s (`q=4`), so
`transport.tcp_timeout: 30.0` is safe. If you raise `serial.timeout`, recompute
with

```
B = t + 0.1 + (r + 1) * t
s = 2 * t + 0.1
request_timeout      = (B + s) * (q + 1) + t
client_timeout_floor = request_timeout + B + s
```

over `t = serial.timeout`, `r = bus_retries`, `q = bus_queue_depth`. Do not
leave `transport.tcp_timeout` below the floor, or normal bus contention will be
reported as an unreachable board; `transport.timeout` cannot stand in for it —
it is the 1 s per-response serial wait, and reusing it would install a 1 s
client timeout against a 26.2 s floor. Keep `transport.tcp_retries: 0`: a retry
after a timeout re-sends a request whose first copy may still be executing.

**Gateway version requirement.** `scietex.hal.serial >= 2.7.0` on the gateway
host. On 2.6.0 or earlier a lock-wait expiry is answered with Modbus exception
`0x0B` (target device failed to respond) instead of `0x06` (server device busy),
so bus contention is indistinguishable from an unreachable board and the service
will report a healthy amplifier as unhealthy. The requirement cannot be enforced
by dependency pins, because the gateway is a separate deployment: check the
image tag of the `scietex.modbus_service` container rather than assuming pip
resolved it.

A configuration change at runtime is **restart-required**: the board registry is
not live-rebuilt.

### Breaking changes in 0.3.0

- Task namespace `amplifier:*` → `current_amplifier:*`. The old names are
  removed, not aliased; a client still sending them gets an unknown task.
- Config shape changed: the `amplifiers` list with per-board `serial`/`tcp`
  blocks is gone. Old configs no longer decode — move the connection fields
  under one `transport` and each board under `devices`, keyed by device id.
- Command parameters use the plugin's spellings: `label` → `device`,
  `resistance_ohm` → `feedback_resistor_ohm`, `gain` → `boost_gain`,
  `enabled` → `dc_bias_enabled` / `filter_enabled` /
  `input_compensation_enabled` (per verb), `level` → `dc_bias_level` /
  `input_compensation_level`, `low_cutoff_hz` → `filter_low_cutoff_hz`,
  `high_cutoff_hz` → `filter_high_cutoff_hz`. `amplifier:set-feedback-resistor`
  also became `current_amplifier:select-feedback-resistor`.
- Identity moved from `label` to the device id: the registry key, the readings
  stream key, the telemetry `devices` map and the command selector all use the
  device id now. A board without a `label` keeps working; a board whose only
  identity was its label must be given a device id in `devices`.

An operator must rewrite `current_amplifier.yml` to the new shape and update
every client that submits `amplifier:*` tasks.

## Tasks

Control lane:

| Task | Lane | Required capability | Arguments | Result |
| --- | --- | --- | --- | --- |
| `monitoring:start` | control | none | `{"device": "amp0"}` or empty for every board | `MonitoringResponse{action, labels, monitoring}`, `labels` being the affected device ids |
| `monitoring:stop` | control | none | `{"device": "amp0"}` or empty for every board | `MonitoringResponse{action, labels, monitoring}` |

Data lane:

| Task | Lane | Required capability | Arguments | Result |
| --- | --- | --- | --- | --- |
| `current_amplifier:list` | data | none | `{}` | `AmplifierListResponse{service, instance_id, amplifiers[]}` with each board's `device`, `label` and `controls` summary; answers from the worker registry, never sent by the plugin |
| `current_amplifier:status` | data | none | `{"device": "amp0"}` or empty for every board | `AmplifierStatusResponse{amplifiers[]}` from cached poll counters; no bus I/O; answers from the worker registry, never sent by the plugin |
| `current_amplifier:read` | data | none | `{"device": "amp0"}` | `AmplifierCommandResponse{device, task, state}` from a live `read_data()` |
| `current_amplifier:select-preamp-range` | data | none | `{"device": ..., "preamp_range": 1.0}` | always `UNSUPPORTED`: the HAL exposes `select_feedback_resistor` only, so no range-to-resistance mapping exists |
| `current_amplifier:select-feedback-resistor` | data | `can_select_preamp` and `feedback_resistor_ohm` in the board's enumerated Rf options | `{"device": ..., "feedback_resistor_ohm": 1000000}` | echo `feedback_resistor_ohm` plus the state read back after the switch (`readback_error` if the read failed after the write landed) |
| `current_amplifier:set-boost-gain` | data | `can_set_boost` and `boost_gain` in the board's accepted gains | `{"device": ..., "boost_gain": 10}` | echo `boost_gain` |
| `current_amplifier:set-dc-bias` | data | `capabilities.dc_bias.enabled.write` | `{"device": ..., "dc_bias_enabled": true}` | echo `dc_bias_enabled` |
| `current_amplifier:set-dc-bias-level` | data | `capabilities.dc_bias.level.write` | `{"device": ..., "dc_bias_level": 0.5}` | echo `dc_bias_level` |
| `current_amplifier:set-bandwidth` | data | `can_set_bandwidth` | `{"device": ..., "bandwidth_hz": 100000}` | echo `bandwidth_hz` |
| `current_amplifier:set-filter` | data | `capabilities.filters.enabled.write` and `filter_type` in `capabilities.filters.types`; `capabilities.filters.cutoff.write` when `filter_low_cutoff_hz` or `filter_high_cutoff_hz` is given | `{"device": ..., "filter_type": "low_pass", "filter_enabled": true, "filter_low_cutoff_hz": 10, "filter_high_cutoff_hz": null}` | echo `filter_type`, `filter_enabled`, `filter_low_cutoff_hz`, `filter_high_cutoff_hz` |
| `current_amplifier:set-input-compensation` | data | `capabilities.input_compensation.enabled.write` | `{"device": ..., "input_compensation_enabled": true}` | echo `input_compensation_enabled` |
| `current_amplifier:set-input-compensation-level` | data | `capabilities.input_compensation.level.write` | `{"device": ..., "input_compensation_level": 1.0}` | echo `input_compensation_level` |
| `current_amplifier:set-bias-compensation-source` | data | `capabilities.bias_compensation_source.access.write` | `{"device": ..., "bias_compensation_source": "dac"}` (`"dac"` or `"manual"`) | echo `bias_compensation_source` plus the state read back after the switch |
| `current_amplifier:set-inverted` | data | `can_set_invert_output` | `{"device": ..., "inverted": true}` | echo `inverted` |

Every write task is gated on the board's declared capabilities, so an
unsupported command is rejected as `UNSUPPORTED` before the driver is called.
`current_amplifier:read` is ungated and does a live `read_data()`;
`current_amplifier:list` and `current_amplifier:status` are ungated and answer
from the worker registry and poll counters without bus I/O. Error codes are
`INVALID_PAYLOAD`, `UNKNOWN_AMPLIFIER`, `UNSUPPORTED`,
`INVALID_PARAMETERS`, `AMPLIFIER_COMMAND_FAILED` (retryable for bus and read
failures), `AMPLIFIER_WRITE_OUTCOME_UNKNOWN` (the write may have reached the
board, so it must not be replayed), and `MONITORING_FAILED`.

## Valkey keys

- Readings stream:
  `scietex:CurrentAmplifier:{instance_id}:current_amplifier:{device}:readings`
  with fields `ts` (ISO-8601 UTC), `output_voltage_v`, `output_current_a`,
  `input_current_a`, `boost_gain`, `feedback_resistor_ohm`, `preamp_range`
  (same value as `feedback_resistor_ohm`; both spellings decode), `dc_bias_level`,
  `bandwidth_hz`, `input_compensation_level`, `dc_bias_enabled`,
  `input_compensation_enabled`, `input_overcurrent`, `output_overload`,
  `inverted`, `filters` (JSON array of filter settings), and
  `transconductance_v_per_a` (`Rf × gain` when both are known). A field the
  board does not report is written as an empty value, which is distinguishable
  from a reported `0.0`; a tri-state flag uses `1`/`0`, with empty for unknown.
  `{device}` is the device id (the `devices` map key), e.g.
  `...:current_amplifier:amp0:readings`; renaming a device id starts a new
  stream key and the old series is not backfilled.
- Telemetry: `scietex:CurrentAmplifier:{instance_id}:telemetry`, msgpack, pushed
  every 2 s with a 6 s TTL. Top level: `state` (`running`, `idle`, `unconfigured`),
  `service`, `instance_id`, `configured`, `poll_enabled`, `timestamp`,
  `devices`, `schema_version` (`1`). `devices` is keyed by device id — the same
  identity as the readings key — e.g. `devices["amp0"]`. Each entry carries
  `state` (`error`, `monitoring`, `ok`, `idle`), `driver`, `connected`,
  `monitoring`, `ts`, every `AmplifierState` field, `reading_count`, `error_count`, `last_error`, `last_ok`,
  `transconductance_v_per_a`, `transport` (matching `current_amplifier:list`),
  and the display `label` when one is configured.

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
  inversion as unsupported, so `current_amplifier:set-bandwidth`,
  `current_amplifier:set-filter`, `current_amplifier:set-dc-bias-level`,
  `current_amplifier:set-input-compensation-level` and
  `current_amplifier:set-inverted` always answer `UNSUPPORTED` on this board.
- **One serial bus per port.** Boards that share a port share one RS485
  segment, and every poll is a separate Modbus transaction on that bus, so N
  boards on one segment see readings roughly N × `poll_interval` apart. Run a
  board on its own segment (a second service instance) when the timing matters.
  The same applies to boards behind one Modbus/TCP gateway: they queue on the
  gateway's single RS485 bus.
- **A failing board backs off.** The first failures in a streak retry at
  `poll_interval`; once the streak reaches `failure_threshold`, every further
  consecutive failure doubles the delay, capped at 10 × `poll_interval`. A
  successful read resets the delay to `poll_interval` immediately. A
  `GatewayBusy` response (bus contended) backs off the same way but does not
  mark the board disconnected.
- **Readings are per board, not per channel.** One entry under `devices` is one
  amplifier channel; the stream key and every task address it by the device id
  (the map key), not by the display `label`.

## Running

```bash
start-current-amplifier-service --conf-dir ~/.config/scietex
```

Flags: `--conf-dir` (auto-resolved when omitted), `--service-name` (default
`$SCIETEX_SERVICE_NAME` or `CurrentAmplifier`), `--logging-level` (default
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

### Local build

```bash
podman build -f Containerfile.local -t scietex-current-amplifier-service:local .
```

`Containerfile.local` builds the same image from the working tree instead of the
index, so it works before a version is published — the release build above
installs `==${VERSION}` and fails until that version exists on PyPI. The release
path is unchanged.

## Deployment

An RS485 board is reached through a USB adapter, so the adapter node must be
passed into the container (a service on `transport.kind: tcp` needs only network
access to its gateway):

```bash
podman run --rm \
    --device /dev/ttyUSB0 \
    -v ~/.config/scietex:/config:z \
    registry.buro-nts.ru/scietex-current-amplifier-service:latest
```

| Option | Purpose |
| --- | --- |
| `--device /dev/ttyUSB0` | Passes the adapter into the container |
| `-v ~/.config/scietex:/config:z` | Host-owned config tree, SELinux-relabelled as shared (`:z`) |

The config file is created under `~/.config/scietex/current_amplifier/` on first
start. `:z` applies the shared SELinux relabel (`container_file_t`, no private
MCS category), which is what a tree mounted by more than one container needs.
Rootless, a non-root container user reaches the node only with the same three
settings the Quadlet drop-in carries (`--userns=keep-id`,
`--group-add keep-groups`, `--security-opt label=disable`).

### Quadlet (systemd user unit)

Install `deploy/current-amplifier-service.container` as
`~/.config/containers/systemd/scietex-current-amplifier-service.container`
and the serial drop-in as
`~/.config/containers/systemd/scietex-current-amplifier-service.container.d/50-serial-devices.conf`,
then reload and enable the generated unit:

```bash
systemctl --user daemon-reload
systemctl --user enable --now scietex-current-amplifier-service
```

The `Volume=` line bind-mounts the host config tree instead of using a named
volume, so `~/.config/scietex/current_amplifier/current_amplifier.yml` is
editable on the host and survives container replacement; the service creates
`current_amplifier/` and the YAML inside it on first start.
`~/.config/scietex/current-amplifier.env` takes optional `KEY=VALUE` environment
overrides. Create it before starting the unit — it may stay empty:
`install -m 600 /dev/null ~/.config/scietex/current-amplifier.env`.

The unit runs rootless, so the adapter is reachable only through the drop-in:
`UserNS=keep-id` maps the container uid to your own user and
`GroupAdd=keep-groups` passes your supplementary groups through. Your host user
must therefore be in the group that owns `/dev/ttyUSB*`, normally `dialout`:

```bash
sudo usermod -aG dialout "$USER"
```

Log out and back in so the new group membership reaches the systemd user
manager. Confirm the owning group with `ls -l /dev/ttyUSB0` and substitute a
different name if your host uses one.

Every `/dev/ttyUSB*` node named by `transport.port` must be passed into the
container, so a service on an additional adapter needs its own `AddDevice=` line
in the drop-in. The board's `address` must match its Modbus slave address and
stay unique on the segment.
A board reached through a Modbus/TCP gateway (`transport.kind: tcp`) needs no device
node, only network access to the gateway's `host:tcp_port`. The `ttyUSB` number is
assigned in enumeration order and can change across reboots or replugs, so pin a
udev symlink when the assignment matters. The segment's line settings must also
match the `transport` block (19200 8-N-1 for EBIC boards).
