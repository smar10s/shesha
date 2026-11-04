/* firmware/src/producer.h — producer vtable + registry */
#pragma once

#include "mode_radio.h"
#include "protocol.h"  /* for DAEMON_MODE_* */
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

typedef void (*producer_frame_sink_t)(const uint8_t *frame, size_t len,
                                      void *user);

typedef struct {
    uint8_t               *sendbuf;
    producer_frame_sink_t  sink;
    void                  *sink_user;
    double                *last_broadcast;
    uint8_t                target_fps;
    uint8_t                accum;
} drain_ctx_t;

typedef struct {
    const char     *name;          /* "waterfall_sw", "wifi_fpga", etc. */
    int             mode;          /* DAEMON_MODE_WATERFALL, etc. */
    bool            requires_fpga; /* needs compatible bitstream */
    int           (*enter)(void);
    void          (*exit)(void);
    bool          (*running)(void); /* is the producer actually running now? */
    bool          (*active)(void);  /* live or starting, before ready */
    void          (*drain)(const drain_ctx_t *ctx);
    mode_radio_req_t radio;        /* bw/sr constraints for this mode */
} producer_ops_t;

/* Registry queries (NULL = not found) */
const producer_ops_t *producer_for_mode(int mode);
const producer_ops_t *producer_fpga_for_mode(int mode);

/* True if `active` is a producer for `mode` — SW or FPGA variant.
 * The CMD_SET_MODE no-op guard: a redundant mode set must not tear the
 * running producer down (that is how a reconnect silently kills the
 * FPGA variant). */
bool producer_mode_active(const producer_ops_t *active, int mode);

/* True when the producer's own threads/state say it is producing now.
 * Reporting configuration is not the same thing: a mode remains configured
 * while an exclusive TX has suspended its producer. */
bool producer_is_running(const producer_ops_t *p);

/* True only when this is the fabric variant and its thread is running.
 * A suspended producer stays configured, so `requires_fpga` alone is not
 * enough: radio commands must use the normal path while the fabric is
 * stopped. */
bool producer_is_fabric_running(const producer_ops_t *p);

/* The radio policy to enforce right now, or NULL if no producer owns the
 * radio.  A suspended producer stays configured but must not lock AGC or
 * gain during the exclusive TX that displaced it. */
const mode_radio_req_t *producer_active_radio(const producer_ops_t *p);

/* True when the producer's thread and resources are live or starting.
 * This is the lifecycle question mode switching must ask: between enter()
 * returning and the producer's ready flag being set, `running` is false but
 * the thread already owns the DMA and the ring. */
bool producer_is_active(const producer_ops_t *p);

/* All registered producers */
extern const producer_ops_t g_producers[];
extern const int g_producer_count;
