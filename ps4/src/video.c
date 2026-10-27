#include "video.h"
#include "video_proto.h"
#include "net_udp.h"
#include "jpeg_dec.h"
#include "h264_dec.h"
#include "feedback_proto.h"
#include "config.h"
#include "dlog.h"

#include <pthread.h>
#include <stdlib.h>
#include <string.h>

#include <orbis/libkernel.h>

/*
 * MJPEG-over-UDP receiver.
 *
 * A single background thread drains the socket, reassembles the chunks of the
 * current frame, and decodes it into the back buffer. The main thread only ever
 * reads the front buffer, so a decode never blocks rendering, and the render
 * blit never blocks the input loop.
 */

typedef struct {
    uint32_t frame_id;
    uint16_t chunk_cnt;
    uint8_t  flags;                 /* VIDEO_FLAG_* of the current frame */
    uint16_t clen[VIDEO_MAX_CHUNKS];
} frame_meta_t;

static udp_conn_t       g_sock;
static int              g_sock_ok;
static pthread_t        g_thread;
static volatile int     g_running;

static jpeg_decoder_t  *g_dec;
static h264_decoder_t  *g_h264;
static int              g_wait_keyframe;   /* H.264: drop until the next IDR */

/* Reassembly state (owned by the receive thread only). */
static uint8_t         *g_frame;          /* VIDEO_MAX_CHUNKS * CHUNK_PAYLOAD */
static uint8_t          g_seen[VIDEO_MAX_CHUNKS / 8];
static frame_meta_t     g_meta;
static int              g_have_frame;
static int              g_got;
static int              g_complete;
static int              g_decoded;

/* Double buffer of decoded RGB24 frames, shared with the main thread. */
static uint8_t         *g_rgb[2];
static int              g_front;
static int              g_fw, g_fh;
static int              g_rgb_cap;

static pthread_mutex_t  g_lock = PTHREAD_MUTEX_INITIALIZER;
static volatile int     g_new;      /* a fresh front buffer is ready */
static volatile uint32_t g_frames;
static volatile uint32_t g_drops;
static volatile uint32_t g_drop_stale;     /* chunk behind the current frame */
static volatile uint32_t g_drop_incomplete;/* frame abandoned before complete */
static volatile uint32_t g_drop_decode;    /* reassembled, but decode failed */
static volatile uint32_t g_drop_nokey;     /* H.264 frame skipped before an IDR */
static volatile uint32_t g_chunks;         /* unique chunks accepted */
static volatile uint32_t g_starts;         /* frames begun */
static uint32_t          g_stale_streak;   /* consecutive stale chunks seen */

/* --- Feedback channel (Step 2): STATS + KEYFRAME_REQUEST -> PC ---------- */
static udp_conn_t       g_fb;
static int              g_fb_ok;
static uint16_t         g_fb_seq;
static uint32_t         g_pkts_expected;   /* sum of chunk_cnt over started frames */
static uint64_t         g_last_stats_us;
static uint64_t         g_last_kf_us;
static uint32_t         g_jitter_us;       /* EWMA of |delta - prev_delta| */
static uint64_t         g_last_arrival_us;
static uint64_t         g_prev_delta_us;
static uint32_t         g_last_complete_id;/* frame_id of the newest shown frame */

/*
 * Inter-arrival jitter estimate. sceKernelGetProcessTime() is documented as
 * microseconds.
 * TODO(verify-on-PS4): confirm the unit on real hardware. If it is not
 * microseconds the reported jitter_ms is scaled wrong (cosmetic only).
 */
static void note_arrival(void)
{
    uint64_t now = sceKernelGetProcessTime();
    if (g_last_arrival_us != 0) {
        uint64_t delta = now - g_last_arrival_us;
        if (g_prev_delta_us != 0) {
            int64_t diff = (int64_t)delta - (int64_t)g_prev_delta_us;
            if (diff < 0)
                diff = -diff;
            /* EWMA with a 1/16 step: smooth, but reacts within ~1 s. */
            g_jitter_us = (g_jitter_us * 15 + (uint32_t)diff) / 16;
        }
        g_prev_delta_us = delta;
    }
    g_last_arrival_us = now;
}

/* Ask the PC encoder for an immediate IDR. Hard-limited to 2 requests/s so a
 * burst of losses cannot trigger a keyframe storm. */
