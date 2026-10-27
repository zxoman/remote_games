#pragma once

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Lightweight debug logging.
 *
 * Design goal: never disturb the input loop or the video pipeline. Therefore:
 *   - the UDP log socket is separate from the input socket and non-blocking;
 *   - dlog() is EVENT-DRIVEN only — do NOT call it from per-packet / per-frame
 *     hot paths. Startup, state changes, and a ~1 Hz stats line are fine.
 *
 * Each line is sent to the PC (so you can watch it with pc/log_viewer.py),
 * written to the kernel debug log (klog, via sceKernelDebugOutText), and echoed
 * to stdout.
 */

/* Open the UDP log socket to dest_ip:port. Safe to call once at startup. */
void dlog_init(const char *dest_ip, unsigned short port);

/* Formatted log line (printf-style), newline-terminated. Event-driven only. */
void dlog(const char *fmt, ...);

void dlog_close(void);

#ifdef __cplusplus
}
#endif
