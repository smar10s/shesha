/* radio_txn.h — one ordered operation for producer and sample-clock changes.
 *
 * The AD9361 shares one sample clock between RX and TX, and DMA RX has a
 * single read stream.  Two rules follow:
 *
 *   1. No radio setting may change while a producer is reading the stream.
 *   2. A failed change must restore both the radio and the producer that
 *      was running before it.
 *
 * Every command that changes mode, bandwidth, sample rate, or the producer
 * variant goes through this module so the ordering and rollback live in one
 * place.  The hardware side effects are callbacks, which keeps the sequence
 * host-testable.
 */
#pragma once

#include "producer.h"

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    /* Settle the target producer's mode constraints (mode or FPGA switch). */
    RADIO_TXN_ENTRY = 0,
    /* Explicit bandwidth on the current producer; the rate follows the mode. */
    RADIO_TXN_BW,
    /* Explicit sample rate, used by CMD_SET_SAMPLE_RATE and TX resume. */
    RADIO_TXN_SR,
} radio_txn_kind_t;

/* Hardware side effects.  `ctx` is opaque to this module. */
typedef struct {
    int (*set_bw)(uint32_t bw_hz, uint32_t *actual_hz, void *ctx);
    int (*set_sr)(uint32_t sr_hz, uint32_t *actual_hz, void *ctx);
} radio_txn_io_t;

typedef struct {
    radio_txn_kind_t kind;
    /* Producer that may need quiescing; NULL when none is active. */
    const producer_ops_t *prev;
    /* Producer to run afterwards (or the current one for BW/SR). */
    const producer_ops_t *next;
    /* True only when `prev` is actually running. */
    bool prev_running;
    /* Cycle `prev` even when it is also `next` (forced re-entry). */
    bool force;
    /* Requested bandwidth or sample rate for BW/SR. */
    uint32_t value;
    /* Radio state before the transaction. */
    uint32_t cur_bw;
    uint32_t cur_sr;
} radio_txn_req_t;

typedef enum {
    RADIO_TXN_OK = 0,
    /* The request is malformed or violates the target mode's constraints. */
    RADIO_TXN_INVALID,
    RADIO_TXN_BW_FAILED,
    RADIO_TXN_SR_FAILED,
    RADIO_TXN_ENTER_FAILED,
} radio_txn_status_t;

typedef struct {
    /* Radio state after the transaction, or the restored state on failure. */
    uint32_t bw;
    uint32_t sr;
} radio_txn_result_t;

/* Run one transaction.  Returns RADIO_TXN_OK on success.  On failure the
 * radio is restored and the previous producer re-entered when it was
 * running (or when `force` says it was displaced by a TX cancellation). */
radio_txn_status_t radio_txn_run(const radio_txn_req_t *req,
                                 const radio_txn_io_t *io, void *io_ctx,
                                 radio_txn_result_t *out);
