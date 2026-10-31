#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define INPUT_MAGIC   0x49345052u /* "RP4I" little-endian */
#define INPUT_VERSION 1
#define INPUT_PKT_LEN 18

/*
 * Multi-controller extension (Phase 1).
 *
 * Player 0 is always sent as the original 18-byte version 1 packet, so a PC
 * receiver that predates multi-pad support keeps working with this build.
 * Players 1..3 use version 2: the same 18 bytes followed by a player byte
 * (offset 18). Old receivers reject the packet on length, so they silently
 * ignore the extra pads instead of misreading them.
 *
 * Keep in sync with pc/protocol.py and docs/protocol.md §1.
 */
#define INPUT_VERSION_MULTI 2
#define INPUT_PKT_MULTI_LEN (INPUT_PKT_LEN + 1) /* 19 */
#define INPUT_MAX_PLAYERS   4                   /* PS4 max logged-in users */

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

/* Version 2 packet: the v1 body plus the player index, appended so the first
 * 18 bytes stay byte-identical to a v1 packet (except the version byte). */
typedef struct __attribute__((packed)) {
    input_pkt_t base;
    uint8_t     player;
} input_multi_pkt_t;

_Static_assert(sizeof(input_multi_pkt_t) == INPUT_PKT_MULTI_LEN,
               "multi-player packet must be 19 bytes");

/* One pad sample: the packet body plus which player it belongs to. */
typedef struct {
    input_pkt_t pkt;    /* always version INPUT_VERSION until encoded */
    uint8_t     player; /* 0..INPUT_MAX_PLAYERS-1 */
} input_sample_t;

/* Initialize scePad + user service and open every available pad slot.
 * Returns 0 if at least one pad opened, negative otherwise. Missing pads are
 * retried periodically from input_poll(), so hot-plug works. */
int input_init(void);

/* Read every open pad into `out` (one sample per connected pad, ascending
 * player order) and return how many were filled. Caller sets `seq`.
 * Also retries closed slots, so a pad connected after boot appears here. */
int input_poll(input_sample_t *out, int max);

/* Serialize one sample: 18 bytes for player 0, 19 bytes for players 1..3.
 * Returns the byte count, or -1 if `cap` is too small. */
int input_encode(const input_sample_t *s, uint8_t *buf, int cap);

/* Number of pad handles currently open (a pad may be open but disconnected). */
int input_connected(void);

/* Whether the given player slot is open (for the on-screen status pips). */
int input_player_open(int player);

void input_close(void);

#ifdef __cplusplus
}
#endif
