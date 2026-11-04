# Shesha Wire Protocol

The daemon and its clients speak a small binary TLV protocol over TCP (port
5555 by default). The daemon is authoritative: clients send commands and
render what comes back, never a local reconstruction of daemon state.

Firmware and clients are built and deployed together. There is no version
field and no compatibility negotiation; change both ends in one commit.

## Normative code

- `firmware/src/protocol.h`, `firmware/src/protocol.c` -- frame envelope,
  message IDs, and the response builders.
- `firmware/src/tx_flags.h` -- `CMD_TX` flag bits.
- `firmware/src/fm_protocol.h` -- `DATA_FM`, the FM transmit sub-payload,
  and both RDS blocks.
- `firmware/src/wifi_protocol.h` -- `DATA_WIFI`.
- `firmware/src/main.c` -- command handlers and the `RESP_STATUS` record.
- `client/shesha/protocol.py` -- the Python client's builders and parsers.
- `client/c/src/protocol_internal.h` -- the C client's subset of the same
  constants and parsers.

This document is the third-party contract: the envelope, the message
catalog, and every variable-length layout a client must parse. The files
above remain authoritative where an offset is not stated here, and
`client/tests/test_docs_protocol.py` pins this document against them.

## Frame

```
u8  type
u32 payload_len        little-endian, max 1 MiB
u32 seq                little-endian
u8  payload[payload_len]
```

Frames use a **9-byte header**. Frames are self-delimiting: read nine bytes,
take the length, read that many more.

`seq` is assigned by the daemon's per-client outbound ring. A jump greater
than one on a data frame means this client fell behind and the daemon
dropped frames; the client library exposes that as `gaps` and `dropped`.
Clients send `seq = 0`. `DATA_PSD` is the exception on the daemon side: its
`seq` is the producer's frame index, not a delivery counter, so it must not
be used for gap accounting.

## Message catalog

The table is checked against `MsgType` by
`client/tests/test_docs_protocol.py`. Add a row whenever you add a message.

### Commands (client to daemon)

| ID | Name | Payload | Response |
|----|------|---------|----------|
| `0x01` | `CMD_SET_FREQ` | `u64` Hz | `RESP_OK` |
| `0x02` | `CMD_SET_GAIN` | `f32` dB | `RESP_OK` |
| `0x03` | `CMD_SET_BW` | `u32` Hz | `RESP_OK` |
| `0x04` | `CMD_SET_FFT_SIZE` | `u32` 256-8192, power of two | `RESP_OK` |
| `0x05` | `CMD_SET_WINDOW` | `u8` 0=rect, 1=hann, 2=blackman-harris, 3=flat-top | `RESP_OK` |
| `0x06` | `CMD_SET_FPS` | `u8` 1-100 | `RESP_OK` |
| `0x07` | `CMD_SET_AGC` | `u8` 0=manual, 1=fast, 2=slow | `RESP_OK` |
| `0x08` | `CMD_SET_SAMPLE_RATE` | `u32` Hz | `RESP_OK` |
| `0x09` | `CMD_SET_ACCUM` | `u8` 0=none, 1=max, 2=avg | `RESP_OK` |
| `0x0A` | `CMD_SET_MODE` | `u8` 0=waterfall, 1=wifi, 2=iq, 3=fm | `RESP_OK` |
| `0x0B` | `CMD_SET_TX_ATTEN` | `f32` dB | `RESP_OK` |
| `0x0C` | `CMD_WIFI_DIAG` | empty | `RESP_WIFI_DIAG` |
| `0x0D` | `CMD_TX` | `u8` proto, `u8` flags, `u16` repeat, `u32` interval_us, sub-payload | `RESP_OK` |
| `0x0E` | `CMD_TX_STOP` | `u8` target, 0xFF=all | `RESP_OK` |
| `0x0F` | `CMD_SET_FPGA` | `u8` 0=off, 1=on; wifi mode only | `RESP_OK` |