static void request_keyframe(uint8_t reason)
{
    if (!g_fb_ok)
        return;

    uint64_t now = sceKernelGetProcessTime();
    if (g_last_kf_us != 0 && (now - g_last_kf_us) < 500000)
        return;
    g_last_kf_us = now;

    fb_kfreq_t k;
    memset(&k, 0, sizeof(k));
    k.hdr.magic = FB_MAGIC;
    k.hdr.version = FB_VERSION;
    k.hdr.type = FB_TYPE_KEYFRAME_REQUEST;
    k.hdr.seq = g_fb_seq++;
    k.last_frame_id = g_meta.frame_id;
    k.reason = reason;
    udp_send(&g_fb, &k, (uint16_t)sizeof(k));
}

static void send_stats(void)
{
    if (!g_fb_ok)
        return;

    fb_stats_t s;
    memset(&s, 0, sizeof(s));
    s.hdr.magic = FB_MAGIC;
    s.hdr.version = FB_VERSION;
    s.hdr.type = FB_TYPE_STATS;
    s.hdr.seq = g_fb_seq++;
    s.packets_expected = g_pkts_expected;
    s.packets_received = g_chunks;
    s.frames_completed = g_frames;
    s.frames_dropped = g_drops;
    uint32_t jit = g_jitter_us / 1000; /* µs -> ms */
    s.jitter_ms = (uint16_t)(jit > 0xFFFF ? 0xFFFF : jit);
    s.last_frame_id = g_last_complete_id;
    s.decoder_queue = 0; /* decoding is synchronous on the receive thread */
    udp_send(&g_fb, &s, (uint16_t)sizeof(s));
}

static void start_frame(uint32_t fid, uint16_t cnt, uint8_t flags)
{
    g_meta.frame_id = fid;
    g_meta.chunk_cnt = cnt;
    g_meta.flags = flags;
    memset(g_meta.clen, 0, sizeof(g_meta.clen));
    memset(g_seen, 0, sizeof(g_seen));
    g_have_frame = 1;
    g_got = 0;
    g_complete = 0;
    g_decoded = 0;
    g_stale_streak = 0;
    g_starts++;
    g_pkts_expected += cnt;
}

static int chunk_seen(uint16_t idx)
{
    return (g_seen[idx >> 3] >> (idx & 7)) & 1;
}

static void mark_chunk(uint16_t idx)
{
    g_seen[idx >> 3] |= (uint8_t)(1u << (idx & 7));
}

static void handle_datagram(const uint8_t *d, int n)
{
    if (n < VIDEO_HDR_LEN)
        return;

    video_hdr_t h;
    memcpy(&h, d, sizeof(h));

    if (h.magic != VIDEO_MAGIC || h.version != VIDEO_VERSION)
        return;

    uint16_t cidx = h.chunk_idx;
    uint16_t ccnt = h.chunk_cnt;
    uint16_t plen = h.payload_len;

    if (plen == 0 || (int)plen + VIDEO_HDR_LEN > n ||
        ccnt == 0 || ccnt > VIDEO_MAX_CHUNKS || cidx >= ccnt)
        return;

    if (!g_have_frame || h.frame_id != g_meta.frame_id) {
        /* Frames are 32-bit and increment by one; a positive distance means a
         * newer frame, so we (re)start. Anything else is a stale straggler. */
        int32_t delta = (int32_t)(h.frame_id - (g_have_frame ? g_meta.frame_id : 0));
        if (!g_have_frame || delta > 0) {
            if (g_have_frame && !g_complete) {
                g_drops++;
                g_drop_incomplete++; /* never finished the previous frame */
                /* A gap breaks H.264 reference chains: wait for the next IDR
                 * and ask the encoder for one right away. */
                if (g_meta.flags & VIDEO_FLAG_H264) {
                    g_wait_keyframe = 1;
                    request_keyframe(FB_KF_REASON_INCOMPLETE);
                }
            }
            start_frame(h.frame_id, ccnt, h.flags);
        } else {
            g_drops++;
            g_drop_stale++; /* chunk for an older frame we already moved past */
            /*
             * A long run of stale chunks means the sender restarted and is
             * numbering from a lower frame_id again; we would otherwise sit
             * here forever rejecting every datagram. Abandon the frame so the
             * next chunk starts fresh.
             */
            if (++g_stale_streak >= 1024) {
                g_have_frame = 0;
                g_got = 0;
                g_complete = 0;
                g_decoded = 0;
                g_wait_keyframe = 1;
                g_stale_streak = 0;
                request_keyframe(FB_KF_REASON_INCOMPLETE);
            }
            return;
        }
    }

    if (g_complete)
        return; /* trailing duplicate of a frame we already decoded */

    if (chunk_seen(cidx))
        return;

    memcpy(g_frame + (size_t)cidx * VIDEO_CHUNK_PAYLOAD,
           d + VIDEO_HDR_LEN, plen);
    g_meta.clen[cidx] = plen;
    mark_chunk(cidx);
    g_got++;
    g_chunks++;

    if (g_got == g_meta.chunk_cnt)
        g_complete = 1;
}

