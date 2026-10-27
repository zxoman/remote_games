#pragma once

#include <stdint.h>

/*
 * PS4 -> PC feedback channel (Step 2): STATS + KEYFRAME_REQUEST.
 *
 * Mirrors docs/protocol.md §4 and pc/protocol.py. All fields little-endian.
 * Every packet starts with the same 8-byte header so the PC can validate
 * magic/version/type and ignore anything it does not understand.
 *
 * NOTE: this runs on the PS4's network stack only — no OpenOrbis API beyond
 * the normal BSD sockets already used by net_udp.c is involved.
 */

/* "RP4F" little-endian. */
#define FB_MAGIC            0x46345052u
#define FB_VERSION          1
#define FB_TYPE_STATS       1
#define FB_TYPE_KEYFRAME_REQUEST 2

#define FB_HDR_LEN          8
#define FB_STATS_LEN        32
#define FB_KFREQ_LEN        14

/* KEYFRAME_REQUEST reason codes (keep in sync with pc/protocol.py). */
#define FB_KF_REASON_INCOMPLETE   0
#define FB_KF_REASON_DECODE_ERROR 1
#define FB_KF_REASON_STARTUP      2

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t  version;
    uint8_t  type;
    uint16_t seq;
} fb_hdr_t;

typedef struct __attribute__((packed)) {
    fb_hdr_t hdr;
    uint32_t packets_expected;   /* sum of chunk_cnt over frames begun */
    uint32_t packets_received;   /* unique chunks accepted */
    uint32_t frames_completed;   /* frames decoded and presented */
    uint32_t frames_dropped;     /* incomplete / stale / decode failures */
    uint16_t jitter_ms;          /* rough inter-arrival jitter estimate */
    uint32_t last_frame_id;      /* last frame_id we completed */
    uint16_t decoder_queue;      /* frames waiting to decode (0: synchronous) */
} fb_stats_t;

typedef struct __attribute__((packed)) {
    fb_hdr_t hdr;
    uint32_t last_frame_id;      /* last frame_id known when the loss happened */
    uint8_t  reason;             /* FB_KF_REASON_* */
    uint8_t  reserved;
} fb_kfreq_t;

#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
_Static_assert(sizeof(fb_hdr_t) == FB_HDR_LEN, "fb_hdr_t size");
_Static_assert(sizeof(fb_stats_t) == FB_STATS_LEN, "fb_stats_t size");
_Static_assert(sizeof(fb_kfreq_t) == FB_KFREQ_LEN, "fb_kfreq_t size");
#endif
