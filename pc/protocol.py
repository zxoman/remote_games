"""Wire formats shared with the PS4 side. Mirrors docs/protocol.md.

All fields little-endian. Keep struct formats in sync with ps4/include/input.h.
"""

from dataclasses import dataclass
import struct

INPUT_MAGIC = b"RP4I"
INPUT_VERSION = 1
INPUT_STRUCT = struct.Struct("<4sBBHBBhhhh")
INPUT_LEN = INPUT_STRUCT.size  # 18

assert INPUT_LEN == 18

# Button bits — keep in sync with docs/protocol.md
BTN_SQUARE = 1 << 0
BTN_CROSS = 1 << 1
BTN_CIRCLE = 1 << 2
BTN_TRIANGLE = 1 << 3
BTN_L1 = 1 << 4
BTN_R1 = 1 << 5
BTN_L2 = 1 << 6
BTN_R2 = 1 << 7
BTN_SHARE = 1 << 8
BTN_OPTIONS = 1 << 9
BTN_L3 = 1 << 10
BTN_R3 = 1 << 11
BTN_UP = 1 << 12
BTN_DOWN = 1 << 13
BTN_LEFT = 1 << 14
BTN_RIGHT = 1 << 15

BUTTON_NAMES = {
    BTN_SQUARE: "square", BTN_CROSS: "cross", BTN_CIRCLE: "circle",
    BTN_TRIANGLE: "triangle", BTN_L1: "l1", BTN_R1: "r1",
    BTN_L2: "l2", BTN_R2: "r2", BTN_SHARE: "share", BTN_OPTIONS: "options",
    BTN_L3: "l3", BTN_R3: "r3", BTN_UP: "up", BTN_DOWN: "down",
    BTN_LEFT: "left", BTN_RIGHT: "right",
}


@dataclass
class InputPacket:
    version: int
    seq: int
    buttons: int
    l2: int
    r2: int
    lx: int
    ly: int
    rx: int
    ry: int

    def pack(self) -> bytes:
        return INPUT_STRUCT.pack(
            INPUT_MAGIC, self.version, self.seq, self.buttons,
            self.l2, self.r2, self.lx, self.ly, self.rx, self.ry,
        )

    @classmethod
    def unpack(cls, data: bytes):
        """Return InputPacket or None if the datagram isn't a valid RP4I packet."""
        if len(data) != INPUT_LEN:
            return None
        magic, version, seq, buttons, l2, r2, lx, ly, rx, ry = \
            INPUT_STRUCT.unpack(data)
        if magic != INPUT_MAGIC or version != INPUT_VERSION:
            return None
        return cls(version, seq, buttons, l2, r2, lx, ly, rx, ry)

    def pressed_buttons(self):
        return [name for bit, name in BUTTON_NAMES.items() if self.buttons & bit]


# --- Stats packet (Phase 4, defined now) ---

STATS_MAGIC = b"RP4S"
STATS_VERSION = 1
STATS_STRUCT = struct.Struct("<4sBBHHHI")
STATS_LEN = STATS_STRUCT.size  # 16

assert STATS_LEN == 16


@dataclass
class StatsPacket:
    loss_permil: int = 0   # packet loss per-mille (0-1000)
    jitter_us: int = 0
    rtt_ms: int = 0
    last_frame_seq: int = 0

    def pack(self) -> bytes:
        return STATS_STRUCT.pack(
            STATS_MAGIC, STATS_VERSION, 0,
            self.loss_permil, self.jitter_us, self.rtt_ms, self.last_frame_seq,
        )

    @classmethod
    def unpack(cls, data: bytes):
        if len(data) != STATS_LEN:
            return None
        magic, ver, _, loss, jitter, rtt, frame = STATS_STRUCT.unpack(data)
        if magic != STATS_MAGIC or ver != STATS_VERSION:
            return None
        return cls(loss, jitter, rtt, frame)


# --- Video packet (Phase 2 MJPEG, Phase 3 H.264 over UDP) ---

