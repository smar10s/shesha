/* firmware/src/iq_mode.h — raw IQ RX producer (DAEMON_MODE_IQ) */
#pragma once

#include "producer.h"

#define IQ_POP_MAX 64

int  iq_mode_enter(void);
void iq_mode_exit(void);
bool iq_mode_running(void);
bool iq_mode_active(void);

/* The poll loop sets this from the live subscription masks.  With no IQ
 * subscriber the producer drops chunks without packing, leaving the link
 * and a core to TX. */
void iq_mode_set_rx_wanted(bool wanted);

/* Drop queued IQ slots.  Called when the last subscriber leaves so the
 * next subscriber does not start on data captured while nobody was
 * reading. */
void iq_mode_flush(void);

void drain_iq(const drain_ctx_t *ctx);
