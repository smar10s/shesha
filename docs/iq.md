# IQ Mode

IQ is the raw pass-through mode: the daemon moves samples between the DDR
DMA ring and the host with as little interpretation as possible. It is both
a producer and a TX stream, and a client may use both directions at once.

Unlike FM TX, an IQ transmission does not displace the RX producer and does
not claim the sample clock exclusively. A mode change while an IQ stream is
running cancels the stream first, because it would otherwise pull the clock
out from under the TX thread.

## RX path

`DDR ring -> packed int12/int8 -> DATA_IQ`

There is no float roundtrip. Words are memcpy-staged out of the raw mapping
and shift/OR-packed straight to the wire format (`firmware/src/iq_pack.h`).
Each frame carries 16384 samples through a 256-slot SPSC ring.

While no client subscribes to `DATA_IQ`, the producer advances the DMA read
pointer without reading or packing, leaving that core to TX.

## TX path

`host bytes -> byte ring -> unpack -> dma_tx_stream_feed_fixed`

The client pushes packed wire-format bytes. The first push implicitly starts
the stream at the mode's sample rate. The 1 MB byte ring is bounded, so a
client ahead of realtime gets `RESP_RETRY` backpressure. `CMD_IQ_TX_STREAM_END`
sets the end flag and the TX thread drains.

## Link budget

RX and TX share the USB2 link as well as the sample clock. Duplex is
admitted, never throttled: with any RX subscriber, TX starts only if
`(rx_streams * rx_bytes + tx_bytes) * rate` fits a **16 MB/s combined RX+TX
budget**. That is a single admission figure, not a per-format ceiling; the
measured one-way ceilings are ~15 MB/s at 8-bit and ~19.5 MB/s at 12-bit
(`README.md`).

At the 6 MSPS cap that makes 6 MSPS receive-only; 8-bit 4+4 is the measured
duplex point, and 12-bit 4+4 is refused. With no RX subscriber the producer
stays idle, so the policy cap is an admission limit, not a throughput
guarantee. The budget also gates `CMD_SET_IQ_BITS` and `CMD_SUBSCRIBE` while
TX is active.

## Measured limits

With the RX producer idle, TX-only tops out near 5.0-5.9 MSPS at 8 bit and
4.9-5.3 MSPS at 12 bit; the remaining limit is the synchronous push path,
not the link. The C client is clean across that range. The Python client
sustains 100% below the 6 MSPS cap and 98.4% at 6 MSPS 12-bit; the TUI and
CLI can lag only at that edge. This is a known ceiling, not a general
throughput loss.

RX-only multi-subscriber load is unguarded: every subscriber receives its own
copy of `DATA_IQ`, and the shared budget accounts for the count only when TX
is active.

## Packing

One format serves both directions; 12-bit is the native width.

| bits | bytes/sample | layout |
|------|--------------|--------|
| 12 | 3 | `b0 = I[7:0]`, `b1 = I[11:8] or Q[3:0]<<4`, `b2 = Q[11:4]` |
| 8 | 2 | `b0 = I[11:4]`, `b1 = Q[11:4]` (top 8 bits of the 12-bit value) |

`CMD_SET_IQ_BITS` (default 12) applies to both directions and is reported as
`iq_bits` in `RESP_STATUS`. The width in force at capture time is stamped in
every `DATA_IQ` frame, so a mid-stream knob change never mis-frames the
drain.

## Clients

The Python client exposes `DaemonClient.iq_rx()`. The C client
(`client/c/`) is scoped to this path: connection, synchronous commands,
`DATA_IQ` reception, and the IQ TX byte stream. Both are demonstrated in
`examples/`.

Wire layout: `docs/protocol.md`. Throughput claims above were measured on
the Pluto and are not enforced by tests.
