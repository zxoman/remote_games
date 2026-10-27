#pragma once

#include <stdint.h>
#include <netinet/in.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int fd;
    struct sockaddr_in dest; /* PC address */
} udp_conn_t;

/* Create a UDP socket bound to any local address on `bind_port` (0 = ephemeral),
 * with `dest_ip`:`dest_port` as the default send target. Returns 0 on success. */
int udp_open(udp_conn_t *c, const char *dest_ip, uint16_t dest_port, uint16_t bind_port);

/* Create a non-blocking UDP socket bound to `bind_port` for receiving only
 * (no default destination). Returns 0 on success. */
int udp_open_listen(udp_conn_t *c, uint16_t bind_port);

/* Send `len` bytes to the configured destination. Returns bytes sent or -1. */
int udp_send(udp_conn_t *c, const void *buf, uint16_t len);

/* Receive one datagram. Returns bytes received, 0 if nothing is pending
 * (non-blocking), or a negative errno-style value on error. */
int udp_recv(udp_conn_t *c, void *buf, uint16_t len);

void udp_close(udp_conn_t *c);

#ifdef __cplusplus
}
#endif
