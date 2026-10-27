#include "input.h"
#include "dlog.h"

#include <string.h>
#include <stdio.h>

#include <orbis/Pad.h>
#include <orbis/UserService.h>
#include <orbis/libkernel.h>

/*
 * DualShock 4 reader for OpenOrbis.
 *
 * Verified against OpenOrbis v0.5.4 headers:
 *   include/orbis/Pad.h        -> scePadInit(void), scePadOpen(user,type,index,param),
 *                                 scePadReadState(handle, OrbisPadData*)
 *   include/orbis/_types/pad.h -> OrbisPadData, ORBIS_PAD_BUTTON_*, stick/analog
 *   include/orbis/UserService.h-> sceUserServiceGetInitialUser
 *
 * NOTE (honesty): this has not been run on real hardware. The API signatures
 * are verified from the shipped headers, but runtime behavior (which user id
 * is active, pad connect sequence) still needs a real PS4 to confirm.
 */

static int g_pad_handle = -1;

int input_init(void)
{
    /* scePadInit() takes no arguments in this toolchain. */
    int rc = scePadInit();
    if (rc < 0)
        dlog("scePadInit failed: 0x%08X", (unsigned)rc);

    /* Resolve the first logged-in user; pads are opened per user. */
    int user_id = -1;
    OrbisUserServiceInitializeParams param;
    param.priority = ORBIS_KERNEL_PRIO_FIFO_LOWEST;
    sceUserServiceInitialize(&param);
    sceUserServiceGetInitialUser(&user_id);
    if (user_id < 0)
        user_id = 0; /* fall back to default user */

    g_pad_handle = scePadOpen(user_id, ORBIS_PAD_PORT_TYPE_STANDARD, 0, NULL);
    if (g_pad_handle < 0) {
        dlog("pad open FAILED user=%d rc=0x%08X", user_id, (unsigned)g_pad_handle);
        g_pad_handle = -1;
        return -1;
    }
    dlog("pad open OK user=%d handle=%d", user_id, g_pad_handle);
    return 0;
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

int input_read(input_pkt_t *pkt)
{
    memset(pkt, 0, sizeof(*pkt));
    pkt->magic = INPUT_MAGIC;
    pkt->version = INPUT_VERSION;

    if (g_pad_handle < 0)
        return -1;

    OrbisPadData pad;
    memset(&pad, 0, sizeof(pad));

    /* scePadReadState fills the current state without blocking. */
    int rc = scePadReadState(g_pad_handle, &pad);
    if (rc < 0)
        return -1; /* pad disconnected: caller sends a neutral packet */

    pkt->buttons = map_buttons(pad.buttons);

    pkt->lx = stick_to_i16(pad.leftStick.x);
    pkt->ly = stick_to_i16(pad.leftStick.y);
    pkt->rx = stick_to_i16(pad.rightStick.x);
    pkt->ry = stick_to_i16(pad.rightStick.y);

    pkt->l2 = pad.analogButtons.l2;
    pkt->r2 = pad.analogButtons.r2;
    return 0;
}

void input_close(void)
{
    if (g_pad_handle >= 0) {
        scePadClose(g_pad_handle);
        g_pad_handle = -1;
    }
}
