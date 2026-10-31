#pragma once

/*
 * Runtime configuration.
 * For Phase 1 these are compile-time defaults; Phase 4 will load a config
 * file from the app's data directory (ux0:app/...) at startup.
 */

/* PC's LAN IP (the machine running pc/receive_input.py).
 * The PS4 sends input, feedback (47002) and dlog (47003) here, so it must be
 * an address that PC actually has. Nothing on the wire carries it, which is
 * why the two machines' configs can disagree.
 * Current setup: PS4 = 192.168.1.14, PC = 192.168.1.16 (DHCP) with .15 added
 * as a secondary address (`ip addr add 192.168.1.15/24 dev eno1`) because the
 * installed PKG was built against .15. */
#define CFG_PC_IP       "192.168.1.15"

/* PC input listener port (matches pc/config.ini [input] listen_port). */
#define CFG_INPUT_PORT  47001

/* Stats port (Phase 4; opened but unused in Phase 1). */
#define CFG_STATS_PORT  47002

/* Feedback port (Step 2): the PS4 sends STATS + KEYFRAME_REQUEST here.
 * Matches pc/config.ini [video] feedback_port. */
#define CFG_FEEDBACK_PORT 47002

/* Input send rate in Hz. DS4 over Bluetooth polls at ~250 Hz (USB ~1000). */
#define CFG_INPUT_RATE_HZ 250

/* Video (Phase 2): MJPEG received from the PC on this UDP port.
 * NOTE: the PS4 blocks binding several port ranges (47010 gives EACCES);
 * 9025 is known to bind. Keep this in sync with pc/config.ini [video]. */
#define CFG_VIDEO_PORT   9025

/* Expected frame geometry (must match pc/config.ini [video]). */
#define CFG_VIDEO_WIDTH  1280
#define CFG_VIDEO_HEIGHT 720

/* Debug log is sent to the PC on this UDP port (pc/log_viewer.py). */
#define CFG_LOG_PORT     47003
