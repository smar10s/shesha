#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "hal.h"
#include "adc_clip.h"

/*
 * Windowed DDR unpack for the waterfall producer.
 *
 * The production path is convert-then-window: the platform's
 * convert_rx_to_float() unpacks a frame, then window_apply() makes a second
 * pass over both I/Q arrays to multiply it.  That second pass re-reads and
 * re-writes 2N floats for no reason — the unpack loop already has each sample
 * in a register, so the window factor can be applied right there.
 *
 * Kept in shesha (not the styx convert.c) because the platform convert is
 * shared with FM/WiFi/IQ and stays scalar; this is only ever the waterfall.
 *
 * Same memcpy-staging discipline as convert.c: the DDR reserve is mapped
 * O_SYNC (uncached), so bulk-copy through a cached stage rather than reading
 * words one at a time through the volatile pointer.  `win` indexes from
 * `ddr_buf`, so a caller reading a wrapped second chunk passes win +
 * first_chunk.  Ordering against the DMA write pointer is the caller's job
 * (state a barrier after the WR_PTR check, as dma_rx.c does).
 */
#define WF_RX_SCALE      (1.0f / 2048.0f)
#define WF_CONVERT_STAGE 4096

static inline void wf_convert_windowed(const volatile uint32_t *ddr_buf,
                                       size_t n_samples, const float *win,
                                       float *out_real, float *out_imag,
                                       uint64_t *clip_count)
{
    uint32_t stage[WF_CONVERT_STAGE];
    uint64_t clips = clip_count ? *clip_count : 0;

    for (size_t done = 0; done < n_samples; ) {
        size_t n = n_samples - done;
        if (n > WF_CONVERT_STAGE)
            n = WF_CONVERT_STAGE;

        memcpy(stage, (const uint32_t *)ddr_buf + done, n * sizeof(uint32_t));

        for (size_t i = 0; i < n; i++) {
            uint32_t word = stage[i];
            int16_t re = IQ_REAL(word);
            int16_t im = IQ_IMAG(word);
            float w = win[done + i];
            clips += adc_clip_is_raw12(word);
            clips += adc_clip_is_raw12(word >> 12);
            out_real[done + i] = (float)re * WF_RX_SCALE * w;
            out_imag[done + i] = (float)im * WF_RX_SCALE * w;
        }
        done += n;
    }
    if (clip_count)
        *clip_count = clips;
}