static void try_decode(void)
{
    uint16_t cnt = g_meta.chunk_cnt;
    uint16_t last = g_meta.clen[cnt - 1];
    size_t total;
    int is_h264 = (g_meta.flags & VIDEO_FLAG_H264) != 0;

    g_decoded = 1; /* one attempt per frame: never retry (or re-count) it */

    if (last == 0) {
        g_drops++;
        g_drop_decode++;
        if (is_h264) {
            g_wait_keyframe = 1;
            request_keyframe(FB_KF_REASON_DECODE_ERROR);
        }
        return;
    }
    total = (size_t)(cnt - 1) * VIDEO_CHUNK_PAYLOAD + last;
    if (total == 0 || total > (size_t)VIDEO_MAX_CHUNKS * VIDEO_CHUNK_PAYLOAD) {
        g_drops++;
        g_drop_decode++;
        if (is_h264) {
            g_wait_keyframe = 1;
            request_keyframe(FB_KF_REASON_DECODE_ERROR);
        }
        return;
    }

    int keyframe = (g_meta.flags & VIDEO_FLAG_KEYFRAME) != 0;
    int w = 0, h = 0;
    uint8_t *back = g_rgb[g_front ^ 1];

    if (is_h264) {
        /* After a gap the reference chain is broken: ignore P-frames until the
         * next IDR, then resume cleanly. */
        if (g_wait_keyframe && !keyframe) {
            g_drop_nokey++;
            return;
        }
        /* The PC's ABR (Step 4) may lower fps or resolution mid-session and
         * later restore it. Frames are presented at g_fw x g_fh from the
         * decoder, and g_rgb_cap covers the largest configured geometry, so a
         * smaller frame always fits.
         * TODO(verify-on-PS4): confirm FFmpeg's H.264 decoder re-initialises
         * cleanly on an in-band SPS resolution change (it rebuilds the swscale
         * context below, but this has not been exercised on hardware). */
        int rc = h264_decoder_decode(g_h264, g_frame, total, back, g_rgb_cap, &w, &h);
        if (rc < 0) {
            g_wait_keyframe = 1;
            g_drops++;
            g_drop_decode++;
            request_keyframe(FB_KF_REASON_DECODE_ERROR);
            return;
        }
        if (rc == 1)
            return; /* consumed, no output frame yet */
        g_wait_keyframe = 0;
    } else {
        if (jpeg_decoder_decode(g_dec, g_frame, total, back, g_rgb_cap, &w, &h) != 0) {
            g_drops++;
            g_drop_decode++;
            return;
        }
    }

    pthread_mutex_lock(&g_lock);
    g_front ^= 1;
    g_fw = w;
    g_fh = h;
    g_new = 1;
    g_frames++;
    g_last_complete_id = g_meta.frame_id;
    pthread_mutex_unlock(&g_lock);
}

static void *recv_thread(void *arg)
{
    (void)arg;
    uint8_t dgram[VIDEO_MAX_DGRAM + 64];

    while (g_running) {
        int idle = 1;

        for (int i = 0; i < 128; i++) {
            int n = udp_recv(&g_sock, dgram, sizeof(dgram));
            if (n <= 0)
                break;
            idle = 0;
            note_arrival();
            handle_datagram(dgram, n);
            if (g_complete)
                break;
        }

        if (g_complete && !g_decoded) {
            try_decode();
        }

        /* 1 Hz STATS to the PC (Step 2). */
        uint64_t now_us = sceKernelGetProcessTime();
        if (g_last_stats_us == 0)
            g_last_stats_us = now_us;
        if (now_us - g_last_stats_us >= 1000000) {
            send_stats();
            g_last_stats_us = now_us;
        }

        if (idle)
            sceKernelUsleep(1000); /* 1 ms; keep CPU use low while idle */
    }
    return NULL;
}