VIDEO_MAGIC = b"RP4V"
VIDEO_VERSION = 1
VIDEO_FLAG_KEYFRAME = 0x01  # H.264: access unit contains an IDR slice
VIDEO_FLAG_H264 = 0x02      # payload is H.264, not JPEG

# magic, version, flags, chunk_idx, chunk_cnt, frame_id, payload_len
VIDEO_HDR_STRUCT = struct.Struct("<4sBBHHIH")
VIDEO_HDR_LEN = VIDEO_HDR_STRUCT.size  # 16
assert VIDEO_HDR_LEN == 16

VIDEO_MAX_DGRAM = 1200
VIDEO_CHUNK_PAYLOAD = VIDEO_MAX_DGRAM - VIDEO_HDR_LEN  # 1184

# Keep in sync with ps4/include/video_proto.h (max chunks the receiver buffers).
VIDEO_MAX_CHUNKS = 2048


@dataclass
class VideoHeader:
    version: int
    flags: int
    chunk_idx: int
    chunk_cnt: int
    frame_id: int
    payload_len: int

    def pack(self) -> bytes:
        return VIDEO_HDR_STRUCT.pack(
            VIDEO_MAGIC, self.version, self.flags,
            self.chunk_idx, self.chunk_cnt, self.frame_id, self.payload_len,
        )


def iter_chunks(frame_id: int, payload: bytes, flags: int = VIDEO_FLAG_KEYFRAME):
    """Yield one datagram per chunk. All but the last carry exactly
    VIDEO_CHUNK_PAYLOAD bytes, which the receiver relies on for fixed-stride
    reassembly (see ps4/src/video.c)."""
    n = (len(payload) + VIDEO_CHUNK_PAYLOAD - 1) // VIDEO_CHUNK_PAYLOAD
    for idx in range(n):
        chunk = payload[idx * VIDEO_CHUNK_PAYLOAD:(idx + 1) * VIDEO_CHUNK_PAYLOAD]
        hdr = VideoHeader(VIDEO_VERSION, flags, idx, n, frame_id, len(chunk))
        yield hdr.pack() + chunk


def h264_has_idr(au: bytes) -> bool:
    """True if an Annex-B access unit contains an IDR slice (NAL type 5).

    The PS4 decoder treats such an AU as a resync point (VIDEO_FLAG_KEYFRAME),
    so this must be exactly the NAL that starts a clean reference chain."""
    n = len(au)
    i = 0
    while i + 4 <= n:
        if au[i] == 0 and au[i + 1] == 0:
            if au[i + 2] == 1:            # 3-byte start code
                hdr = i + 3
            elif au[i + 2] == 0 and au[i + 3] == 1:  # 4-byte start code
                hdr = i + 4
            else:
                i += 1
                continue
            if hdr >= n:
                return False
            if (au[hdr] & 0x1F) == 5:     # 5 = coded slice of an IDR picture
                return True
            i = hdr + 1
        else:
            i += 1
    return False


# --- Feedback channel (PS4 -> PC): STATS + KEYFRAME_REQUEST ---
#
# New magic (`RP4F`) so it cannot collide with the legacy `RP4S` draft above
# (which nothing implements yet). Everything here is additive: old readers
# ignore datagrams whose magic they don't recognise.

FEEDBACK_MAGIC = b"RP4F"
FEEDBACK_VERSION = 1

FEEDBACK_TYPE_STATS = 1
FEEDBACK_TYPE_KEYFRAME_REQUEST = 2

# Common header: magic, version, type, seq. Every feedback packet starts with it.
FEEDBACK_HDR_STRUCT = struct.Struct("<4sBBH")
FEEDBACK_HDR_LEN = FEEDBACK_HDR_STRUCT.size  # 8

# KEYFRAME_REQUEST reason codes.
KF_REASON_INCOMPLETE = 0     # a frame was abandoned before all chunks arrived
KF_REASON_DECODE_ERROR = 1   # reassembly succeeded but the decoder failed
KF_REASON_STARTUP = 2        # receiver just (re)started and needs an IDR

