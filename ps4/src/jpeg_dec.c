#include "jpeg_dec.h"

#include <setjmp.h>
#include <stdlib.h>
#include <string.h>

/* jpeglib.h deliberately does not pull in <stdio.h>/<stddef.h>; it requires the
 * application to define FILE and size_t first (see its header comment). */
#include <stddef.h>
#include <stdio.h>

#include <jpeglib.h>

struct jpeg_decoder {
    int unused;
};

/*
 * libjpeg-turbo error handling: the default handler calls exit(), which is not
 * acceptable in a game loop. We longjmp back to decode() instead.
 */
struct rp4_jpeg_error {
    struct jpeg_error_mgr pub;
    jmp_buf jb;
};

static void rp4_error_exit(j_common_ptr cinfo)
{
    struct rp4_jpeg_error *e = (struct rp4_jpeg_error *)cinfo->err;
    longjmp(e->jb, 1);
}

/* A memory source manager, since we decode from a RAM buffer rather than a
 * FILE* (and the toolchain has no jpeg_mem_src guarantee). */
typedef struct {
    struct jpeg_source_mgr pub;
    JOCTET eoi[2];
} rp4_mem_src;

static void mem_init_source(j_decompress_ptr cinfo)
{
    rp4_mem_src *s = (rp4_mem_src *)cinfo->src;
    s->eoi[0] = 0xFF;
    s->eoi[1] = JPEG_EOI;
}

static boolean mem_fill_input_buffer(j_decompress_ptr cinfo)
{
    rp4_mem_src *s = (rp4_mem_src *)cinfo->src;
    /* Ran off the end of the buffer: feed a synthetic EOI so baseline streams
     * terminate cleanly instead of erroring. */
    s->pub.next_input_byte = s->eoi;
    s->pub.bytes_in_buffer = 2;
    return TRUE;
}

static void mem_skip_input_data(j_decompress_ptr cinfo, long num_bytes)
{
    rp4_mem_src *s = (rp4_mem_src *)cinfo->src;
    if (num_bytes <= 0)
        return;
    if ((size_t)num_bytes <= s->pub.bytes_in_buffer) {
        s->pub.next_input_byte += num_bytes;
        s->pub.bytes_in_buffer -= (size_t)num_bytes;
    } else {
        s->pub.bytes_in_buffer = 0;
    }
}

static void mem_term_source(j_decompress_ptr cinfo)
{
    (void)cinfo;
}

static void mem_attach(j_decompress_ptr cinfo, const uint8_t *data, size_t len)
{
    rp4_mem_src *s;

    if (cinfo->src == NULL) {
        s = (rp4_mem_src *)(*cinfo->mem->alloc_small)(
            (j_common_ptr)cinfo, JPOOL_PERMANENT, sizeof(rp4_mem_src));
        cinfo->src = (struct jpeg_source_mgr *)s;
    } else {
        s = (rp4_mem_src *)cinfo->src;
    }

    s->pub.init_source = mem_init_source;
    s->pub.fill_input_buffer = mem_fill_input_buffer;
    s->pub.skip_input_data = mem_skip_input_data;
    s->pub.resync_to_restart = jpeg_resync_to_restart;
    s->pub.term_source = mem_term_source;
    s->pub.bytes_in_buffer = len;
    s->pub.next_input_byte = data;
}

jpeg_decoder_t *jpeg_decoder_create(void)
{
    jpeg_decoder_t *d = (jpeg_decoder_t *)calloc(1, sizeof(*d));
    return d;
}

void jpeg_decoder_destroy(jpeg_decoder_t *d)
{
    free(d);
}

int jpeg_decoder_decode(jpeg_decoder_t *d, const uint8_t *jpg, size_t len,
                        uint8_t *out, size_t out_cap, int *outW, int *outH)
{
    struct jpeg_decompress_struct cinfo;
    struct rp4_jpeg_error jerr;
    int w, h, stride;

    (void)d;
    if (!jpg || len == 0 || !out)
        return -1;

    memset(&cinfo, 0, sizeof(cinfo));
    cinfo.err = jpeg_std_error(&jerr.pub);
    jerr.pub.error_exit = rp4_error_exit;

    jpeg_create_decompress(&cinfo);

    if (setjmp(jerr.jb)) {
        jpeg_destroy_decompress(&cinfo);
        return -1;
    }

    mem_attach(&cinfo, jpg, len);

    jpeg_read_header(&cinfo, TRUE);
    cinfo.out_color_space = JCS_RGB;
    jpeg_start_decompress(&cinfo);

    w = (int)cinfo.output_width;
    h = (int)cinfo.output_height;
    stride = w * 3;

    if (w <= 0 || h <= 0 || (size_t)stride * (size_t)h > out_cap) {
        jpeg_destroy_decompress(&cinfo);
        return -2;
    }

    while (cinfo.output_scanline < cinfo.output_height) {
        JSAMPROW row = out + (size_t)cinfo.output_scanline * (size_t)stride;
        jpeg_read_scanlines(&cinfo, &row, 1);
    }

    jpeg_finish_decompress(&cinfo);
    jpeg_destroy_decompress(&cinfo);

    *outW = w;
    *outH = h;
    return 0;
}
