#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define INPUT_MAGIC   0x49345052u /* "RP4I" little-endian */
#define INPUT_VERSION 1
#define INPUT_PKT_LEN 18

/* Button bitmask — keep in sync with docs/protocol.md */
#define BTN_SQUARE     (1u << 0)
#define BTN_CROSS      (1u << 1)
#define BTN_CIRCLE     (1u << 2)
#define BTN_TRIANGLE   (1u << 3)
#define BTN_L1         (1u << 4)
#define BTN_R1         (1u << 5)
#define BTN_L2         (1u << 6)
#define BTN_R2         (1u << 7)
#define BTN_SHARE      (1u << 8)
#define BTN_OPTIONS    (1u << 9)
#define BTN_L3         (1u << 10)
#define BTN_R3         (1u << 11)
#define BTN_UP         (1u << 12)
#define BTN_DOWN       (1u << 13)
#define BTN_LEFT       (1u << 14)
#define BTN_RIGHT      (1u << 15)

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t  version;
    uint8_t  seq;
    uint16_t buttons;
    uint8_t  l2;
    uint8_t  r2;
    int16_t  lx, ly;
    int16_t  rx, ry;
} input_pkt_t;

_Static_assert(sizeof(input_pkt_t) == INPUT_PKT_LEN, "input packet must be 18 bytes");

/* Initialize scePad + user service and open a pad handle for the initial
 * user. Returns 0 on success. */
int input_init(void);

/* Poll the open pad and fill `pkt` (caller sets seq). Returns 0 on success,
 * negative if no pad is open/available. */
int input_read(input_pkt_t *pkt);

void input_close(void);

#ifdef __cplusplus
}
#endif