int video_start(uint16_t port, int max_w, int max_h)
{
    g_rgb_cap = max_w * max_h * 3;

    g_frame = (uint8_t *)malloc((size_t)VIDEO_MAX_CHUNKS * VIDEO_CHUNK_PAYLOAD);
    g_rgb[0] = (uint8_t *)calloc(1, g_rgb_cap);
    g_rgb[1] = (uint8_t *)calloc(1, g_rgb_cap);
    g_dec = jpeg_decoder_create();
    g_h264 = h264_decoder_create();
    g_wait_keyframe = 1; /* an H.264 stream always begins on an IDR */

    if (!g_frame || !g_rgb[0] || !g_rgb[1] || !g_dec) {
        dlog("vstart: ALLOC FAIL frame=%p rgb0=%p rgb1=%p dec=%p (cap=%d)",
             (void *)g_frame, (void *)g_rgb[0], (void *)g_rgb[1],
             (void *)g_dec, g_rgb_cap);
        return -1;
    }
    dlog("vstart: jpeg=%p h264=%p cap=%d", (void *)g_dec, (void *)g_h264, g_rgb_cap);

    int rc = udp_open_listen(&g_sock, port);
    if (rc != 0) {
        dlog("vstart: listen(%u) failed rc=%d", (unsigned)port, rc);
        return -2;
    }
    g_sock_ok = 1;

    /* Feedback channel (Step 2). A failure here is non-fatal: video still
     * flows, we only lose the PC's adaptation signal. */
    g_fb_seq = 0;
    g_pkts_expected = 0;
    g_last_stats_us = 0;
    g_last_kf_us = 0;
    g_jitter_us = 0;
    g_last_arrival_us = 0;
    g_prev_delta_us = 0;
    g_last_complete_id = 0;
    int frc = udp_open(&g_fb, CFG_PC_IP, CFG_FEEDBACK_PORT, 0);
    if (frc == 0) {
        g_fb_ok = 1;
        dlog("vstart: feedback -> %s:%u", CFG_PC_IP,
             (unsigned)CFG_FEEDBACK_PORT);
    } else {
        dlog("vstart: feedback open failed rc=%d", frc);
    }

    g_front = 0;
    g_fw = g_fh = 0;
    g_have_frame = 0;
    g_complete = 0;

    g_running = 1;
    if (pthread_create(&g_thread, NULL, recv_thread, NULL) != 0) {
        g_running = 0;
        return -3;
    }

    /* The receiver always needs an IDR before it can show anything. */
    request_keyframe(FB_KF_REASON_STARTUP);
    return 0;
}

void video_stop(void)
{
    if (g_running) {
        g_running = 0;
        pthread_join(g_thread, NULL);
    }
    if (g_sock_ok) {
        udp_close(&g_sock);
        g_sock_ok = 0;
    }
    if (g_fb_ok) {
        udp_close(&g_fb);
        g_fb_ok = 0;
    }
    if (g_dec) {
        jpeg_decoder_destroy(g_dec);
        g_dec = NULL;
    }
    if (g_h264) {
        h264_decoder_destroy(g_h264);
        g_h264 = NULL;
    }
}

int video_poll(void)
{
    int v;
    pthread_mutex_lock(&g_lock);
    v = g_new;
    g_new = 0;
    pthread_mutex_unlock(&g_lock);
    return v;
}

int video_lock_frame(const uint8_t **rgb, int *w, int *h)
{
    pthread_mutex_lock(&g_lock);
    if (g_fw <= 0 || g_fh <= 0) {
        pthread_mutex_unlock(&g_lock);
        return 0;
    }
    *rgb = g_rgb[g_front];
    *w = g_fw;
    *h = g_fh;
    return 1;
}

void video_unlock_frame(void)
{
    pthread_mutex_unlock(&g_lock);
}

uint32_t video_frames(void) { return g_frames; }
uint32_t video_drops(void)  { return g_drops; }
uint32_t video_drop_stale(void)      { return g_drop_stale; }
uint32_t video_drop_incomplete(void) { return g_drop_incomplete; }
uint32_t video_drop_decode(void)     { return g_drop_decode; }
uint32_t video_drop_nokey(void)      { return g_drop_nokey; }
uint32_t video_chunks(void)          { return g_chunks; }
uint32_t video_starts(void)          { return g_starts; }
