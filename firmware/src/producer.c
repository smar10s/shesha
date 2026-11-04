/* firmware/src/producer.c */
#include "producer.h"

#include "waterfall_mode.h"
#include "wifi_mode.h"
#include "fm_mode.h"
#include "iq_mode.h"
#include "wifi_fpga.h"

const producer_ops_t g_producers[] = {
    {
        .name         = "waterfall_sw",
        .mode         = DAEMON_MODE_WATERFALL,
        .requires_fpga = false,
        .enter        = waterfall_mode_enter,
        .exit         = waterfall_mode_exit,
        .running      = waterfall_mode_running,
        .active       = waterfall_mode_active,
        .drain        = drain_waterfall,
        .radio        = { .bw_min = 200000, .bw_max = 56000000 },
    },
    {
        .name         = "wifi_sw",
        .mode         = DAEMON_MODE_WIFI,
        .requires_fpga = false,
        .enter        = wifi_mode_enter,
        .exit         = wifi_mode_exit,
        .running      = wifi_mode_running,
        .active       = wifi_mode_active,
        .drain        = drain_wifi,
        .radio        = { .bw_fixed = 20000000, .sr_fixed = 20000000 },
    },
    {
        .name         = "wifi_fpga",
        .mode         = DAEMON_MODE_WIFI,
        .requires_fpga = true,
        .enter        = wifi_fpga_enter,
        .exit         = wifi_fpga_exit,
        .running      = wifi_fpga_running,
        .active       = wifi_fpga_active,
        .drain        = drain_wifi,
        .radio        = { .bw_fixed = 28000000, .sr_fixed = 20000000,
                          .agc_fixed = MODE_RADIO_AGC_FAST_ATTACK },
    },
    {
        .name         = "fm_sw",
        .mode         = DAEMON_MODE_FM,
        .requires_fpga = false,
        .enter        = fm_mode_enter,
        .exit         = fm_mode_exit,
        .running      = fm_mode_running,
        .active       = fm_mode_active,
        .drain        = drain_fm,
        .radio        = { .bw_fixed = 200000, .sr_fixed = 2100000 },
    },
    {
        .name         = "iq_sw",
        .mode         = DAEMON_MODE_IQ,
        .requires_fpga = false,
        .enter        = iq_mode_enter,
        .exit         = iq_mode_exit,
        .running      = iq_mode_running,
        .active       = iq_mode_active,
        .drain        = drain_iq,
        .radio        = { .bw_min = 200000, .bw_max = 8000000,
                          .sr_independent = true,
                          .sr_min = 2100000, .sr_max = 6000000 },
    },
};

const int g_producer_count = sizeof(g_producers) / sizeof(g_producers[0]);

const producer_ops_t *producer_for_mode(int mode)
{
    for (int i = 0; i < g_producer_count; i++)
        if (g_producers[i].mode == mode && !g_producers[i].requires_fpga)
            return &g_producers[i];
    return NULL;
}

const producer_ops_t *producer_fpga_for_mode(int mode)
{
    for (int i = 0; i < g_producer_count; i++)
        if (g_producers[i].mode == mode && g_producers[i].requires_fpga)
            return &g_producers[i];
    return NULL;
}

bool producer_mode_active(const producer_ops_t *active, int mode)
{
    return active != NULL && active->mode == mode;
}

bool producer_is_running(const producer_ops_t *p)
{
    return p != NULL && p->running != NULL && p->running();
}

bool producer_is_fabric_running(const producer_ops_t *p)
{
    return p != NULL && p->requires_fpga && producer_is_running(p);
}

const mode_radio_req_t *producer_active_radio(const producer_ops_t *p)
{
    return producer_is_active(p) ? &p->radio : NULL;
}

bool producer_is_active(const producer_ops_t *p)
{
    return p != NULL && p->active != NULL && p->active();
}
