#include <cstdio>
#include <cstring>
#include <sstream>

#include <orbis/libkernel.h>

#include "graphics.h"   /* Scene2D (local copy, adds BlitRGB) */
#include "config.h"
#include "dlog.h"
#include "input.h"
#include "net_udp.h"
#include "video.h"

/*
 * Reverse Remote Play — PS4 side.
 *
 *   Phase 1: read the DualShock 4 and stream an 18-byte packet to the PC at
 *            CFG_INPUT_RATE_HZ.
 *   Phase 2: receive MJPEG video from the PC and display it full-screen.
 *
 * Timing philosophy: sending input is the priority. The video receive/decode
 * runs on its own thread (see video.c) and the main loop never waits on it.
 * Flips are submitted WITHOUT waiting on vsync, so neither a slow flip nor a
 * slow decode stalls the input loop.
 *
 * On-screen status:
 *   no video yet  -> dark green (pad open) / dark red (no pad) + white marker
 *                    that follows the left stick
 *   video shown   -> a small green/red square in the top-left corner
 */

/* log.h (used by our graphics.cpp) expects this global. */
std::stringstream debugLogStream;

static uint64_t now_us(void) { return (uint64_t)sceKernelGetProcessTime(); }

int main(void)
{
    /* Unbuffered stdout so logs appear immediately in the PS4 log socket. */
    setvbuf(stdout, NULL, _IONBF, 0);

    dlog_init(CFG_PC_IP, CFG_LOG_PORT);
    dlog("boot: remote play rev; pc=%s in=%u vid=%u log=%u built=%s %s",
         CFG_PC_IP, CFG_INPUT_PORT, CFG_VIDEO_PORT, CFG_LOG_PORT,
         __DATE__, __TIME__);

    Scene2D *scene = new Scene2D(1280, 720, 4);
    bool have_video = scene->Init(0xC000000, 2);
    if (!have_video)
        dlog("warning: Scene2D init failed, running headless");

    if (have_video) {
        int vrc = video_start(CFG_VIDEO_PORT, CFG_VIDEO_WIDTH, CFG_VIDEO_HEIGHT);
        dlog("video_start: %s (rc=%d, port %u)",
             vrc == 0 ? "OK" : "FAILED", vrc, CFG_VIDEO_PORT);
    }

    udp_conn_t input_conn;
    int udp_ok = (udp_open(&input_conn, CFG_PC_IP, CFG_INPUT_PORT, 0) == 0);
    if (!udp_ok)
        dlog("error: udp_open failed (check CFG_PC_IP)");

    int pad_ok = (input_init() == 0);
    dlog("input_init: %s; target %s:%u",
         pad_ok ? "OK" : "FAILED", CFG_PC_IP, CFG_INPUT_PORT);

    const uint64_t period_us = 1000000ull / CFG_INPUT_RATE_HZ;

    uint8_t seq = 0;
    uint64_t next = now_us();
    uint64_t sent = 0, errors = 0;
    int frame_id = 0;
    int last_lx = 0, last_ly = 0;

    int last_pad_ok = -1;
    int ever_rendered = 0;
    int logged_first_frame = 0;
    uint32_t seen_frames = 0;
    uint64_t last_render = 0;

    for (;;) {
        uint64_t t = now_us();
        if (t < next) {
            sceKernelUsleep((uint32_t)(next - t));
            continue;
        }
        next += period_us;
        if (now_us() > next + 1000000ull)
            next = now_us(); /* fell far behind; resync */

        /* --- input: read pad + send (highest priority) --- */
        input_pkt_t pkt;
        pad_ok = (input_read(&pkt) == 0);
        last_lx = pkt.lx;
        last_ly = pkt.ly;
        pkt.seq = seq++;

        if (udp_ok) {
            if (udp_send(&input_conn, &pkt, INPUT_PKT_LEN) < 0)
                errors++;
            else
                sent++;
        }

        /* --- render: on a new video frame, a pad status change, or the very
         *     first frame; capped to ~33 fps so we never flood the flip queue.
         *     Output timestamp is the last recorded value on purpose. --- */
        int new_video = (video_frames() != seen_frames);
        int status_changed = (pad_ok != last_pad_ok);

        if (have_video && (new_video || status_changed || !ever_rendered) &&
            (t - last_render) >= 30000ull) {

            const uint8_t *rgb;
            int vw, vh;
            if (video_lock_frame(&rgb, &vw, &vh)) {
                if (!logged_first_frame) {
                    dlog("first video frame %dx%d", vw, vh);
                    logged_first_frame = 1;
                }
                scene->BlitRGB(rgb, vw, vh);
                video_unlock_frame();
                seen_frames = video_frames();
            } else {
                Color bg = pad_ok ? Color{10, 40, 15} : Color{50, 10, 10};
                scene->FrameBufferFill(bg);
                int cx = 640 + (last_lx * 560 / 32768);
                int cy = 360 + (last_ly * 320 / 32768);
                scene->DrawRectangle(cx - 16, cy - 16, 32, 32, Color{230, 230, 230});
            }

            /* Pad status pip, always visible. */
            Color pip = pad_ok ? Color{0, 220, 0} : Color{220, 0, 0};
            scene->DrawRectangle(8, 8, 24, 24, pip);

            scene->SubmitFlip(frame_id);
            scene->FrameBufferSwap();
            frame_id++;
            last_render = t;

            if (status_changed && ever_rendered)
                dlog("pad %s", pad_ok ? "connected" : "disconnected");

            last_pad_ok = pad_ok;
            ever_rendered = 1;
        }

        /* ~1 Hz: one stats line, plus a line whenever frames start dropping. */
        if ((seq & 0xFF) == 0) {
            dlog("stats sent=%llu err=%llu pad=%s frames=%u drops=%u "
                 "stale=%u inc=%u dec=%u nokey=%u ch=%u starts=%u",
                 (unsigned long long)sent, (unsigned long long)errors,
                 pad_ok ? "OK" : "MISSING",
                 (unsigned)video_frames(), (unsigned)video_drops(),
                 (unsigned)video_drop_stale(), (unsigned)video_drop_incomplete(),
                 (unsigned)video_drop_decode(), (unsigned)video_drop_nokey(),
                 (unsigned)video_chunks(), (unsigned)video_starts());
        }
    }

    video_stop();
    input_close();
    udp_close(&input_conn);
    dlog_close();
    return 0;
}
