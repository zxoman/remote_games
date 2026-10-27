#include "h264_dec.h"

#include <stdlib.h>
#include <string.h>

#include <libavcodec/avcodec.h>
#include <libavutil/error.h>
#include <libavutil/imgutils.h>
#include <libavutil/log.h>
#include <libswscale/swscale.h>

/*
 * Minimal FFmpeg H.264 decoder wrapper (see h264_dec.h).
 *
 * The build is deliberately tiny (see install/build-ffmpeg-ps4.sh): H.264
 * decoder + parser + libswscale, plain C, no threads. libswscale converts the
 * decoded YUV frame to RGB24 so the presentation path stays identical to MJPEG.
 */

struct h264_decoder {
    AVCodecContext *ctx;
    AVPacket       *pkt;
    AVFrame        *frame;
    struct SwsContext *sws;
    int             sws_w, sws_h;
    enum AVPixelFormat sws_src;
};

h264_decoder_t *h264_decoder_create(void)
{
    h264_decoder_t *d = (h264_decoder_t *)calloc(1, sizeof(*d));
    if (!d)
        return NULL;

    /* The PS4 app writes its own log; keep FFmpeg's noisy console output off. */
    av_log_set_level(AV_LOG_QUIET);

    const AVCodec *codec = avcodec_find_decoder(AV_CODEC_ID_H264);
    if (!codec)
        goto fail;

    d->ctx = avcodec_alloc_context3(codec);
    if (!d->ctx)
        goto fail;

    /* Single-threaded: the FFmpeg build has no pthreads (matches the proven
     * Moonlight-ps4 configuration). Tolerate a bit of non-standard input. */
    d->ctx->thread_count = 1;
    d->ctx->flags2 |= AV_CODEC_FLAG2_FAST;

    if (avcodec_open2(d->ctx, codec, NULL) < 0)
        goto fail;

    d->pkt = av_packet_alloc();
    d->frame = av_frame_alloc();
    if (!d->pkt || !d->frame)
        goto fail;

    return d;

fail:
    h264_decoder_destroy(d);
    return NULL;
}

void h264_decoder_destroy(h264_decoder_t *d)
{
    if (!d)
        return;
    if (d->sws)
        sws_freeContext(d->sws);
    if (d->frame)
        av_frame_free(&d->frame);
    if (d->pkt)
        av_packet_free(&d->pkt);
    if (d->ctx)
        avcodec_free_context(&d->ctx);
    free(d);
}

static int convert_to_rgb24(h264_decoder_t *d, const AVFrame *f,
                            uint8_t *out, size_t out_cap, int *outW, int *outH)
{
    int w = f->width;
    int h = f->height;

    if (w <= 0 || h <= 0 || (size_t)w * (size_t)h * 3u > out_cap)
        return -2;

    if (!d->sws || d->sws_w != w || d->sws_h != h || d->sws_src != (enum AVPixelFormat)f->format) {
        if (d->sws)
            sws_freeContext(d->sws);
        d->sws = sws_getContext(w, h, (enum AVPixelFormat)f->format,
                                w, h, AV_PIX_FMT_RGB24,
                                SWS_BILINEAR, NULL, NULL, NULL);
        if (!d->sws)
            return -3;
        d->sws_w = w;
        d->sws_h = h;
        d->sws_src = (enum AVPixelFormat)f->format;
    }

    uint8_t *dst_data[4] = { out, NULL, NULL, NULL };
    int dst_linesize[4] = { w * 3, 0, 0, 0 };
    sws_scale(d->sws, (const uint8_t *const *)f->data, f->linesize,
              0, h, dst_data, dst_linesize);

    *outW = w;
    *outH = h;
    return 0;
}

int h264_decoder_decode(h264_decoder_t *d, const uint8_t *au, size_t len,
                        uint8_t *out, size_t out_cap, int *outW, int *outH)
{
    if (!d || !d->ctx || !au || len == 0 || !out)
        return -1;

    av_packet_unref(d->pkt);
    if (av_new_packet(d->pkt, (int)len) < 0)
        return -1;
    memcpy(d->pkt->data, au, len);

    int ret = avcodec_send_packet(d->ctx, d->pkt);
    av_packet_unref(d->pkt);
    if (ret < 0 && ret != AVERROR(EAGAIN))
        return -1;

    int rc = 1; /* consumed, no frame yet */
    while ((ret = avcodec_receive_frame(d->ctx, d->frame)) == 0) {
        int w = 0, h = 0;
        int c = convert_to_rgb24(d, d->frame, out, out_cap, &w, &h);
        av_frame_unref(d->frame);
        if (c == 0) {
            *outW = w;
            *outH = h;
            rc = 0;
        } else {
            rc = -2;
        }
    }

    if (ret < 0 && ret != AVERROR(EAGAIN) && ret != AVERROR_EOF) {
        /* Corrupt / desynced stream: force a reset so we wait for the next IDR. */
        avcodec_flush_buffers(d->ctx);
        return -1;
    }

    return rc;
}
