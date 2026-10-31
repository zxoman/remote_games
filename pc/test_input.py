"""Tests for multi-controller input (pc/protocol.py + pc/gamepad.py).

The wire format is the interesting part: player 0 must stay a byte-compatible
18-byte v1 packet forever (old receivers), while players 1..3 use a 19-byte v2
packet that old receivers reject on length. No sockets and no uinput/ViGEm are
involved.
"""

import pytest

from gamepad import VirtualPadSet
from protocol import (
    BTN_CIRCLE, BTN_CROSS, INPUT_LEN, INPUT_MAX_PLAYERS, INPUT_MULTI_LEN,
    INPUT_VERSION, INPUT_VERSION_MULTI, InputPacket,
)


def pkt(player=0, seq=1, buttons=BTN_CROSS, sticks=(1, 2, 3, 4)):
    lx, ly, rx, ry = sticks
    return InputPacket(INPUT_VERSION, seq, buttons, 10, 20, lx, ly, rx, ry, player)


# --- wire format ----------------------------------------------------------

def test_player_zero_keeps_the_classic_packet():
    p = pkt(player=0)
    wire = p.pack()
    assert len(wire) == INPUT_LEN == 18
    assert wire[:4] == b"RP4I"
    assert wire[4] == INPUT_VERSION          # version 1, not 2
    assert InputPacket.unpack(wire) == p


@pytest.mark.parametrize("player", [1, 2, 3])
def test_extra_players_use_the_appended_v2_format(player):
    wire = pkt(player=player).pack()
    assert len(wire) == INPUT_MULTI_LEN == 19
    assert wire[:4] == b"RP4I"
    assert wire[4] == INPUT_VERSION_MULTI
    assert wire[18] == player                 # player byte is last
    # The v1 body is byte-identical apart from the version byte.
    assert wire[:4] == pkt(player=0).pack()[:4]
    assert wire[5:18] == pkt(player=0).pack()[5:18]
    assert InputPacket.unpack(wire).player == player


def test_v2_round_trip_preserves_every_field():
    for player in range(INPUT_MAX_PLAYERS):
        p = InputPacket(INPUT_VERSION, 200, 0xBEEF, 7, 8, -1, -2, -3, -4, player)
        q = InputPacket.unpack(p.pack())
        assert q == p
        assert (q.buttons, q.l2, q.r2, q.lx, q.ly, q.rx, q.ry) == \
               (0xBEEF, 7, 8, -1, -2, -3, -4)
        assert q.version == (INPUT_VERSION if player == 0 else INPUT_VERSION_MULTI)


def test_version_follows_the_player_field():
    # A packet cannot exist in a state pack() would not emit.
    assert InputPacket(INPUT_VERSION_MULTI, 1, 0, 0, 0, 0, 0, 0, 0, 0).version == 1
    assert InputPacket(INPUT_VERSION, 1, 0, 0, 0, 0, 0, 0, 0, 2).version == 2


def test_an_old_receiver_sees_no_valid_packet_for_extra_players():
    # Pre-multi-pad receivers only call unpack() on the 18-byte v1 format; a
    # 19-byte datagram is not a v1 packet and must not decode as one.
    wire = pkt(player=1).pack()
    assert len(wire) != INPUT_LEN


@pytest.mark.parametrize("data", [
    b"", b"RP4I", b"RP4I\x01" + bytes(12),          # too short
    b"RP4I\x01" + bytes(14),                         # 19 bytes but v1
    b"RP4I\x02" + bytes(12),                         # 18 bytes but v2
    b"RP4M\x01" + bytes(13),                         # bad magic
    b"RP4I\x01" + bytes(13) + b"\x00\x00",           # 20 bytes
])
def test_rejects_wrong_lengths_and_magics(data):
    assert InputPacket.unpack(data) is None


def test_rejects_out_of_range_players():
    for player in (INPUT_MAX_PLAYERS, 200):
        wire = bytearray(pkt(player=1).pack())
        wire[18] = player
        assert InputPacket.unpack(bytes(wire)) is None


def test_player_zero_may_still_arrive_as_v2_and_is_normalised():
    # Not emitted by our sender, but accepting it keeps the parser liberal.
    wire = bytearray(pkt(player=1).pack())
    wire[18] = 0
    q = InputPacket.unpack(bytes(wire))
    assert q is not None and q.player == 0 and q.version == 1
    assert q.pack()[4] == INPUT_VERSION              # re-packed as v1


# --- routing to virtual pads ---------------------------------------------

class FakePad:
    def __init__(self, index):
        self.index = index
        self.applied = []
        self.resets = 0
        self.closed = 0

    def apply(self, p):
        self.applied.append(p)

    def reset(self):
        self.resets += 1

    def close(self):
        self.closed += 1


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make_set(max_players=INPUT_MAX_PLAYERS, timeout=1.0):
    clock = FakeClock()
    made = []

    def factory():
        pad = FakePad(len(made))
        made.append(pad)
        return pad

    pads = VirtualPadSet(max_players, factory=factory, clock=clock,
                         timeout=timeout)
    return pads, made, clock


def test_player_one_pad_exists_before_any_packet():
    pads, made, _ = make_set()
    assert len(made) == 1
    assert pads.active_players() == []


def test_pads_are_created_in_player_order():
    pads, made, _ = make_set()
    assert pads.apply(pkt(player=2)) is True
    assert [p.index for p in made] == [0, 1, 2]      # 0 and 1 created idle
    assert pads.active_players() == [2]
    assert made[2].applied[-1].player == 2


def test_first_packet_from_a_player_reports_new_connection_once():
    pads, _, _ = make_set()
    assert pads.apply(pkt(player=0)) is True
    assert pads.apply(pkt(player=0)) is False
    assert pads.active_players() == [0]


def test_players_beyond_max_are_ignored_not_fatal():
    pads, made, _ = make_set(max_players=2)
    assert pads.apply(pkt(player=2)) is False
    assert len(made) == 1
    assert pads.active_players() == []


def test_invalid_max_players_is_rejected():
    with pytest.raises(ValueError):
        VirtualPadSet(0)
    with pytest.raises(ValueError):
        VirtualPadSet(INPUT_MAX_PLAYERS + 1)


def test_silent_pad_is_reset_after_the_timeout():
    pads, made, clock = make_set(timeout=1.0)
    pads.apply(pkt(player=1))
    clock.t += 0.9
    assert pads.tick() == []
    clock.t += 0.2
    assert pads.tick() == [1]
    assert made[1].resets == 1
    assert pads.active_players() == []
    # Nothing is reset twice.
    assert pads.tick() == []


def test_a_packet_keeps_the_pad_alive():
    pads, made, clock = make_set(timeout=1.0)
    for _ in range(5):
        pads.apply(pkt(player=0))
        clock.t += 0.5
        assert pads.tick() == []
    assert made[0].resets == 0


def test_idle_created_pads_are_not_ticked():
    pads, made, clock = make_set(timeout=1.0)
    pads.apply(pkt(player=2))          # creates pads 0, 1, 2
    clock.t += 5.0
    assert pads.tick() == [2]          # only the pad that saw a packet
    assert made[0].resets == 0
    assert made[1].resets == 0


def test_close_closes_every_created_pad_and_forgets_state():
    pads, made, _ = make_set()
    pads.apply(pkt(player=1))
    pads.close()
    assert [p.closed for p in made] == [1, 1]
    assert pads.active_players() == []
    assert pads.tick() == []
