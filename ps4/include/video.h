#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Phase 2/3 video receiver.
 *
 * video_start() opens the UDP listen socket, allocates the reassembly and
 * double buffers, and spawns a background thread that reassembles frames and
 * decodes them (MJPEG via libjpeg-turbo, H.264 via FFmpeg). Running the decode
 * off the main thread keeps the 250 Hz input send loop from stalling on a
 * ~20-40 ms decode.
 */

/* Returns 0 on success. `max_w`/`max_h` bound the decoded frame size. */
int  video_start(uint16_t port, int max_w, int max_h);
void video_stop(void);

/* 1 if a new frame has been decoded since the previous call (clears the flag). */
int  video_poll(void);

/* Lock the current frame and hand back its RGB24 pixels. Returns 1 and locks
 * the frame if one is available; 0 if no frame has been decoded yet. Must be
 * paired with video_unlock_frame(). The pointer is only valid until unlock. */
int  video_lock_frame(const uint8_t **rgb, int *w, int *h);
void video_unlock_frame(void);

uint32_t video_frames(void); /* frames decoded and shown */
uint32_t video_drops(void);  /* frames abandoned / failed to decode */
/* Breakdown of video_drops(), for diagnosing where datagrams go missing. */
uint32_t video_drop_stale(void);      /* chunk arrived behind current frame */
uint32_t video_drop_incomplete(void); /* frame abandoned before completing */
uint32_t video_drop_decode(void);     /* reassembled, but decode failed */
uint32_t video_drop_nokey(void);      /* H.264 frame skipped before an IDR */
uint32_t video_chunks(void);          /* unique chunks accepted */
uint32_t video_starts(void);          /* frames begun */

#ifdef __cplusplus
}
#endif
