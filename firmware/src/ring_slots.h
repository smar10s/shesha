/* firmware/src/ring_slots.h — slot types for the unified SPSC rings.
 *
 * Each producer pushes one of these structs into its spsc_ring_t.
 * Timestamps are part of the slot so the ring is fully type-erased.
 */
#pragma once

#include "wifi_protocol.h"   /* wifi_rx_frame_t, WIFI_MAX_PSDU */
#include "rds_state.h"
#include <stddef.h>
#include <stdint.h>
/* --- Ring capacities (shared between mode modules and main.c) --- */
#define RING_SLOTS_PSD   256
#define RING_SLOTS_WIFI  256
#define RING_SLOTS_FM    256

/* The FFT sizes CMD_SET_FFT_SIZE accepts.  psd_slot_size() must keep every
 * allowed stride aligned. */
#define PSD_FFT_SIZE_MIN  256
#define PSD_FFT_SIZE_MAX  8192

/* --- PSD accumulation mode (consumer-side reduction) ---
 *
 * The producer is un-paced, so the drain usually has several frames to turn
 * into the one it broadcasts.  This picks how.  The reduction runs in the
 * LINEAR POWER domain (see wf_mag.h) and the drain converts to dB once,
 * afterwards. */
typedef enum {
    ACCUM_NONE = 0,  /* latest snapshot only; the rest are discarded */
    ACCUM_MAX  = 1,  /* per-bin maximum (peak-hold) */
    ACCUM_AVG  = 2,  /* per-bin mean power */
} accum_mode_t;

/* --- PSD slot ---
 * Variable size: psd_slot_size(fft_size) bytes per slot.
 * Layout: double ts, uint64 frame_index, uint64 freq_hz, uint32
 * sample_rate_hz, then fft_size floats of LINEAR POWER (not dB).
 *
 * `frame_index` is the frame's position in the global order the shared claim
 * defines (wf_claim.h).  It is what lets the drain merge two producers, and
 * it is what the wire carries as the sequence number -- a ring's own write
 * counter cannot, because two rings' counters are not comparable.
 *
 * freq_hz and sample_rate_hz are stamped when the slot is produced, so a
 * frame drained after a retune still describes the capture it came from. */
typedef struct {
    double   ts;
    uint64_t frame_index;
    uint64_t freq_hz;
    uint32_t sample_rate_hz;
    float    power[];  /* C99 flexible array: fft_size floats, linear power */
} psd_slot_t;

static inline size_t psd_slot_size(size_t fft_size)
{
    return sizeof(psd_slot_t) + fft_size * sizeof(float);
}

/* The drain walks the pop buffer by psd_slot_size() and reads `power` through
 * the struct, so the two must agree.  Getting this wrong offsets every slot
 * and turns the spectrum into noise with nothing to indict it.  The struct's
 * tail padding is what makes the stride 8-byte aligned for the power-of-two
 * FFT sizes the command accepts. */
_Static_assert(offsetof(psd_slot_t, power) == sizeof(double) + sizeof(uint64_t)
                                            + sizeof(uint64_t) + sizeof(uint32_t),
               "psd_slot_size must match the psd_slot_t layout");
_Static_assert(sizeof(psd_slot_t) % _Alignof(psd_slot_t) == 0,
               "psd_slot_t size must be a multiple of its alignment");
_Static_assert((sizeof(psd_slot_t)
                + PSD_FFT_SIZE_MIN * sizeof(float)) % _Alignof(psd_slot_t) == 0,
               "minimum PSD slot stride must preserve psd_slot_t alignment");
_Static_assert((sizeof(psd_slot_t)
                + PSD_FFT_SIZE_MAX * sizeof(float)) % _Alignof(psd_slot_t) == 0,
               "maximum PSD slot stride must preserve psd_slot_t alignment");

/* --- WiFi slot --- */
typedef struct {
    double          ts;
    wifi_rx_frame_t frame;
} wifi_slot_t;

/* --- FM slot --- */
#define FM_AUDIO_FRAME_SIZE 1024

typedef struct {
    float    audio[FM_AUDIO_FRAME_SIZE];
    uint32_t n_samples;
    uint32_t audio_rate;
    rds_state_t rds;
} fm_audio_frame_t;

typedef struct {
    double           ts;
    fm_audio_frame_t frame;
} fm_slot_t;

/* --- IQ slot ---
 * Raw wire-format samples.  The payload buffer is sized for the widest
 * (12-bit) packing; 8-bit frames use the first IQ_CHUNK_SAMPLES * 2 bytes
 * of it.  `bits` records the width at capture time so a mid-stream knob
 * change never mis-frames the drain. */
#define IQ_CHUNK_SAMPLES 16384
#define RING_SLOTS_IQ    256

typedef struct {
    double   ts;
    uint16_t n;
    uint8_t  bits;
    uint64_t freq_hz;
    uint32_t sample_rate_hz;
    uint8_t  packed[IQ_CHUNK_SAMPLES * 3];
} iq_slot_t;
