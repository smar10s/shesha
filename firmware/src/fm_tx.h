#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#include "rds_tx.h"

/*
 * FM transmit.  Audio arrives already pre-emphasised and normalised — see
 * fm_mod.h for why that belongs to the client rather than here.
 */

/* fm_tx_stream_push() return codes.  "Ring full" is deliberately distinct
 * from "error": it is the normal backpressure signal, and the caller should
 * retry rather than tear the stream down. */
#define FM_TX_PUSH_OK     0
#define FM_TX_PUSH_ERR   (-1)
#define FM_TX_PUSH_FULL  (-2)

typedef struct {
    bool     active;           /* a stream exists and is still consuming */
    bool     end_of_stream;    /* client has sent CMD_TX_STREAM_END */
    uint32_t ring_fill;        /* audio samples buffered, not yet modulated */
    uint32_t ring_size;        /* audio ring capacity, samples */
    uint32_t audio_rate;
    uint64_t audio_consumed;   /* audio samples modulated so far */
    uint64_t starved_samples;  /* carrier emitted for want of audio */
    uint32_t push_rejects;     /* pushes refused because the ring was full */
} fm_tx_stream_stats_t;

/* One-shot: modulate a complete buffer and play it once. */
int  fm_tx_start(const float *audio, uint32_t audio_rate,
                 uint32_t deviation_hz, uint32_t dac_rate,
                 size_t n_audio_samples, const rds_tx_config_t *rds);

/*
 * Streaming: start with an optional first chunk, then push more as it
 * arrives.  Audio is buffered in a bounded lock-free SPSC ring — the
 * producer is the daemon's poll loop and the consumer is the TX thread, so
 * neither ever blocks the other.
 */
int  fm_tx_stream_start(const float *first_chunk, uint32_t n,
                        uint32_t audio_rate, uint32_t deviation_hz,
                        uint32_t dac_rate, const rds_tx_config_t *rds);

/*
 * Update the RDS config of a running stream (CMD_SET_RDS).  Enabling turns
 * the subcarriers on and disabling turns them off, so a live toggle works.
 *
 * @return 0, or -1 if no streaming FM TX is active.
 */
int  fm_tx_set_rds(const rds_tx_config_t *cfg);

/*
 * Append audio.  Never blocks and never sleeps: it runs on the single
 * poll-loop thread, so blocking here would stall every other client and
 * the PSD broadcast.
 *
 * @return FM_TX_PUSH_OK, or FM_TX_PUSH_FULL if the caller should retry
 *         later, or FM_TX_PUSH_ERR if there is no active stream.
 */
int  fm_tx_stream_push(const float *chunk, uint32_t n);

/* Mark end of audio.  The stream finishes once the ring drains. */
void fm_tx_stream_end(void);

/*
 * Discard the audio queued in the ring but not yet modulated, without
 * stopping the stream.  Used to seek: the carrier and the DMA engine keep
 * running, so the jump costs only the already-modulated audio still in the
 * DDR ring, not a full restart.
 *
 * @return 0, or -1 if no streaming FM TX is active.
 */
int  fm_tx_stream_flush(void);

/* Release the stream object.  Only safe once the TX thread is joined, so
 * call tx_scheduler_stop() first. */
void fm_tx_stream_release(void);

void fm_tx_stream_stats(fm_tx_stream_stats_t *out);
