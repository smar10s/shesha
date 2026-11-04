#include "deimos_rx.h"
#include "wifi_fpga.h"
#include "accel.h"
#include "radio.h"
#include "spsc_ring.h"
#include "ring_slots.h"
#include "protocol.h"
#include "hal.h"
#include "thread_stats.h"
#include <stdatomic.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>

/* Pipeline shutdown registers (offsets within generic accel regions).
 * deimos_rx_cleanup() calls hal_cleanup() which would destroy the shared
 * HAL — we disable the pipeline manually instead. */
#define FABRIC_STF_ENABLE    (REG_ACCEL_B_BASE + 0x04)
#define FABRIC_FIFO_CONTROL  (REG_ACCEL_A_BASE + 0x0C)
#define FABRIC_FIFO_FLUSH    (1 << 0)

/* LO PLL settling before STF is enabled.  Matches the deimos retune path;
 * enabling STF during the transient can leave the detector primed on ADC
 * garbage. */
#define FABRIC_RETUNE_SETTLE_US 50000

extern atomic_bool g_running;
extern spsc_ring_t *g_wifi_ringbuf;
extern radio_state_t g_radio;

static pthread_t g_producer_thread;
static atomic_bool g_producer_running = false;
/* The poll thread's own liveness.  It drops on thread exit or a deimos
 * error, while g_producer_running stays true so exit() still joins. */
static atomic_bool g_producer_ready = false;
static bool g_initialized = false;
static bool g_calibration_done = false;
static _Atomic uint32_t g_frames_delivered;
static _Atomic uint32_t g_fcs_good;

extern double monotonic_sec(void);

int wifi_fpga_freq_to_channel(uint64_t hz)
{
    /* Round to the nearest channel centre.  The AD9361 normally reports a
     * centre a few Hz below the requested value (2436.999998 for 2437 MHz);
     * truncating to whole MHz would select the previous 5 MHz channel and
     * detune the fabric receiver by 5 MHz. */
    if (hz >= 2412000000ULL && hz <= 2484000000ULL) {
        /* Channel 14 sits alone at 2484, 12 MHz above channel 13. */
        if (hz >= 2478000000ULL)
            return 14;
        int ch = (int)((hz - 2412000000ULL + 2500000ULL) / 5000000ULL) + 1;
        return ch > 13 ? 13 : ch;
    }
    if (hz >= 5000000000ULL && hz <= 5900000000ULL)
        return (int)((hz - 5000000000ULL + 2500000ULL) / 5000000ULL);
    return 1;
}

static bool probe(void)
{
    uint32_t pid = hal_reg_read(REG_BUILD_ID_PROJECT);
    if (pid == PROJECT_ID_WIFI) return true;
    fprintf(stderr, "wifi_fpga: bitstream PROJECT_ID mismatch "
            "(expected 0x%08X, got 0x%08" PRIX32 ")\n",
            PROJECT_ID_WIFI, pid);
    return false;
}

/* Disable the fabric without deimos_rx_cleanup(): that calls hal_cleanup()
 * and destroys the HAL every other mode still shares.  Used by every exit
 * and failure path so they all leave the pipeline in the same state. */
static void fabric_disable(void)
{
    hal_reg_write(FABRIC_STF_ENABLE, 0);
    usleep(12000);  /* watchdog drain */
    hal_reg_write(FABRIC_FIFO_CONTROL, FABRIC_FIFO_FLUSH);
    usleep(1000);
    hal_reg_write(FABRIC_FIFO_CONTROL, 0);
}

static void *run(void *arg)
{
    (void)arg;
    thread_stats_name_self("wifi-fpga-producer");
    atomic_store(&g_producer_ready, true);
    deimos_rx_frame_t dframe;

    while (atomic_load(&g_running) && atomic_load(&g_producer_running)) {
        int rc = deimos_rx_poll(&dframe);
        if (rc == 1) {
            wifi_slot_t slot;
            slot.ts = monotonic_sec();
            radio_capture_snapshot(&slot.frame.freq_hz, NULL);
            slot.frame.rate_code = dframe.rate_code;
            slot.frame.rate_mbps = dframe.rate_mbps;
            slot.frame.length = dframe.length;
            slot.frame.fcs_ok = dframe.fcs_ok;
            slot.frame.rx_gain = (float)hal_ad9361_get_rx_gain();
            slot.frame.psdu_len = dframe.psdu_len;
            memcpy(slot.frame.psdu, dframe.psdu, dframe.psdu_len);
            spsc_ring_push(g_wifi_ringbuf, &slot);
            atomic_fetch_add(&g_frames_delivered, 1);
            if (dframe.fcs_ok)
                atomic_fetch_add(&g_fcs_good, 1);
        } else if (rc == 0) {
            usleep(100);
        } else {
            break;
        }
    }
    atomic_store(&g_producer_ready, false);
    return NULL;
}