`CMD_SET_FPGA` is rejected with `locked by active tx` while any TX session
is active. Stop the TX before entering or leaving the fabric.
| `0x15` | `CMD_TX_STREAM_DATA` | `u32` n, then n x `f32` audio | `RESP_OK` or `RESP_RETRY` |
| `0x16` | `CMD_TX_STREAM_END` | empty | `RESP_OK` |
| `0x17` | `CMD_TX_STATS` | empty | `RESP_TX_STATS` |
| `0x1C` | `CMD_SET_IQ_BITS` | `u8` 8 or 12 | `RESP_OK` |
| `0x1D` | `CMD_IQ_TX_STREAM_DATA` | `u32` n, then n packed IQ bytes; starts the stream | `RESP_OK` or `RESP_RETRY` |
| `0x1E` | `CMD_IQ_TX_STREAM_END` | empty | `RESP_OK` |
| `0x1F` | `CMD_SET_WORKERS` | `u8` 1-2, clamped at or below fs_max | `RESP_OK` |
| `0x24` | `CMD_SET_RDS` | 76-byte RDS block; requires an active FM stream | `RESP_OK` |
| `0x25` | `CMD_TX_STREAM_FLUSH` | empty | `RESP_OK` |
| `0x30` | `CMD_STATUS` | empty | `RESP_STATUS` |
| `0x31` | `CMD_CAPS` | empty | `RESP_CAPS` |
| `0x32` | `CMD_THREAD_STATS` | empty | `RESP_THREAD_STATS` |
| `0x33` | `CMD_SUBSCRIBE` | `u8` subscription mask | `RESP_OK` |

### Responses (daemon to client)

| ID | Name | Payload |
|----|------|---------|
| `0x10` | `RESP_OK` | `u8` command type, then command-specific committed values (see below) |
| `0x11` | `RESP_ERROR` | `u8` command type, then a message string |
| `0x12` | `RESP_STATUS` | fixed-size status record, 91 bytes |
| `0x13` | `RESP_CAPS` | fixed-size capability record, 24 bytes |
| `0x14` | `RESP_WIFI_DIAG` | fixed-size decoder counters, 24 bytes |
| `0x18` | `RESP_TX_STATS` | fixed-size TX telemetry, 96 bytes |
| `0x19` | `RESP_RETRY` | `u8` command type; backpressure, resend later |
| `0x1A` | `RESP_THREAD_STATS` | 36-byte header, then 52-byte records |

#### RESP_OK variants

The first byte is always the command type that succeeded. Most commands
stop there. Four radio setters append the values the hardware actually
committed, because a request may be clamped or derived:

| Command | Extra payload | Meaning |
|---------|---------------|---------|
| `CMD_SET_FREQ` | `u64` actual_hz | Accepted frequency read back from the RX LO. |
| `CMD_SET_BW` | `u32` actual_bw_hz, `u32` actual_sample_rate_hz | Accepted bandwidth and the sample rate it implies. |
| `CMD_SET_SAMPLE_RATE` | `u32` actual_hz | Accepted sample rate. |
| `CMD_SET_MODE` | `u32` actual_bw_hz, `u32` actual_sample_rate_hz | Radio state after the producer switched. |

All values are little-endian. A client keys the parse on the command it
sent. Do not assume every `RESP_OK` carries a fixed payload.

### Data (daemon to client, streaming)

| ID | Name | Payload |
|----|------|---------|
| `0x20` | `DATA_PSD` | timestamp, centre MHz, dB floor, dB ceiling, then `f32` bins |
| `0x21` | `DATA_WIFI` | 28-byte header, PSDU, always-present meta block, optional IE summary (see below) |
| `0x22` | `DATA_FM` | 16-byte header, fixed 190-byte RDS block, then `f32` audio |
| `0x23` | `DATA_IQ` | 20-byte header, then packed IQ samples |

## Subscriptions

A newly connected client receives no data frames. `CMD_SUBSCRIBE` takes a
mask: bit 0 PSD, bit 1 WiFi, bit 2 FM, bit 3 IQ. Subscribing is independent
of the active mode; frames only arrive for the mode that is running.

Data frames are best-effort. A client that stops reading is evicted once its
socket buffer fills and the daemon cannot send. Control frames (command
responses and status pushes) use a separate queue and are never displaced by
a full data ring.

## State and status

Every successful state-mutating command answers `RESP_OK` **and** pushes an
unsolicited `RESP_STATUS` to every connected client, including the issuer.
The first `CMD_IQ_TX_STREAM_DATA` counts as a state change because it starts
a TX session; later pushes in the same stream stay silent.

Clients treat their configuration as a write-only seed: send it once to a
fresh daemon, then read state from `RESP_STATUS` forever after. There is no
optimistic local state.

Four status fields answer different questions:

