/* firmware/src/wifi_mode.h — software WiFi producer */
#pragma once

#include "producer.h"

#define WIFI_POP_MAX 64

int  wifi_mode_enter(void);
void wifi_mode_exit(void);
bool wifi_mode_running(void);
bool wifi_mode_active(void);

void drain_wifi(const drain_ctx_t *ctx);
