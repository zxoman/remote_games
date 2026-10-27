#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Minimal libjpeg-turbo backend for the Phase 2 MJPEG path.
 *
 * The decoder is stateless across frames (each JPEG is self-contained); the
 * opaque handle only exists so a future libSceJpegDec / H.264 backend can hold
 * persistent state behind the same interface.
 */

typedef struct jpeg_decoder jpeg_decoder_t;

jpeg_decoder_t *jpeg_decoder_create(void);
void jpeg_decoder_destroy(jpeg_decoder_t *d);

/* Decode `len` bytes of JPEG at `jpg` into `out` as tightly packed RGB24
 * (outW*outH*3, row-major). `out_cap` is the capacity of `out` in bytes. On
 * success writes outW and outH and returns 0; returns negative on failure (bad
 * stream, or the decoded image does not fit in `out`). */
int jpeg_decoder_decode(jpeg_decoder_t *d, const uint8_t *jpg, size_t len,
                        uint8_t *out, size_t out_cap, int *outW, int *outH);

#ifdef __cplusplus
}
#endif