- `daemon_mode` is the configured mode.
- `rx_active` says that mode's producer is running right now. An exclusive
  FM transmission stops the producer without changing the configured mode.
- `fpga_active` is the configured producer variant.
- `fpga_running` says that configured FPGA producer is actually running. A
  fabric error can leave it configured but stopped.

The status record also carries the active producer's radio policy (bandwidth
and rate bounds, whether rate is independent, the pinned AGC mode), the IQ
sample width in force, PSD coverage and worker count, and lifetime ADC
sample and clip counters. A client differences the counters between status
samples and treats a decrease as a daemon restart.

Changing the status layout means updating `build_status_payload` in
`firmware/src/main.c`, `_STATUS_STRUCT` in `client/shesha/protocol.py`, the
client `Status` dataclass, and the tests that build payloads by hand.
`client/tests/test_protocol_status.py` pins the layout because a mismatch
yields plausible garbage, not an error.

## Data frame semantics

Timestamps in data frames are daemon monotonic seconds. They are not
wall-clock time and are only comparable within one connection.

- **PSD**: `center_freq_mhz` is the tuning when the slot was captured, not
  the span. Bins are dBFS and fft-shifted (DC at the centre).
- **WiFi**: the PSDU carries no FCS; the daemon reports whether the frame
  passed its own check. The PSDU is omitted when that check fails, but the
  metadata block is always present. See the layout below.
- **FM**: the audio sample rate is stamped in every frame and must be read
  from the frame, never assumed. The fixed RDS block carries decoded state
  only, so the client performs no RDS DSP.
- **IQ**: the sample width stamped in the frame is the width in force at
  capture time, so changing `CMD_SET_IQ_BITS` mid-stream cannot mis-frame a
  queued drain.

### DATA_WIFI layout

All fields are little-endian. The fixed header is 28 bytes:

| Offset | Type | Field |
|--------|------|-------|
| 0 | `f64` | timestamp, daemon monotonic seconds |
| 8 | `u8` | rate_code |
| 9 | `u8` | rate_mbps |
| 10 | `u8` | fcs_ok (0 or 1) |
| 11 | `u16` | length, the over-the-air frame length |
| 13 | `u8` | frame class (0 ack, 1 block_ack, 2 ht_vht, 3 beacon, 4 mgmt, 5 probe_req, 6 probe_resp, 7 data, 8 eapol, 9 deauth, 10 disassoc, 11 unknown) |
| 14 | `f32` | rx_gain_db |
| 18 | `u16` | psdu_len |
| 20 | `u64` | rx_freq_hz, capture-time centre frequency |
| 28 | `u8[psdu_len]` | PSDU |

The PSDU is absent (`psdu_len = 0`) when the FCS check failed, even if
bytes were received. The metadata block starts at `28 + psdu_len`, so its
offset follows the PSDU that is actually present.

The metadata block is always present, even when every flag is clear. Its
fixed prefix is 20 bytes:

| Offset | Type | Field |
|--------|------|-------|
| 0 | `u8` | flags (see below) |
| 1 | `u8[6]` | BSSID |
| 7 | `u8[6]` | source address |
| 13 | `u8[6]` | destination address |
| 19 | `u8` | ssid_len, 0-32 |
| 20 | `u8[ssid_len]` | SSID |

The block is `20 + ssid_len` bytes. Address fields are zero when their flag
bit is clear.

Metadata flag bits:

| Bit | Mask | Meaning |
|-----|------|---------|
| 0 | `0x01` | BSSID valid |
| 1 | `0x02` | source address valid |
| 2 | `0x04` | destination address valid |
| 3 | `0x08` | SSID present |
| 4 | `0x10` | frame is EAPOL |
| 5 | `0x20` | 14-byte IE summary follows |

When bit 5 is set, a 14-byte IE summary follows the metadata block:

| Offset | Type | Field |
|--------|------|-------|
| 0 | `u8` | primary channel |
| 1 | `u8` | security: 0 open, 1 WEP, 2 WPA2-PSK, 3 WPA2-EAP, 4 WPA3-SAE, 5 mixed, 6 WPA1 |
| 2 | `u8` | cipher: 0 none, 1 WEP, 2 TKIP, 3 CCMP, 4 GCMP-256 |
| 3 | `u8` | PHY flags: bit 0 HT, 1 HT-40, 2 VHT, 3 VHT-80, 4 VHT-160, 5 HE |
| 4 | `u16` | MCS-derived max rate in Mbps; 0 means use legacy_rate |
| 6 | `u8` | spatial streams (0 unknown) |
| 7 | `u8` | channel width: 0 20 MHz, 1 40, 2 80, 3 160 |
| 8 | `u8` | centre channel |
| 9 | `u8` | secondary offset: 0 none, 1 above, 3 below |
| 10 | `u8[2]` | country code, ASCII |
| 12 | `u8` | PMF: 0 none, 1 capable, 2 required |
| 13 | `u8` | legacy rate in 0.5 Mbps units (reference only) |

