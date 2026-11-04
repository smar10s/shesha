/* fm_protocol.h — FM and RDS wire formats.
 *
 * Kept apart from protocol.h so protocol-wide code does not drag in the
 * scanner and RDS types.  The wire bytes are unchanged. */
#pragma once

#include "protocol.h"
#include "rds_state.h"
#include "rds_tx.h"

/*
 * DATA_FM payload layout: the existing 16-byte header (timestamp, audio
 * rate, sample count), then the fixed RDS state block, then the audio. The
 * RDS block comes before the variable-length audio so its offsets are
 * constants on both ends. PROTO_RDS_WIRE_SIZE is pinned by
 * firmware/tests/test_protocol_fm.c and client/tests/test_protocol_fm.py.
 */
#define PROTO_FM_HEADER_SIZE 16
#define PROTO_RDS_WIRE_SIZE  190

/*
 * RDS transmit config block, carried in the FM start payload before the
 * audio and alone in CMD_SET_RDS.  Pinned here and mirrored in
 * client/shesha/protocol.py (_RDS_TX_STRUCT).
 *
 *   off  type    field
 *   0    u8      flags: bit0 enabled, bit1 tp
 *   1    u16     pi
 *   3    u8      pty (0..31)
 *   4    u8[8]   ps
 *   12   u8[64]  rt
 */
#define PROTO_RDS_TX_SIZE 76

void proto_pack_rds_tx(uint8_t *p, const rds_tx_config_t *cfg);
void proto_unpack_rds_tx(const uint8_t *p, rds_tx_config_t *cfg);

size_t proto_build_fm(uint8_t *buf, uint32_t seq, double timestamp,
                      uint32_t audio_rate_hz,
                      uint32_t n_samples, const float *audio,
                      const rds_state_t *rds);