# STATS: header + packets_expected, packets_received, frames_completed,
# frames_dropped, jitter_ms, last_frame_id, decoder_queue.
FEEDBACK_STATS_STRUCT = struct.Struct("<4sBBHIIIIHIH")
FEEDBACK_STATS_LEN = FEEDBACK_STATS_STRUCT.size  # 32

# KEYFRAME_REQUEST: header + last_frame_id, reason, reserved.
FEEDBACK_KFREQ_STRUCT = struct.Struct("<4sBBHIBB")
FEEDBACK_KFREQ_LEN = FEEDBACK_KFREQ_STRUCT.size  # 14

assert FEEDBACK_HDR_LEN == 8
assert FEEDBACK_STATS_LEN == 32
assert FEEDBACK_KFREQ_LEN == 14


@dataclass
class FeedbackStatsPacket:
    seq: int = 0
    packets_expected: int = 0
    packets_received: int = 0
    frames_completed: int = 0
    frames_dropped: int = 0
    jitter_ms: int = 0
    last_frame_id: int = 0
    decoder_queue: int = 0

    def pack(self) -> bytes:
        return FEEDBACK_STATS_STRUCT.pack(
            FEEDBACK_MAGIC, FEEDBACK_VERSION, FEEDBACK_TYPE_STATS, self.seq & 0xFFFF,
            self.packets_expected & 0xFFFFFFFF, self.packets_received & 0xFFFFFFFF,
            self.frames_completed & 0xFFFFFFFF, self.frames_dropped & 0xFFFFFFFF,
            min(int(self.jitter_ms), 0xFFFF), self.last_frame_id & 0xFFFFFFFF,
            min(int(self.decoder_queue), 0xFFFF),
        )

    @classmethod
    def unpack(cls, data: bytes):
        if len(data) != FEEDBACK_STATS_LEN:
            return None
        (magic, ver, typ, seq, expected, received, completed, dropped,
         jitter, last_frame, queue) = FEEDBACK_STATS_STRUCT.unpack(data)
        if magic != FEEDBACK_MAGIC or ver != FEEDBACK_VERSION or typ != FEEDBACK_TYPE_STATS:
            return None
        return cls(seq, expected, received, completed, dropped,
                   jitter, last_frame, queue)

    def loss_permil(self) -> int:
        """Packet loss in per-mille (0-1000), derived from expected vs received."""
        if self.packets_expected <= 0:
            return 0
        lost = self.packets_expected - self.packets_received
        if lost < 0:
            lost = 0
        return min(1000, int(lost * 1000 / self.packets_expected))


@dataclass
class KeyframeRequestPacket:
    seq: int = 0
    last_frame_id: int = 0
    reason: int = KF_REASON_INCOMPLETE

    def pack(self) -> bytes:
        return FEEDBACK_KFREQ_STRUCT.pack(
            FEEDBACK_MAGIC, FEEDBACK_VERSION, FEEDBACK_TYPE_KEYFRAME_REQUEST,
            self.seq & 0xFFFF, self.last_frame_id & 0xFFFFFFFF,
            self.reason & 0xFF, 0,
        )

    @classmethod
    def unpack(cls, data: bytes):
        if len(data) != FEEDBACK_KFREQ_LEN:
            return None
        magic, ver, typ, seq, last_frame, reason, _ = FEEDBACK_KFREQ_STRUCT.unpack(data)
        if (magic != FEEDBACK_MAGIC or ver != FEEDBACK_VERSION
                or typ != FEEDBACK_TYPE_KEYFRAME_REQUEST):
            return None
        return cls(seq, last_frame, reason)