## Transmission semantics

`CMD_TX` is command-driven, not mode-driven. A common scheduling header is
followed by a protocol-specific payload; the scheduler handles repetition,
interval, and hardware-cyclic playback without knowing the waveform.

`repeat = 0xFFFF`, `cyclic = 1`, and `stream = 1` are open-ended: the
client that started the transmission owns it, and owner disconnect stops
it. `CMD_TX_STOP`
carries a target; `0xFF` stops everything, any other value stops only if it
matches the active transmission's protocol. A non-matching target is a safe
no-op.

`CMD_TX_STOP` returns before the drain completes. `tx_busy` stays set in
`RESP_STATUS` until the transmitter is actually idle; the daemon sends an
unsolicited status update when that happens.

`RESP_RETRY` is not a failure. The daemon's TX rings are bounded, so a
client running ahead of realtime should wait and resend the same chunk.

Rate and mode policy is described in `ARCHITECTURE.md` and the mode notes;
the short version is that a mode change or a rate-claiming transmission wins
the shared sample clock, and `CMD_SET_SAMPLE_RATE` fails while a conflicting
TX is active.

## CMD_TX payload

```
u8  proto
u8  flags        bit 0 = cyclic, bit 1 = stream
u16 repeat
u32 interval_us  ignored when cyclic
u8  sub_payload[]
```

`proto` is 0 (WiFi) or 3 (FM). IQ transmission uses its own
`CMD_IQ_TX_STREAM_DATA` / `CMD_IQ_TX_STREAM_END` pair instead of `CMD_TX`.
`proto` 1 (LoRa) is reserved and rejected. `repeat` 0 or 1 is one-shot, N
greater than 1 is counted, and `0xFFFF` runs until `CMD_TX_STOP`.

`flags` is a bitmask:

| Bit | Mask | Name | Meaning |
|-----|------|------|---------|
| 0 | `0x01` | `CYCLIC` | Hardware loops the buffered waveform with no gap. |
| 1 | `0x02` | `STREAM` | Open-ended stream; more audio arrives in `CMD_TX_STREAM_DATA`. |

Each protocol accepts only its own subset; any other bit is an error:

| Protocol | Allowed flags |
|----------|---------------|
| WiFi (0) | `CYCLIC` or none |
| FM (3) | `STREAM` or none |

| Mode | flags | repeat | Behavior |
|------|-------|--------|----------|
| One-shot | none | 0 or 1 | single DMA start |
| Counted | none | N > 1 | N starts, `interval_us` apart |
| Continuous | none | 0xFFFF | counted, looping until stopped |
| Gapless | `CYCLIC` | ignored | hardware loops the buffer with no gap |
| FM stream | `STREAM` | 1 (required) | open stream; audio continues via `CMD_TX_STREAM_DATA` |

Cyclic mode requires the whole waveform to fit the DDR TX buffer. Use
continuous mode for discrete transmissions whose gaps are too large to
buffer. FM, streaming or one-shot, requires `repeat = 1` and
`interval_us = 0`; the firmware rejects any other value.

Sub-payloads:

- **WiFi**: `u8` rate_mbps, `u16` psdu_len, then PSDU bytes. The daemon
  appends the FCS.
- **FM**: `u32` audio_rate, `u32` deviation_hz, `u32` dac_rate, `u32`
  n_audio, a fixed 76-byte RDS block, then n x `f32` audio. The RDS block is
  always present and zeroed when RDS is off, so every offset is constant.
  `CMD_SET_RDS` carries the same block alone and is valid only while an FM
  stream is active.

## Adding a message

1. Add the constant to `protocol.h` (or the subsystem header) and `MsgType`
   in `protocol.py`.
2. Add a catalog row here; `test_docs_protocol.py` fails until you do.
3. Add the builder and parser, and pin the layout with a test on each side.
