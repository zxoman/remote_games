"""XOR forward error correction for Reverse Remote Play (Step 6).

The problem
-----------
A 720p30 access unit is ~20-26 datagrams. If **one** of them is lost the PS4
cannot decode the frame at all (an H.264 access unit is all-or-nothing), and the
reference chain is broken until the next IDR. On a 1 % loss link that is ~23 %
of frames (1 - (1 - 0.01)^26).

XOR FEC adds one parity datagram per group of `fec_group` data datagrams:

    P = D0 ^ D1 ^ ... ^ D(G-1)

The receiver reconstructs any *single* missing member of the group:

    Di = P ^ (all present Dj, j != i)

Cost: 1/G extra bandwidth (G = 8 -> +12.5 %). Benefit: a single loss per group
becomes invisible instead of costing a whole frame. Two losses in the same
group still fail — that is why the ABR tightens `fec_group` toward 4 when loss
is high.

Wire format (no new magic, no bigger datagram)
----------------------------------------------
A parity datagram reuses the 16-byte RP4V header:

    flags      |= VIDEO_FLAG_FEC_PARITY (0x04)
    chunk_idx   = (group_start & 0x7FF) | ((group_len - 1) << 11)
    chunk_cnt   = 2049, or 2049 + last_len for the group holding the final chunk
    frame_id    = the frame it protects
    payload_len = span (bytes of parity carried, 1..1184)

`chunk_cnt` is the compatibility trick: 2049 is larger than ``VIDEO_MAX_CHUNKS``,
so every receiver that predates Step 6 already rejects the datagram as invalid
(`ccnt > VIDEO_MAX_CHUNKS`, and `cidx >= ccnt` too). An old PKG silently ignores
parity datagrams and keeps working — it just does not recover anything.

The extra `last_len` in `chunk_cnt` solves a real problem: when the frame's
**final** chunk is the one that was lost, its length is unknown, and the
receiver needs it to trim the access unit. Without it the decoder would be fed
up to 1183 zero bytes of padding. The group that contains the final chunk
therefore carries its length; every other group carries 0.

`span` is simply the longest block in the group (1184 unless the group holds
only the short final chunk), because XOR parity is element-wise: it is always
one 1184-byte block, never `group_len` blocks.
"""

from protocol import (  # noqa: E402
    VIDEO_CHUNK_PAYLOAD,
    VIDEO_FEC_GROUP_MAX,
    VIDEO_FLAG_FEC_PARITY,
    VIDEO_HDR_LEN,
    VIDEO_HDR_STRUCT,
    VIDEO_MAGIC,
    VIDEO_MAX_CHUNKS,
    VIDEO_VERSION,
    chunk_count,
    iter_chunks,
    parse_chunk,
)

FEC_START_BITS = 11                     # 0..2047 chunk indices
FEC_LEN_BITS = 4                        # 1..16 data chunks per group
assert FEC_START_BITS + FEC_LEN_BITS == 15
assert (1 << FEC_START_BITS) == VIDEO_MAX_CHUNKS

# chunk_cnt value that marks a parity datagram. Anything > VIDEO_MAX_CHUNKS is
# already rejected by pre-Step-6 receivers, so this is invisible to them.
FEC_CCNT = VIDEO_MAX_CHUNKS + 1         # 2049
FEC_CCNT_MAX = FEC_CCNT + VIDEO_CHUNK_PAYLOAD


# --- group field: start index + length packed into chunk_idx ---------------

def encode_group_field(start, length):
    """Pack ``(start, length)`` into the 16-bit ``chunk_idx`` field."""
    if not 1 <= length <= VIDEO_FEC_GROUP_MAX:
        raise ValueError(f"fec group length must be 1..{VIDEO_FEC_GROUP_MAX}")
    if not 0 <= start < VIDEO_MAX_CHUNKS:
        raise ValueError(f"fec group start must be 0..{VIDEO_MAX_CHUNKS - 1}")
    if start + length > VIDEO_MAX_CHUNKS:
        raise ValueError("fec group runs past the chunk limit")
    return (start & ((1 << FEC_START_BITS) - 1)) | ((length - 1) << FEC_START_BITS)


