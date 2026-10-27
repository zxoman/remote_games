#include "dlog.h"
#include "net_udp.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include <orbis/libkernel.h>

static udp_conn_t g_sock;
static int        g_sock_ok;

void dlog_init(const char *dest_ip, unsigned short port)
{
    if (dest_ip && port && udp_open(&g_sock, dest_ip, port, 0) == 0)
        g_sock_ok = 1;
}

void dlog(const char *fmt, ...)
{
    char msg[256];
    char line[300];
    va_list ap;

    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);

    unsigned ms = (unsigned)(sceKernelGetProcessTime() / 1000ull);
    snprintf(line, sizeof(line), "[%u ms] %s\n", ms, msg);

    if (g_sock_ok)
        udp_send(&g_sock, line, (uint16_t)strlen(line));

    /* Channel 0 is the conventional user/klog channel. Pass through as "%s" so
     * a stray '%' in the message is never treated as a format specifier. */
    sceKernelDebugOutText(0, "%s", line);

    fputs(line, stdout);
}

void dlog_close(void)
{
    if (g_sock_ok) {
        udp_close(&g_sock);
        g_sock_ok = 0;
    }
}
