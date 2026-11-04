/* tx_flags.h — flags shared by the wire protocol and the TX scheduler.
 *
 * Kept in one header so the command payload parser and the scheduler cannot
 * silently disagree about what a bit means. */
#pragma once

#define TX_FLAG_CYCLIC     0x01
#define TX_FLAG_STREAM     0x02
#define TX_REPEAT_FOREVER  0xFFFF
