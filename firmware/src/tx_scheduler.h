#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <sys/types.h>   /* ssize_t */

#include "tx_flags.h"

int  tx_scheduler_init(void);
void tx_scheduler_cleanup(void);

int  tx_scheduler_start(const float *real, const float *imag,
                        size_t n_samples, uint8_t flags,
                        uint16_t repeat, uint32_t interval_us);

/*
 * Streaming producer callback.
 *
 * Return value:
 *   > 0   samples written to real/imag (at most max_chunk)
 *   0     end of stream: the client closed it and nothing is left
 *   < 0   starved: no data available right now, but the stream is still
 *         open.  The scheduler waits and calls again.
 *
 * The distinction matters.  A real-time source that can synthesise fill
 * (FM's unmodulated carrier) pads and never returns < 0, because the DAC
 * must be kept fed.  A finite vector (IQ) must instead block: padding a
 * discontinuous waveform with silence tears the very packet the client
 * asked to transmit.
 */
typedef ssize_t (*tx_stream_producer)(float *real, float *imag,
                                      size_t max_chunk, void *user);
typedef void (*tx_stream_cleanup)(void *user);

int  tx_scheduler_start_stream(tx_stream_producer produce,
                               tx_stream_cleanup cleanup,
                               void *user, uint32_t dac_rate);

/*
 * Streaming TX telemetry.
 *
 * `emitted` is the ground truth for output rate: the FPGA drain FSM only
 * advances RD_PTR when it actually hands a sample to the DAC, so
 * emitted / elapsed is the true output rate and any deficit against
 * dac_rate is exactly the time-stretch a receiver hears as the audio
 * dragging.
 *
 * These are snapshots published by the TX thread.  The poll loop must read
 * them from here rather than calling dma_tx_stream_*() itself: the
 * monotonic depth accumulator in dma_tx.c keeps state across calls and is
 * not thread safe.
 */
typedef struct {
    bool     active;
    uint32_t dac_rate;
    uint64_t fed;          /* samples written into the DDR ring        */
    uint64_t emitted;      /* samples the DAC has actually output      */
    uint64_t elapsed_us;   /* since dma_tx_stream_trigger()            */
    int32_t  depth;        /* signed DDR depth; negative = underrun    */
    uint32_t max_depth;    /* high-water depth actually reached        */
    uint32_t high_water;   /* configured latency bound, samples        */
    uint32_t underruns;    /* polls that saw depth < 0                 */
} tx_stream_stats_t;

void tx_scheduler_stream_stats(tx_stream_stats_t *out);

/* Ask the TX thread to finish, without waiting for it.  The drain may take
 * hundreds of milliseconds, so a caller on the command path must not join;
 * poll tx_scheduler_busy() and call tx_scheduler_join() once it is false. */
void tx_scheduler_request_stop(void);

/* Set the stop flag and join.  Blocks for as long as the thread takes to
 * drain, so only callers that must guarantee teardown use it. */
void tx_scheduler_stop(void);

/* Join a thread that has already finished (tx_scheduler_busy() is false).
 * Reaps its stack and thread_live flag without waiting for hardware work. */
void tx_scheduler_join(void);

bool tx_scheduler_busy(void);

/* True if the most recent one-shot/cyclic/repeat start failed because the
 * DMA engine would not start.  The client sees the command succeed before
 * that result is known, so this is how it learns the transmission never
 * began.  Cleared by the next tx_scheduler_start() or
 * tx_scheduler_start_stream(). */
bool tx_scheduler_start_failed(void);
