# Shesha Architecture

Shesha is a software-defined radio platform built on PlutoSDR (AD9361 +
Zynq). It replaces the stock IIO DMA pipeline with a custom DDR DMA path,
creating a high-throughput bridge between ADC/DAC and arbitrary host
software.

There are three layers:

1. **Firmware daemon** runs on the Pluto ARM core. It captures IQ from DDR
   DMA, processes it, and streams results over TCP.
2. **Protocol** is binary TLV over TCP. Commands flow in, data streams out.
3. **Clients** speak the protocol: the TUI, the headless CLI, and anything
   else you build.

```
+-------------------------------------------------------------+
|                      Host (any client)                      |
|     TUI   |   CLI   |   pygame   |   C   |   your client    |
+-------------------------------------------------------------+
                               |
                               v
+-------------------------------------------------------------+
|                  shesha-daemon (Pluto ARM)                  |
|                                                             |
|     +---------+    +----------+    +------------------+     |
|     | command |--->| producer |--->|   ring buffers   |     |
|     |dispatch |    | threads  |    | (PSD / WiFi / FM)|     |
|     +----+----+    +----------+    +--------+---------+     |
|          |                                  |               |
|          |         +----------+             | broadcast     |
|          +-------->| TX path  |             v               |
|                    +----------+              TCP clients    |
+-------------------------------------------------------------+
                               |
                 +-------------+-------------+
                 |       DDR DMA (FPGA)      |
                 |      RX: 128 MB ring      |
                 |      TX: 32 MB stream     |
                 +-------------+-------------+
                               |
                 +-------------+-------------+
                 |           AD9361          |
                 |       70 MHz - 6 GHz      |
                 |     12-bit IQ @ 61.44     |
                 +---------------------------+
```

## Daemon

The daemon is a mode-switched producer/consumer system.

- **Producer threads** pull IQ from DMA, transform it, and push results into
  lock-free SPSC ring buffers.
- **The main thread** accepts TCP connections (max 4), dispatches commands,
  and drains ring buffers to every subscribed client.
- **Mode switching** stops the active producer, tears down its buffer, and
  starts the new one.

Conceptually this is an "IIO replacement": instead of the kernel handing raw
samples to userspace, the daemon hands processed data over a small TCP
protocol.

| Mode | Producer | Output | Use case |
|------|----------|--------|----------|
| `waterfall` (0) | capture, window, FFT, fftshift, dBFS | `DATA_PSD` at target FPS | spectrum and waterfall |
| `wifi` (1) | capture, lib80211 decode | `DATA_WIFI` (full PSDU) | protocol analysis |
| `iq` (2) | raw pass-through, packed | `DATA_IQ` | host-side prototyping |
| `fm` (3) | FM demod, audio + RDS | `DATA_FM` | broadcast FM receive |

Fabric WiFi is not a fifth mode. It is `wifi` with FPGA acceleration enabled
by `CMD_SET_FPGA`, reported as `fpga_active` (configured) and
`fpga_running` (actually running).

## Radio policy

The AD9361 is full-duplex, but TX and RX **share one sample clock**. Writing
the RX rate moves the TX rate; only the `in_` node is ever written in this
tree. The rate is therefore a single owned resource with per-mode needs,
modelled in `firmware/src/mode_radio.{c,h}` and pinned by
`firmware/tests/test_mode_radio.c`:

| Owner | Rate | Why |
|-------|------|-----|
| waterfall | `sr = bw`, at least 2.1 MSPS | rate follows span |
| software WiFi | 20 MSPS | baked into lib80211 symbol timing |
| fabric WiFi | 20 MSPS | the OFDM pipeline has no rate parameter |
| FM RX | 2.1 MSPS | decimator taps are designed for it only |
| FM TX | 2.1 MSPS | hard floor for the modulator |
| IQ | 2.1-6 MSPS | rate independent of bandwidth |

The rules that matter:

