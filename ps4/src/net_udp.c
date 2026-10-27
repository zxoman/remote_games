#include "net_udp.h"

#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <fcntl.h>

int udp_open(udp_conn_t *c, const char *dest_ip, uint16_t dest_port, uint16_t bind_port)
{
    memset(c, 0, sizeof(*c));

    c->fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (c->fd < 0)
        return -errno;

    /* Non-blocking sends so a congested network never stalls the render loop. */
    int flags = fcntl(c->fd, F_GETFL, 0);
    if (flags >= 0)
        fcntl(c->fd, F_SETFL, flags | O_NONBLOCK);

    struct sockaddr_in local = {
        .sin_family = AF_INET,
        .sin_port = htons(bind_port),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(c->fd, (struct sockaddr *)&local, sizeof(local)) < 0) {
        int e = -errno;
        close(c->fd);
        c->fd = -1;
        return e;
    }

    c->dest.sin_family = AF_INET;
    c->dest.sin_port = htons(dest_port);
    if (inet_pton(AF_INET, dest_ip, &c->dest.sin_addr) != 1) {
        close(c->fd);
        c->fd = -1;
        return -EINVAL;
    }
    return 0;
}

int udp_send(udp_conn_t *c, const void *buf, uint16_t len)
{
    ssize_t n = sendto(c->fd, buf, len, 0,
                       (struct sockaddr *)&c->dest, sizeof(c->dest));
    return (n < 0) ? -errno : (int)n;
}

int udp_open_listen(udp_conn_t *c, uint16_t bind_port)
{
    memset(c, 0, sizeof(*c));

    c->fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (c->fd < 0)
        return -errno;

    int flags = fcntl(c->fd, F_GETFL, 0);
    if (flags >= 0)
        fcntl(c->fd, F_SETFL, flags | O_NONBLOCK);

    /* The receive loop blocks inside decode() for tens of milliseconds; while
     * it does, thousands of datagrams queue up. The FreeBSD default rcvbuf is
     * only ~42 KB (~35 datagrams), which overflows almost immediately. */
    int rcv = 2 * 1024 * 1024;
    setsockopt(c->fd, SOL_SOCKET, SO_RCVBUF, &rcv, sizeof(rcv));

    struct sockaddr_in local = {
        .sin_family = AF_INET,
        .sin_port = htons(bind_port),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(c->fd, (struct sockaddr *)&local, sizeof(local)) < 0) {
        int e = -errno;
        close(c->fd);
        c->fd = -1;
        return e;
    }
    return 0;
}

int udp_recv(udp_conn_t *c, void *buf, uint16_t len)
{
    ssize_t n = recvfrom(c->fd, buf, len, 0, NULL, NULL);
    if (n < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK)
            return 0;
        return -errno;
    }
    return (int)n;
}

void udp_close(udp_conn_t *c)
{
    if (c->fd >= 0) {
        close(c->fd);
        c->fd = -1;
    }
}
