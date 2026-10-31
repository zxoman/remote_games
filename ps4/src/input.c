#include "input.h"
#include "dlog.h"

#include <string.h>
#include <stdio.h>

#include <orbis/Pad.h>
#include <orbis/UserService.h>
#include <orbis/libkernel.h>

/*
 * DualShock 4 reader for OpenOrbis — one pad per player slot.
 *
 * Verified against OpenOrbis v0.5.4 headers:
 *   include/orbis/Pad.h        -> scePadInit(void), scePadOpen(user,type,index,param),
 *                                 scePadReadState(handle, OrbisPadData*)
 *   include/orbis/_types/pad.h -> OrbisPadData, ORBIS_PAD_BUTTON_*, stick/analog
 *                                 (OrbisPadData.connected is part of that struct)
 *   include/orbis/UserService.h-> sceUserServiceGetInitialUser,
 *                                 sceUserServiceGetLoginUserIdList
 *
 * Player mapping: slot i is the i-th logged-in user. The PS4 ties each pad to
 * a user account, so player 1 = the first logged-in user, player 2 = the
 * second, and so on (ORBIS_USER_SERVICE_MAX_LOGIN_USERS == 4). If a slot has no
 * logged-in user we fall back to opening pad `index = slot` for the initial
 * user, which covers setups that expose several pads for one account.
 *
 * NOTE (honesty): this has not been run on real hardware. The API signatures
 * and the login-list layout are taken from the shipped headers, but the pad
 * slot <-> user mapping, hot-plug timing and the `connected` flag still need a
 * real PS4 to confirm. See docs/protocol.md §1 (multiple controllers).
 */

/* How often to retry a pad slot that failed to open (hot-plug). */
#define PAD_REOPEN_US 2000000ull
/* How long `OrbisPadData.connected == 0` must persist before we close the
 * handle. A spurious zero for one poll must not drop a working pad. */
#define PAD_LOST_US   500000ull

static int      g_pad[INPUT_MAX_PLAYERS];   /* handle, or -1 when closed */
static int32_t  g_user[INPUT_MAX_PLAYERS];  /* user that owns the slot */
static int      g_user_id = -1;             /* initial/primary user */
static uint64_t g_reopen_at[INPUT_MAX_PLAYERS];
static uint64_t g_lost_since[INPUT_MAX_PLAYERS];
static int      g_open;                     /* handles currently open */
static int      g_logged_open[INPUT_MAX_PLAYERS]; /* last logged state */

static void close_slot(int slot, const char *why)
{
    if (g_pad[slot] < 0)
        return;
    scePadClose(g_pad[slot]);
    g_pad[slot] = -1;
    g_lost_since[slot] = 0;
    g_reopen_at[slot] = sceKernelGetProcessTime() + PAD_REOPEN_US;
    if (g_open > 0)
        g_open--;
    if (g_logged_open[slot]) {
        g_logged_open[slot] = 0;
        dlog("pad slot %d: closed (%s)", slot, why);
    }
}

static int open_slot(int slot)
{
    uint64_t now = sceKernelGetProcessTime();
    if (g_pad[slot] >= 0 || now < g_reopen_at[slot])
        return g_pad[slot] >= 0 ? 0 : -1;

    int32_t uid = g_user[slot];
    int h = -1;
    if (uid >= 0)
        h = scePadOpen(uid, ORBIS_PAD_PORT_TYPE_STANDARD, 0, NULL);
    /* Fallback: same account, explicit pad index. */
    if (h < 0 && g_user_id >= 0 && (uid < 0 || uid != g_user_id))
        h = scePadOpen(g_user_id, ORBIS_PAD_PORT_TYPE_STANDARD, slot, NULL);

    if (h < 0) {
        g_reopen_at[slot] = now + PAD_REOPEN_US;
        return -1;
    }

    g_pad[slot] = h;
    g_lost_since[slot] = 0;
    g_open++;
    if (!g_logged_open[slot]) {
        g_logged_open[slot] = 1;
        dlog("pad slot %d: open (user %d, handle %d)", slot, (int)uid, h);
    }
    return 0;
}

int input_init(void)
{
    for (int i = 0; i < INPUT_MAX_PLAYERS; i++) {
        g_pad[i] = -1;
        g_user[i] = -1;
        g_reopen_at[i] = 0;
        g_lost_since[i] = 0;
        g_logged_open[i] = 0;
    }
    g_open = 0;

    /* scePadInit() takes no arguments in this toolchain. */
    int rc = scePadInit();
    if (rc < 0)
        dlog("scePadInit failed: 0x%08X", (unsigned)rc);

    OrbisUserServiceInitializeParams param;
    param.priority = ORBIS_KERNEL_PRIO_FIFO_LOWEST;
    sceUserServiceInitialize(&param);

    g_user_id = -1;
    sceUserServiceGetInitialUser(&g_user_id);
    if (g_user_id < 0)
        g_user_id = 0; /* fall back to default user */

    /* Slot i = i-th logged-in user (ORBIS_USER_SERVICE_USER_ID_INVALID when
     * that player slot has nobody logged in). */
    OrbisUserServiceLoginUserIdList list;
    memset(&list, 0xFF, sizeof(list));
    int lrc = sceUserServiceGetLoginUserIdList(&list);
    if (lrc == 0) {
        for (int i = 0; i < INPUT_MAX_PLAYERS; i++)
            g_user[i] = list.userId[i];
    }
    if (g_user[0] < 0)
        g_user[0] = g_user_id;

    int opened = 0;
    for (int i = 0; i < INPUT_MAX_PLAYERS; i++)
        opened += (open_slot(i) == 0);

    dlog("input_init: %d/%d pad(s) open (user %d, login list %s)",
         opened, INPUT_MAX_PLAYERS, (int)g_user_id, lrc == 0 ? "OK" : "FAILED");
    return opened > 0 ? 0 : -1;
}