- **Mode entry owns the rate.** `switch_producer` settles it before the
  producer starts, so no producer ever sees the `dma_rx_stop()` /
  `dma_rx_start()` of a rate change.
- **A mode change or a rate-claiming TX always wins.** While any TX is
  active, `CMD_SET_SAMPLE_RATE` succeeds only if it repeats the current
  rate. `CMD_SET_BW` is allowed there only when it cannot move that rate.
- **Idempotent commands are no-ops.** A redundant `CMD_SET_MODE` does not
  restart the producer. Client configuration is seeded into a fresh daemon,
  never re-pushed into a running one.
- **Fabric reconfiguration is a serialized radio transaction.** The daemon
  holds the fabric STF off, writes both LOs, records the committed channel,
  and releases. Deimos blocks its poll thread for the transaction, so a
  frame read cannot span the LO change. Shesha owns both LOs in fabric mode;
  the producer never tunes RX alone.
- **`CMD_SET_FPGA` is locked by TX.** Entering or leaving the fabric
  reinitializes the radio, so it is rejected while any TX session is active
  (`locked by active tx`). Stop the TX first.
- **FM TX displaces the RX producer** and restores the mode, rate, and
  fabric state afterwards. WiFi TX and IQ TX are concurrent with RX and
  never touch the rate.
- **Waterfall tuning knobs re-cycle only the waterfall producer.** FFT,
  window, and worker changes snapshot, stop, apply, and roll back on
  failure; they never change the radio rate.

## Threading model

```
Thread              Owns                    Writes              Reads
------------------------------------------------------------------------
main                TCP server, poll()      ring buf (pop)      g_mode, g_radio
                    command dispatch        g_radio, g_mode
waterfall producer  FFT pipeline            psd_ringbuf (push)  g_running
wifi producer       lib80211 decode         wifi_ringbuf (push) g_running
fabric producer     deimos_rx_poll()        wifi_ringbuf (push) g_running
fm producer         FM demod                fm_ringbuf (push)   g_running, g_radio
iq producer         IQ pack                 iq_ringbuf (push)   g_running, g_iq_bits
tx scheduler        DMA retrigger timer     (none)              scheduler state
```

Ring buffers use atomic write indices for lock-free SPSC synchronization.
The WiFi decoder counters are the exception: they are mutex-protected because
the main thread reads them while a producer writes them.

## Transmission

TX is command-driven, not mode-driven. Two concerns stay separate:

1. **Waveform generation** turns protocol parameters into I/Q samples.
2. **Scheduling** handles repetition, interval timing, and cyclic playback
   without knowing the waveform.

A client sends `CMD_TX` with a common scheduling header and a
protocol-specific payload. The daemon builds the waveform and hands it to
the scheduler.

Open-ended transmissions (a stream, `cyclic = 1`, or `repeat = 0xFFFF`) are
owned by the client that started them; owner disconnect stops them.
`CMD_TX_STOP` carries a target: `0xFF` stops every path, any other value
stops only the matching protocol, and a non-matching target is a safe no-op.

Adding a protocol takes three steps:

1. Add a `TX_PROTO_*` constant and sub-payload format.
2. Implement `foo_tx_build()` to produce I/Q samples.
3. Dispatch it from the `CMD_TX` handler into the scheduler.

Scheduling semantics and sub-payload layouts are in `docs/protocol.md`.

## Platform and accelerators

**Decision:** styx owns the HAL (register mapping, AD9361 sysfs). Child
bitstreams add accelerator AXI peripherals at standardized addresses. Styx
maps the memory generically; accelerator libraries interpret the registers.

```
shesha-daemon
   waterfall_mode --+
   wifi_mode -------+-- links styx HAL (hal.c)
   wifi_fpga -------+
         |          |
         +-- links deimos_rx (accelerator library)
```

`PROJECT_ID` identifies the flashed bitstream:

| Value | ASCII | Meaning |
|-------|-------|---------|
| `0x53545958` | `STYX` | base infrastructure only |
| `0x57494649` | `WIFI` | deimos fabric WiFi decode |