int wifi_fpga_enter(void)
{
    /* Idempotency guard, matching the other producers.  A second enter
     * while the poll thread is live would overwrite g_producer_thread and
     * leak the old thread against a freed ring. */
    if (atomic_load(&g_producer_running)) return 0;

    if (!probe()) return -1;

    deimos_radio_config_t radio_cfg = {
        .channel = wifi_fpga_freq_to_channel(g_radio.freq_hz),
        .bandwidth_mhz = 28,
        .agc_mode = DEIMOS_AGC_FAST_ATTACK,
        .skip_calibration = g_calibration_done,
    };
    deimos_pipeline_config_t pipe_cfg = DEIMOS_PIPELINE_LIVE_DEFAULT;

    if (deimos_radio_init(&radio_cfg) != 0) {
        fprintf(stderr, "wifi_fpga: deimos_radio_init failed\n");
        return -1;
    }

    /* Pipeline is armed with STF disabled.  Shesha owns both LOs: write
     * the exact frequency the client asked for, not the 5 MHz channel
     * centre, before letting the poll thread go live. */
    deimos_pipeline_arm(&pipe_cfg);
    uint64_t actual = 0;
    if (radio_set_freq(&g_radio, g_radio.freq_hz, &actual) != 0) {
        fprintf(stderr, "wifi_fpga: radio_set_freq failed\n");
        fabric_disable();
        return -1;
    }
    usleep(FABRIC_RETUNE_SETTLE_US);
    deimos_rx_note_channel(wifi_fpga_freq_to_channel(actual));
    if (deimos_stf_gate(DEIMOS_TRIGGER_NONE) != 0) {
        fprintf(stderr, "wifi_fpga: deimos_stf_gate failed\n");
        fabric_disable();
        return -1;
    }
    if (!g_calibration_done)
        g_calibration_done = true;
    g_initialized = true;

    g_wifi_ringbuf = spsc_ring_create(sizeof(wifi_slot_t), RING_SLOTS_WIFI);
    if (!g_wifi_ringbuf) {
        fprintf(stderr, "wifi_fpga: wifi ringbuf create failed\n");
        fabric_disable();
        g_initialized = false;
        return -1;
    }

    atomic_store(&g_frames_delivered, 0);
    atomic_store(&g_fcs_good, 0);
    atomic_store(&g_producer_ready, false);
    atomic_store(&g_producer_running, true);
    if (pthread_create(&g_producer_thread, NULL, run, NULL) != 0) {
        atomic_store(&g_producer_running, false);
        spsc_ring_destroy(g_wifi_ringbuf);
        g_wifi_ringbuf = NULL;
        fabric_disable();
        g_initialized = false;
        return -1;
    }
    printf("mode: wifi (fpga)\n");
    return 0;
}

void wifi_fpga_exit(void)
{
    if (atomic_load(&g_producer_running)) {
        atomic_store(&g_producer_running, false);
        pthread_join(g_producer_thread, NULL);
    }

    if (g_initialized) {
        fabric_disable();
        g_initialized = false;
    }

    if (g_wifi_ringbuf) {
        spsc_ring_destroy(g_wifi_ringbuf);
        g_wifi_ringbuf = NULL;
    }
}

bool wifi_fpga_running(void)
{
    return atomic_load(&g_producer_running) && atomic_load(&g_producer_ready);
}

bool wifi_fpga_active(void)
{
    return atomic_load(&g_producer_running);
}

void wifi_fpga_get_diag(uint32_t *frames, uint32_t *fcs_ok)
{
    uint32_t good = atomic_load_explicit(&g_fcs_good,
                                         memory_order_seq_cst);
    uint32_t delivered = atomic_load_explicit(&g_frames_delivered,
                                              memory_order_seq_cst);
    if (frames)
        *frames = delivered;
    if (fcs_ok)
        *fcs_ok = good;
}
