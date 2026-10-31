#pragma once

#include <stdint.h>

/*
 * Video-over-UDP wire format (Phase 2 MJPEG, Phase 3 H.264). Mirrors
 * docs/protocol.md §3 and pc/protocol.py. All fields little-endian.
 *
 * One encoded frame (a JPEG image, or one H.264 access unit) is split into
 * `chunk_cnt` datagrams. Every chunk except the last carries exactly
 * VIDEO_CHUNK_PAYLOAD bytes so the receiver can place chunk N at offset
 * N * VIDEO_CHUNK_PAYLOAD; the last chunk carries the tail.
 */

#define VIDEO_MAGIC        0x56345052u /* "RP4V" little-endian */
#define VIDEO_VERSION      1
#define VIDEO_FLAG_KEYFRAME 0x01u /* H.264: access unit contains an IDR */
#define VIDEO_FLAG_H264     0x02u /* payload is H.264, not JPEG */
#define VIDEO_FLAG_FEC_PARITY 0x04u /* Step 6: XOR parity, not a data chunk */

#define VIDEO_MAX_DGRAM    1200
#define VIDEO_HDR_LEN      (int)sizeof(video_hdr_t) /* 16 */
#define VIDEO_CHUNK_PAYLOAD (VIDEO_MAX_DGRAM - VIDEO_HDR_LEN) /* 1184 */

/* 2048 chunks * 1184 B ~= 2.4 MB max frame — ample for 1080p MJPEG. */
#define VIDEO_MAX_CHUNKS   2048

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t  version;
    uint8_t  flags;
    uint16_t chunk_idx;
    uint16_t chunk_cnt;
    uint32_t frame_id;
    uint16_t payload_len;
} video_hdr_t;

_Static_assert(sizeof(video_hdr_t) == 16, "video header must be 16 bytes");

/*
 * Step 6: XOR FEC parity datagrams.
 *
 * One parity datagram per group of `group_len` data chunks lets the receiver
 * rebuild a *single* lost datagram of that group:
 *
 *     P  = D0 ^ D1 ^ ... ^ Dn            (element-wise, zero padded to 1184 B)
 *     Di = P ^ (all present Dj, j != i)
 *
 * A parity datagram reuses video_hdr_t, so it costs no extra header bytes:
 *
 *     flags       |= VIDEO_FLAG_FEC_PARITY
 *     chunk_idx    = (group_start & 0x7FF) | ((group_len - 1) << 11)
 *     chunk_cnt    = VIDEO_FEC_CCNT (+ the final chunk's length, see below)
 *     payload_len  = parity bytes carried (1..VIDEO_CHUNK_PAYLOAD)
 *
 * chunk_cnt > VIDEO_MAX_CHUNKS is what keeps this backward compatible: a
 * receiver built before Step 6 already rejects such a datagram (both
 * `ccnt > VIDEO_MAX_CHUNKS` and `cidx >= ccnt` fail), so old PKGs silently
 * ignore parity and keep working.
 *
 * `chunk_cnt - VIDEO_FEC_CCNT` is the true length of the frame's last chunk
 * when this group contains it, else 0. That is the only length the receiver
 * cannot otherwise know when the final chunk is the lost one.
 */

#define VIDEO_FEC_START_BITS   11   /* chunk index: 0..2047 */
#define VIDEO_FEC_LEN_BITS     4    /* group length: 1..16 */
#define VIDEO_FEC_GROUP_MAX    16
#define VIDEO_FEC_CCNT         (VIDEO_MAX_CHUNKS + 1)  /* 2049, never a chunk */
#define VIDEO_FEC_CCNT_MAX     (VIDEO_FEC_CCNT + VIDEO_CHUNK_PAYLOAD)

static inline uint16_t video_fec_group_field(uint16_t start, uint8_t len)
{
    return (uint16_t)((start & ((1u << VIDEO_FEC_START_BITS) - 1u))
                      | ((uint16_t)(len - 1u) << VIDEO_FEC_START_BITS));
}

/* Returns 0 and fills *start and *len, or -1 when malformed. */
static inline int video_fec_group_decode(uint16_t field, uint16_t *start,
                                         uint8_t *len)
{
    uint16_t s = (uint16_t)(field & ((1u << VIDEO_FEC_START_BITS) - 1u));
    uint8_t  l = (uint8_t)(((field >> VIDEO_FEC_START_BITS)
                            & ((1u << VIDEO_FEC_LEN_BITS) - 1u)) + 1u);
    if ((uint32_t)s + l > VIDEO_MAX_CHUNKS)
        return -1;
    *start = s;
    *len = l;
    return 0;
}