Why the layering holds:

- Styx stays generic. It does not know about WiFi or LoRa; it maps address
  space.
- Accelerator libraries define their own register semantics and call
  `hal_reg_read()` / `hal_reg_write()`.
- Shesha probes `PROJECT_ID` at runtime, so one binary supports every known
  accelerator.
- Accelerator bitstreams are siblings and reuse the same two accelerator
  regions with different semantics.

Shesha links styx `hal.c` directly. The register map lives in
`firmware/platforms/styx/firmware/src/hal.h`.

## Subsystems

**Waterfall** captures a block, applies the selected window, runs an FFT,
shifts DC to the centre, and emits dBFS bins as `DATA_PSD`. Only bins cross
USB2, so the full span is viewable at reduced coverage. A second worker can
run on the other core. Details in `docs/psd-waterfall.md`.

**WiFi** decodes legacy 802.11a/g at 20 MSPS. The software path runs
lib80211 over captured IQ; the fabric path polls deimos and emits the same
`DATA_WIFI` frames. Both produce full PSDUs. See the mode notes in
`README.md`.

**FM** is the only mode that is both a producer and a TX protocol. RX runs
DC block, discriminator, a three-stage decimating FIR, and de-emphasis; an
RDS decoder taps the first stage. TX runs pre-emphasis, phase modulation,
and int12 packing, streamed in chunks. FM TX suspends and restores the RX
producer around the shared clock. Design and measurements are in
`docs/fm-rx.md` and `docs/fm-tx.md`.

**IQ** is raw pass-through with no float roundtrip, usable in both
directions at once. TX and RX are admitted against a shared USB2 budget
rather than throttled. Details in `docs/iq.md`.

## Source layout

```
firmware/src/          daemon: main loop, producers, protocol, radio, TX
firmware/platforms/    styx HAL/DMA submodule, deimos fabric submodule
firmware/extern/       lib80211 submodule
client/shesha/         Python client: protocol, DaemonClient, CLI, TUI
client/c/              dependency-free C client for the IQ path
examples/              runnable protocol examples
docs/                  protocol manual and subsystem notes
scripts/               design and measurement tools
```

Entry points: `firmware/src/main.c` (server and dispatch),
`producer.h/c` (mode registry), `protocol.h/c` (wire), `mode_radio.h/c`
(rate policy), `tx_session.h/c` (clock claim).

## Hardware

| Component | Spec |
|-----------|------|
| Platform | ADALM-PLUTO (Zynq 7010 + AD9361) with a styx-based bitstream |
| Frequency | 70 MHz - 6 GHz |
| ADC/DAC | 12-bit, up to 61.44 MSPS |
| DDR RX buffer | 128 MB ring |
| DDR TX buffer | 32 MB, one-shot, cyclic, or streamed |
| Radio control | sysfs |
| TCP port | 5555, max 4 clients |

### Dual-core is a requirement

The Zynq 7010 has two Cortex-A9 cores, but the Pluto boot environment sets
`maxcpus=1`. FM TX and the RDS-enabled FM RX producer do not fit one core,
and the second PSD worker needs the other core. Shesha therefore requires
CPU1 online.

The styx submodule provides
`firmware/platforms/styx/bin/enable_dual_core.sh`, which brings CPU1 online
and persists `maxcpus=2` in u-boot. Verify with `nproc` reporting 2. The
runtime write alone is lost on reboot.

## Where to read next

| Document | Answers |
|----------|---------|
| `docs/protocol.md` | wire format, message catalog, status semantics |
| `docs/iq.md` | IQ paths, packing, link budget, measured limits |
| `docs/psd-waterfall.md` | FFT pipeline, coverage, workers |
| `docs/fm-rx.md` | FM receive and RDS design and measurements |
| `docs/fm-tx.md` | FM transmit chain and streaming |
| `docs/tui.md` | TUI internals |
| `README.md` | install, modes, CLI, usage examples |
