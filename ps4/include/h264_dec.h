#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Phase 3 H.264 backend (FFmpeg software decoder).
 *
 * Stateful across frames: unlike JPEG, H.264 access units reference earlier
 * frames, so the decoder keeps its own reference picture list. The caller must
 * feed the access units of a single stream in order and re-sync on an IDR after
 * any gap (see video.c).
 *
 * Output is tightly packed RGB24 (outW*outH*3) so the rest of the pipeline is
 * byte-identical to the MJPEG path and can share the same frame buffers.
 */

typedef struct h264_decoder h264_decoder_t;

h264_decoder_t *h264_decoder_create(void);
void h264_decoder_destroy(h264_decoder_t *d);

/* Decode one Annex-B access unit.
 *
 *   Returns  0  a frame was decoded into `out` (outW/outH written)
 *            1  the access unit was consumed but produced no frame yet
 *           <0  decode error / lost state; caller should wait for a keyframe
 *
 * `out_cap` is the capacity of `out` in bytes. */
int h264_decoder_decode(h264_decoder_t *d, const uint8_t *au, size_t len,
                        uint8_t *out, size_t out_cap, int *outW, int *outH);

#ifdef __cplusplus
}
#endif