/* Map OrbisPadButton bits -> our protocol bitmask. */
static uint16_t map_buttons(uint32_t raw)
{
    uint16_t out = 0;
    if (raw & ORBIS_PAD_BUTTON_CROSS)    out |= BTN_CROSS;
    if (raw & ORBIS_PAD_BUTTON_CIRCLE)   out |= BTN_CIRCLE;
    if (raw & ORBIS_PAD_BUTTON_SQUARE)   out |= BTN_SQUARE;
    if (raw & ORBIS_PAD_BUTTON_TRIANGLE) out |= BTN_TRIANGLE;
    if (raw & ORBIS_PAD_BUTTON_L1)       out |= BTN_L1;
    if (raw & ORBIS_PAD_BUTTON_R1)       out |= BTN_R1;
    if (raw & ORBIS_PAD_BUTTON_L2)       out |= BTN_L2;
    if (raw & ORBIS_PAD_BUTTON_R2)       out |= BTN_R2;
    if (raw & ORBIS_PAD_BUTTON_OPTIONS)  out |= BTN_OPTIONS;
    if (raw & ORBIS_PAD_BUTTON_L3)       out |= BTN_L3;
    if (raw & ORBIS_PAD_BUTTON_R3)       out |= BTN_R3;
    if (raw & ORBIS_PAD_BUTTON_UP)       out |= BTN_UP;
    if (raw & ORBIS_PAD_BUTTON_DOWN)     out |= BTN_DOWN;
    if (raw & ORBIS_PAD_BUTTON_LEFT)     out |= BTN_LEFT;
    if (raw & ORBIS_PAD_BUTTON_RIGHT)    out |= BTN_RIGHT;
    /*
     * SHARE: OpenOrbis v0.5.4's OrbisPadButton enum defines the lowest bit as
     * 0x0002 (L3), leaving 0x0001 unnamed. On a real DS4 that bit is SHARE.
     * Uncomment once confirmed on hardware:
     *   if (raw & 0x0001) out |= BTN_SHARE;
     */
    return out;
}

/* Small deadzone so drifting sticks don't jitter the PC pad. */
static int16_t apply_deadzone(int16_t v)
{
    const int16_t DZ = 3000; /* ~9% of full 16-bit range */
    return (v > -DZ && v < DZ) ? 0 : v;
}

/* u8 (0..255, 128 center) -> i16 (-32768..32767) */
static int16_t stick_to_i16(uint8_t v)
{
    return apply_deadzone((int16_t)(((int)v - 128) * 256));
}

int input_poll(input_sample_t *out, int max)
{
    int n = 0;
    uint64_t now = sceKernelGetProcessTime();

    for (int slot = 0; slot < INPUT_MAX_PLAYERS && n < max; slot++) {
        if (g_pad[slot] < 0) {
            if (open_slot(slot) != 0)
                continue;
        }

        OrbisPadData pad;
        memset(&pad, 0, sizeof(pad));

        /* scePadReadState fills the current state without blocking. */
        int rc = scePadReadState(g_pad[slot], &pad);
        if (rc < 0) {
            close_slot(slot, "read error");
            continue;
        }

        /* TODO(verify-on-PS4): `connected` is part of the reversed struct but
         * has not been observed on hardware. A false zero only costs a 0.5 s
         * input freeze (then the handle is re-opened), and the PC resets the
         * virtual pad after 1 s of silence either way. */
        if (!pad.connected) {
            if (g_lost_since[slot] == 0)
                g_lost_since[slot] = now;
            else if (now - g_lost_since[slot] >= PAD_LOST_US)
                close_slot(slot, "disconnected");
            continue;
        }
        g_lost_since[slot] = 0;

        input_sample_t *s = &out[n++];
        memset(&s->pkt, 0, sizeof(s->pkt));
        s->player = (uint8_t)slot;
        s->pkt.magic = INPUT_MAGIC;
        s->pkt.version = INPUT_VERSION;
        s->pkt.buttons = map_buttons(pad.buttons);
        s->pkt.l2 = pad.analogButtons.l2;
        s->pkt.r2 = pad.analogButtons.r2;
        s->pkt.lx = stick_to_i16(pad.leftStick.x);
        s->pkt.ly = stick_to_i16(pad.leftStick.y);
        s->pkt.rx = stick_to_i16(pad.rightStick.x);
        s->pkt.ry = stick_to_i16(pad.rightStick.y);
    }
    return n;
}

int input_encode(const input_sample_t *s, uint8_t *buf, int cap)
{
    if (!s || !buf)
        return -1;

    /* Player 1 (slot 0) keeps the original wire format: 18-byte version 1. */
    if (s->player == 0) {
        if (cap < INPUT_PKT_LEN)
            return -1;
        memcpy(buf, &s->pkt, INPUT_PKT_LEN);
        return INPUT_PKT_LEN;
    }

    if (cap < INPUT_PKT_MULTI_LEN)
        return -1;
    input_multi_pkt_t m;
    memcpy(&m, &s->pkt, INPUT_PKT_LEN);
    m.base.version = INPUT_VERSION_MULTI;
    m.player = s->player;
    memcpy(buf, &m, INPUT_PKT_MULTI_LEN);
    return INPUT_PKT_MULTI_LEN;
}

int input_connected(void)
{
    return g_open;
}

int input_player_open(int player)
{
    if (player < 0 || player >= INPUT_MAX_PLAYERS)
        return 0;
    return g_pad[player] >= 0;
}

void input_close(void)
{
    for (int i = 0; i < INPUT_MAX_PLAYERS; i++)
        close_slot(i, "shutdown");
}