def decode_group_field(value):
    """Inverse of :func:`encode_group_field`; None if the field is malformed."""
    if not 0 <= value < (1 << 16):
        return None
    start = value & ((1 << FEC_START_BITS) - 1)
    length = ((value >> FEC_START_BITS) & ((1 << FEC_LEN_BITS) - 1)) + 1
    if start + length > VIDEO_MAX_CHUNKS:
        return None
    return start, length


# --- encoding -------------------------------------------------------------

def xor_blocks(blocks):
    """XOR zero-padded 1184-byte blocks.

    Returns ``(parity, span)``: one block of element-wise XOR, and ``span`` =
    the longest input, i.e. how many bytes are meaningful. Only the final chunk
    of a frame may be shorter than 1184; the padding is implicit zeros, which
    XOR to nothing.
    """
    if not blocks:
        raise ValueError("xor_blocks needs at least one block")
    span = 0
    for i, b in enumerate(blocks):
        if len(b) > VIDEO_CHUNK_PAYLOAD:
            raise ValueError(f"block {i} is larger than {VIDEO_CHUNK_PAYLOAD}")
        span = max(span, len(b))
    parity = bytearray(VIDEO_CHUNK_PAYLOAD)
    for b in blocks:
        for j, byte in enumerate(b):
            parity[j] ^= byte
    return bytes(parity[:span]), span


def parity_datagram(frame_id, start, blocks, last_len=0):
    """Build the RP4V parity datagram for ``blocks`` (the group's payloads).

    ``last_len`` is the true length of the frame's final chunk when this group
    contains it, else 0.
    """
    if not 0 <= last_len <= VIDEO_CHUNK_PAYLOAD:
        raise ValueError("last_len out of range")
    parity, span = xor_blocks(blocks)
    hdr = VIDEO_HDR_STRUCT.pack(
        VIDEO_MAGIC, VIDEO_VERSION, VIDEO_FLAG_FEC_PARITY,
        encode_group_field(start, len(blocks)), FEC_CCNT + last_len,
        frame_id & 0xFFFFFFFF, span)
    return hdr + parity


# --- decoding -------------------------------------------------------------

class ParityPacket:
    """A parsed parity datagram."""

    __slots__ = ("frame_id", "group_start", "group_len", "span", "last_len",
                 "parity")

    def __init__(self, frame_id, group_start, group_len, span, last_len, parity):
        self.frame_id = frame_id
        self.group_start = group_start
        self.group_len = group_len
        self.span = span
        self.last_len = last_len
        self.parity = parity

    def __repr__(self):
        return (f"ParityPacket(frame={self.frame_id}, start={self.group_start}, "
                f"len={self.group_len}, span={self.span}, "
                f"last_len={self.last_len})")


def parse_parity(data):
    """Parse an RP4V parity datagram, or return None (never raises)."""
    if len(data) < VIDEO_HDR_LEN:
        return None
    magic, ver, flags, cidx, ccnt, fid, plen = VIDEO_HDR_STRUCT.unpack_from(data)
    if magic != VIDEO_MAGIC or ver != VIDEO_VERSION:
        return None
    if not flags & VIDEO_FLAG_FEC_PARITY:
        return None
    if not FEC_CCNT <= ccnt <= FEC_CCNT_MAX:
        return None
    if plen == 0 or plen > VIDEO_CHUNK_PAYLOAD or VIDEO_HDR_LEN + plen > len(data):
        return None
    group = decode_group_field(cidx)
    if group is None:
        return None
    start, length = group
    return ParityPacket(fid, start, length, plen, ccnt - FEC_CCNT,
                        data[VIDEO_HDR_LEN:VIDEO_HDR_LEN + plen])


def recovered_length(parity, index, chunk_cnt):
    """Length of the chunk a parity packet reconstructs at ``index``.

    Every chunk but the frame's last is a full 1184 bytes. The group that holds
    the last chunk carries its real length in ``last_len``.
    """
    if index == chunk_cnt - 1 and parity.last_len:
        return parity.last_len
    return VIDEO_CHUNK_PAYLOAD


