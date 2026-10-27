"""Cross-platform virtual gamepad.

Maps DS4 input packets (from the PS4 or fake_sender.py) to an emulated
Xbox 360 controller:

  * Windows -> ViGEmBus via `vgamepad`
  * Linux   -> kernel uinput via `evdev` (needs /dev/uinput access)

Both backends share one button/axis mapping expressed in neutral names
(a/b/x/y/lb/rb/ls/rs/back/start + dpad + triggers + sticks), so the protocol
translation lives in exactly one place.
"""

import atexit
import sys

from protocol import (
    InputPacket,
    BTN_CROSS, BTN_CIRCLE, BTN_SQUARE, BTN_TRIANGLE,
    BTN_L1, BTN_R1, BTN_L2, BTN_R2,
    BTN_SHARE, BTN_OPTIONS, BTN_L3, BTN_R3,
    BTN_UP, BTN_DOWN, BTN_LEFT, BTN_RIGHT,
)

# Protocol button bit -> neutral face/shoulder name.
# DS4 face buttons map to XInput *positions* (Cross=bottom=A, etc.).
_BUTTON_MAP = (
    ("a", BTN_CROSS), ("b", BTN_CIRCLE), ("x", BTN_SQUARE), ("y", BTN_TRIANGLE),
    ("lb", BTN_L1), ("rb", BTN_R1),
    ("ls", BTN_L3), ("rs", BTN_R3),
    ("back", BTN_SHARE), ("start", BTN_OPTIONS),
)


def _clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------
class _VigemBackend:
    """Windows backend using the ViGEmBus driver through `vgamepad`."""

    def __init__(self):
        import vgamepad as vg
        self._vg = vg
        self._pad = vg.VX360Gamepad()
        self._btn = {
            "a": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
            "b": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
            "x": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
            "y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
            "lb": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
            "rb": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
            "ls": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
            "rs": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
            "back": vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
            "start": vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
        }
        self._dpad = {
            "up": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
            "down": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
            "left": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
            "right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
        }

    def button(self, name, pressed):
        b = self._btn[name]
        if pressed:
            self._pad.press_button(b)
        else:
            self._pad.release_button(b)

    def dpad(self, up, down, left, right):
        for name, on in (("up", up), ("down", down), ("left", left), ("right", right)):
            b = self._dpad[name]
            if on:
                self._pad.press_button(b)
            else:
                self._pad.release_button(b)

    def trigger(self, side, value):
        v = _clamp(value, 0, 255)
        if side == "l":
            self._pad.left_trigger(value=v)
        else:
            self._pad.right_trigger(value=v)

    def stick(self, side, x, y):
        x, y = _clamp(x, -32768, 32767), _clamp(y, -32768, 32767)
        if side == "l":
            self._pad.left_joystick(x_value=x, y_value=y)
        else:
            self._pad.right_joystick(x_value=x, y_value=y)

    def commit(self):
        self._pad.update()

    def reset(self):
        self._pad.reset()
        self._pad.update()

    def close(self):
        self._pad.reset()
        self._pad.update()


class _UInputBackend:
    """Linux backend using the uinput kernel device via `evdev`."""

    def __init__(self):
        from evdev import UInput, ecodes as e, AbsInfo
        self._e = e
        cap = {
            e.EV_KEY: [
                e.BTN_A, e.BTN_B, e.BTN_X, e.BTN_Y,
                e.BTN_TL, e.BTN_TR, e.BTN_SELECT, e.BTN_START,
                e.BTN_MODE, e.BTN_THUMBL, e.BTN_THUMBR,
            ],
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, -32768, 32767, 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, -32768, 32767, 0, 0, 0)),
                (e.ABS_RX, AbsInfo(0, -32768, 32767, 0, 0, 0)),
                (e.ABS_RY, AbsInfo(0, -32768, 32767, 0, 0, 0)),
                (e.ABS_Z, AbsInfo(0, 0, 255, 0, 0, 0)),    # left trigger
                (e.ABS_RZ, AbsInfo(0, 0, 255, 0, 0, 0)),   # right trigger
                (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)), # dpad X
                (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)), # dpad Y
            ],
        }
        # Advertise as a real Xbox 360 pad so SDL/games pick the right mapping.
        self._pad = UInput(cap, name="Microsoft X-Box 360 pad",
                           vendor=0x045e, product=0x028e,
                           version=0x0114, bustype=e.BUS_USB)
        self._btn = {
            "a": e.BTN_A, "b": e.BTN_B, "x": e.BTN_X, "y": e.BTN_Y,
            "lb": e.BTN_TL, "rb": e.BTN_TR,
            "ls": e.BTN_THUMBL, "rs": e.BTN_THUMBR,
            "back": e.BTN_SELECT, "start": e.BTN_START,
        }

    def button(self, name, pressed):
        self._pad.write(self._e.EV_KEY, self._btn[name], 1 if pressed else 0)

    def dpad(self, up, down, left, right):
        x = (-1 if left else 0) + (1 if right else 0)
        y = (-1 if up else 0) + (1 if down else 0)
        self._pad.write(self._e.EV_ABS, self._e.ABS_HAT0X, x)
        self._pad.write(self._e.EV_ABS, self._e.ABS_HAT0Y, y)

    def trigger(self, side, value):
        v = _clamp(value, 0, 255)
        code = self._e.ABS_Z if side == "l" else self._e.ABS_RZ
        self._pad.write(self._e.EV_ABS, code, v)

    def stick(self, side, x, y):
        x, y = _clamp(x, -32768, 32767), _clamp(y, -32768, 32767)
        if side == "l":
            self._pad.write(self._e.EV_ABS, self._e.ABS_X, x)
            self._pad.write(self._e.EV_ABS, self._e.ABS_Y, y)
        else:
            self._pad.write(self._e.EV_ABS, self._e.ABS_RX, x)
            self._pad.write(self._e.EV_ABS, self._e.ABS_RY, y)

    def commit(self):
        self._pad.syn()

    def reset(self):
        for name in self._btn:
            self.button(name, False)
        self.dpad(False, False, False, False)
        self.trigger("l", 0)
        self.trigger("r", 0)
        self.stick("l", 0, 0)
        self.stick("r", 0, 0)
        self.commit()

    def close(self):
        try:
            self.reset()
            self._pad.close()
        except Exception:
            pass


def _make_backend():
    if sys.platform.startswith("win"):
        return _VigemBackend()
    if sys.platform.startswith("linux"):
        return _UInputBackend()
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


class VirtualPad:
    def __init__(self):
        self._b = _make_backend()
        atexit.register(self.close)

    def apply(self, pkt: InputPacket) -> None:
        for name, bit in _BUTTON_MAP:
            self._b.button(name, bool(pkt.buttons & bit))
        self._b.dpad(
            bool(pkt.buttons & BTN_UP), bool(pkt.buttons & BTN_DOWN),
            bool(pkt.buttons & BTN_LEFT), bool(pkt.buttons & BTN_RIGHT),
        )
        self._b.trigger("l", pkt.l2)
        self._b.trigger("r", pkt.r2)
        self._b.stick("l", pkt.lx, pkt.ly)
        self._b.stick("r", pkt.rx, pkt.ry)
        self._b.commit()

    def reset(self) -> None:
        self._b.reset()

    def close(self) -> None:
        self._b.close()
