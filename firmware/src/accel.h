/*
 * accel.h — Accelerator bitstream identity constants
 *
 * Shesha probes REG_BUILD_ID_PROJECT at startup to determine which
 * accelerator (if any) is present in the FPGA fabric.  Each child
 * bitstream of styx sets PROJECT_ID to a unique 4-byte ASCII tag.
 *
 * The base styx bitstream reports PROJECT_ID = 0x53545958 ("STYX").
 * Accelerator bitstreams override this with their own identity.
 */
#pragma once

#include <stdint.h>

/* Known PROJECT_ID values (4-byte ASCII, big-endian) */
#define PROJECT_ID_STYX  0x53545958  /* "STYX" — base infrastructure only */
#define PROJECT_ID_WIFI  0x57494649  /* "WIFI" — deimos 802.11a/g fabric decode */
/* #define PROJECT_ID_LORA  0x4C4F5241  "LORA" — future LoRa accelerator */