def rebuild(parity, present):
    """Reconstruct the one missing chunk of a group.

    ``present`` maps chunk index -> payload for the group members we do have
    (no entry for the missing one). Returns ``(index, payload)`` or None when
    the group does not have exactly one hole.
    """
    missing = [i for i in range(parity.group_start,
                                 parity.group_start + parity.group_len)
               if i not in present]
    if len(missing) != 1:
        return None
    index = missing[0]
    buf = bytearray(VIDEO_CHUNK_PAYLOAD)
    buf[:len(parity.parity)] = parity.parity
    for i in present:
        payload = present[i]
        for j, byte in enumerate(payload):
            buf[j] ^= byte
    return index, bytes(buf)


# --- sender helper --------------------------------------------------------

def fec_datagram_count(n_data, group):
    """Datagrams a frame needs, including parity (0 group = FEC off)."""
    if group <= 0 or n_data <= 0:
        return n_data
    return n_data + (n_data + group - 1) // group


def iter_fec_datagrams(frame_id, payload, flags=0, group=8):
    """Yield the frame's data chunks with a parity datagram after each group."""
    if group <= 0:
        yield from iter_chunks(frame_id, payload, flags)
        return
    n = chunk_count(len(payload))
    last_len = len(payload) - (n - 1) * VIDEO_CHUNK_PAYLOAD if n else 0
    blocks = []
    start = 0
    for i, dgram in enumerate(iter_chunks(frame_id, payload, flags)):
        yield dgram
        blocks.append(dgram[VIDEO_HDR_LEN:])
        if len(blocks) == group or i == n - 1:
            ends_frame = i == n - 1
            yield parity_datagram(frame_id, start, blocks,
                                  last_len if ends_frame else 0)
            start += len(blocks)
            blocks = []


# --- reference receiver ---------------------------------------------------

class FecReceiver:
    """Reference PC implementation of the PS4's reassembly + recovery.

    Mirrors ``handle_datagram``/``fec_try_recover`` in ``ps4/src/video.c`` so
    the C logic has an executable spec. Feed it the datagrams of one frame:

        r = FecReceiver(frame_id, chunk_cnt)
        for dgram in datagrams:
            r.feed(dgram)
        if not r.complete:
            r.recover()
        r.frame   # bytes or None
    """

    def __init__(self, frame_id, chunk_cnt):
        self.frame_id = frame_id
        self.chunk_cnt = chunk_cnt
        self.data = [None] * chunk_cnt
        self.parity = []
        self.recovered = 0
        self.rejected = 0

    @property
    def complete(self):
        return all(d is not None for d in self.data)

    def received(self):
        return sum(1 for d in self.data if d is not None)

    def feed(self, dgram):
        """Accept one datagram; returns True when the frame is complete."""
        chunk = parse_chunk(dgram)
        if chunk is not None:
            hdr, payload = chunk
            if (hdr.frame_id != self.frame_id
                    or hdr.chunk_cnt != self.chunk_cnt
                    or hdr.chunk_idx >= self.chunk_cnt):
                self.rejected += 1
                return self.complete
            if self.data[hdr.chunk_idx] is None:
                self.data[hdr.chunk_idx] = payload
            return self.complete

        p = parse_parity(dgram)
        if p is not None and p.frame_id == self.frame_id:
            self.parity.append(p)
        else:
            self.rejected += 1
        return self.complete

    def recover(self):
        """Rebuild every group with exactly one hole. Returns chunks fixed."""
        fixed = 0
        for p in self.parity:
            end = p.group_start + p.group_len
            if end > self.chunk_cnt:
                continue
            present = {i: self.data[i] for i in range(p.group_start, end)
                       if self.data[i] is not None}
            rebuilt = rebuild(p, present)
            if rebuilt is None:
                continue
            index, payload = rebuilt
            length = recovered_length(p, index, self.chunk_cnt)
            if length <= 0 or length > len(payload):
                continue
            self.data[index] = payload[:length]
            fixed += 1
        self.recovered += fixed
        return fixed

    @property
    def frame(self):
        """The reassembled payload, or None if the frame is still incomplete."""
        if not self.complete:
            return None
        return b"".join(self.data)
