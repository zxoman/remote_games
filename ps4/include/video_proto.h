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
