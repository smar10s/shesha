/* firmware/src/fm_mode.h — FM RX producer */
#pragma once

#include "producer.h"

#define FM_POP_MAX 64

int  fm_mode_enter(void);
void fm_mode_exit(void);
bool fm_mode_running(void);
bool fm_mode_active(void);

void drain_fm(const drain_ctx_t *ctx);