def parse_feedback(data: bytes):
    """Return a StatsPacket-like feedback object, or None if malformed/unknown.

    Callers must treat None as "ignore this datagram" (magic, version and type
    are all validated here)."""
    if len(data) < FEEDBACK_HDR_LEN:
        return None
    magic, ver, typ, _seq = FEEDBACK_HDR_STRUCT.unpack(data[:FEEDBACK_HDR_LEN])
    if magic != FEEDBACK_MAGIC or ver != FEEDBACK_VERSION:
        return None
    if typ == FEEDBACK_TYPE_STATS:
        return FeedbackStatsPacket.unpack(data)
    if typ == FEEDBACK_TYPE_KEYFRAME_REQUEST:
        return KeyframeRequestPacket.unpack(data)
    return None


def parse_chunk(data: bytes):
    """Return (VideoHeader, payload) or None if `data` isn't a valid RP4V chunk."""
    if len(data) < VIDEO_HDR_LEN:
        return None
    magic, ver, flags, cidx, ccnt, fid, plen = VIDEO_HDR_STRUCT.unpack(
        data[:VIDEO_HDR_LEN])
    if magic != VIDEO_MAGIC or ver != VIDEO_VERSION:
        return None
    if plen == 0 or VIDEO_HDR_LEN + plen > len(data):
        return None
    payload = data[VIDEO_HDR_LEN:VIDEO_HDR_LEN + plen]
    return VideoHeader(ver, flags, cidx, ccnt, fid, plen), payload


if __name__ == "__main__":
    # Round-trip sanity check.
    p = InputPacket(1, 7, BTN_CROSS | BTN_UP, 128, 255, -100, -200, 300, 400)
    q = InputPacket.unpack(p.pack())
    assert q == p, q
    s = StatsPacket(25, 1500, 0, 1234)
    assert StatsPacket.unpack(s.pack()) == s

    # Video chunking round-trip: a 3-chunk "JPEG" reassembles exactly.
    fake = bytes(range(256)) * 14  # 3584 bytes -> 4 chunks (1184*3=3552)
    chunks = list(iter_chunks(42, fake))
    assert len(chunks) == 4, len(chunks)
    assert all(len(c) <= VIDEO_MAX_DGRAM for c in chunks)
    out = bytearray()
    for c in chunks:
        h, payload = parse_chunk(c)
        assert h.frame_id == 42
        out += payload
    assert bytes(out) == fake, "video reassembly mismatch"

    # Annex-B IDR detection (the PS4 uses this to pick a resync point).
    sps = b"\x00\x00\x00\x01\x67\x42\x00\x1e"           # NAL type 7
    idr = b"\x00\x00\x01\x65\x88\x84"                    # NAL type 5
    pframe = b"\x00\x00\x01\x41\x9a\x00"                 # NAL type 1
    assert h264_has_idr(sps + idr)
    assert not h264_has_idr(sps + pframe)
    assert not h264_has_idr(b"")

    # Feedback channel round-trips (Step 2).
    fs = FeedbackStatsPacket(seq=5, packets_expected=1000, packets_received=980,
                             frames_completed=300, frames_dropped=12,
                             jitter_ms=7, last_frame_id=123456, decoder_queue=1)
    fs2 = parse_feedback(fs.pack())
    assert fs2 == fs, fs2
    assert fs2.loss_permil() == 20, fs2.loss_permil()

    kf = KeyframeRequestPacket(seq=9, last_frame_id=777,
                               reason=KF_REASON_INCOMPLETE)
    assert parse_feedback(kf.pack()) == kf

    # Malformed/unknown feedback must be ignored, never raise.
    assert parse_feedback(b"") is None
    assert parse_feedback(b"XXXX" + b"\x00" * 24) is None
    bad_magic = bytearray(fs.pack()); bad_magic[0:4] = b"NOPE"
    assert parse_feedback(bytes(bad_magic)) is None
    bad_ver = bytearray(fs.pack()); bad_ver[4] = 99
    assert parse_feedback(bytes(bad_ver)) is None
    assert parse_feedback(fs.pack()[:-1]) is None            # truncated
    assert parse_feedback(b"RP4F\x01\x63\x00\x00") is None   # unknown type

    print("protocol self-test OK")