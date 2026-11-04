/* wifi_fpga.h — fabric WiFi producer lifecycle and software diagnostics. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Nearest Wi-Fi channel centre for an RF frequency in Hz. */
int  wifi_fpga_freq_to_channel(uint64_t hz);

int  wifi_fpga_enter(void);
void wifi_fpga_exit(void);
bool wifi_fpga_running(void);
bool wifi_fpga_active(void);
void wifi_fpga_get_diag(uint32_t *frames, uint32_t *fcs_ok);
