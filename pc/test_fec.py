"""Tests for Step 6 XOR FEC (pc/fec.py).

Two levels:

* the encoding primitives (group field, parity maths, datagram parsing);
* ``FecReceiver``, the executable spec for ``ps4/src/video.c``: build a frame,
  throw datagrams away, and check that exactly the recoverable losses are
  recovered.

No sockets, no PS4, no GStreamer.
"""

import random

import pytest

from fec import (
    FecReceiver,
    decode_group_field,
    encode_group_field,
    fec_datagram_count,
    iter_fec_datagrams,
    parity_datagram,
    parse_parity,
    rebuild,
    recovered_length,
    xor_blocks,
)
from protocol import (
    VIDEO_CHUNK_PAYLOAD,
    VIDEO_MAX_CHUNKS,
    VIDEO_FEC_GROUP_MAX,
    VIDEO_FLAG_FEC_PARITY,
    VIDEO_FLAG_H264,
    VIDEO_FLAG_KEYFRAME,
    VIDEO_HDR_STRUCT,
    VIDEO_MAX_DGRAM,
    chunk_count,
    iter_chunks,
    parse_chunk,
)


def make_frame(nchunks, seed=1):
    """A payload of ``nchunks`` chunks with a deliberately short last chunk."""
    rnd = random.Random(seed)
    body = bytes(rnd.randrange(256) for _ in range((nchunks - 1) * VIDEO_CHUNK_PAYLOAD))
    return body + bytes(rnd.randrange(256) for _ in range(100))


def frame_datagrams(payload, frame_id=7, group=8, flags=VIDEO_FLAG_H264):
    return list(iter_fec_datagrams(frame_id, payload, flags, group))


# --- group field ----------------------------------------------------------

@pytest.mark.parametrize("start,length", [
    (0, 1), (0, 8), (8, 8), (16, 4), (2047, 1), (2040, 8), (100, 16),
])
def test_group_field_round_trip(start, length):
    assert decode_group_field(encode_group_field(start, length)) == (start, length)


def test_group_field_never_sets_the_top_bit():
    # Any value with bit 15 set would be nonsense; keep the encoding in 15 bits
    # so a future flag can use bit 15 without another format change.
    for start in (0, 1, 1024, 2032):
        for length in (1, 8, 16):
            assert encode_group_field(start, length) < 0x8000


@pytest.mark.parametrize("start,length", [
    (0, 0), (0, VIDEO_FEC_GROUP_MAX + 1), (-1, 4), (2048, 1), (2040, 16),
])
def test_group_field_rejects_impossible_groups(start, length):
    with pytest.raises(ValueError):
        encode_group_field(start, length)


def test_group_decode_rejects_overflow_past_the_chunk_limit():
    # start 2044 + length 16 would need 2060 chunks.
    assert decode_group_field(encode_group_field(2044, 4)) == (2044, 4)
    bad = (2044 & 0x7FF) | ((16 - 1) << 11)
    assert decode_group_field(bad) is None


# --- parity maths ---------------------------------------------------------

def test_parity_xors_every_block_and_reports_the_span():
    blocks = [bytes([1]) * VIDEO_CHUNK_PAYLOAD,
              bytes([2]) * VIDEO_CHUNK_PAYLOAD,
              bytes([4]) * 10]
    parity, span = xor_blocks(blocks)
    # XOR parity is element-wise: one 1184-byte block, not three.
    assert span == VIDEO_CHUNK_PAYLOAD
    assert len(parity) == VIDEO_CHUNK_PAYLOAD
    assert parity[0] == 1 ^ 2 ^ 4
    assert parity[9] == 1 ^ 2 ^ 4
    assert parity[10] == 1 ^ 2                          # the 10-byte block ends
    assert parity[VIDEO_CHUNK_PAYLOAD - 1] == 1 ^ 2


def test_rebuild_recovers_any_single_member():
    blocks = [bytes([i * 37 & 0xFF]) * VIDEO_CHUNK_PAYLOAD for i in range(1, 8)]
    blocks.append(bytes(50))
    parity, span = xor_blocks(blocks)
    p = parse_parity(parity_datagram(3, 0, blocks))
    assert p is not None and p.span == span
    for missing in range(len(blocks)):
        present = {i: b for i, b in enumerate(blocks) if i != missing}
        index, payload = rebuild(p, present)
        assert index == missing
        assert payload[:len(blocks[missing])] == blocks[missing]


def test_rebuild_refuses_two_holes():
    p = parse_parity(parity_datagram(3, 0, [b"x" * 100, b"x" * 100, b"x" * 100]))
    assert rebuild(p, {}) is None                       # three holes
    assert rebuild(p, {0: b"x" * 100}) is None           # two holes
    assert rebuild(p, {0: b"x" * 100, 1: b"x" * 100}) is not None


def test_rebuild_refuses_a_full_group():
    p = parse_parity(parity_datagram(3, 0, [b"x" * 100, b"y" * 100]))
    assert rebuild(p, {0: b"x" * 100, 1: b"y" * 100}) is None


# --- datagram format ------------------------------------------------------

def test_parity_datagram_is_a_valid_but_empty_clip():
    payload = make_frame(3)
    dgrams = frame_datagrams(payload, group=3)
    parity = [d for d in dgrams if parse_parity(d) is not None]
    assert len(parity) == 1
    assert len(parity[0]) <= VIDEO_MAX_DGRAM
    hdr = VIDEO_HDR_STRUCT.unpack_from(parity[0])
    assert hdr[2] & VIDEO_FLAG_FEC_PARITY
    assert hdr[4] > VIDEO_MAX_CHUNKS       # chunk_cnt: never a data chunk
    # A pre-Step-6 receiver rejects it, because it checks chunk_cnt.
    assert parse_chunk(parity[0]) is None


@pytest.mark.parametrize("group", [1, 2, 4, 8, 16])
def test_datagram_count_matches_the_generator(group):
    payload = make_frame(26)
    n = chunk_count(len(payload))
    assert len(frame_datagrams(payload, group=group)) == fec_datagram_count(n, group)


def test_fec_off_yields_exactly_the_plain_chunks():
    payload = make_frame(5)
    plain = list(iter_chunks(7, payload, VIDEO_FLAG_H264))
    assert frame_datagrams(payload, group=0) == plain


def test_parity_is_interleaved_after_its_group():
    payload = make_frame(5)
    dgrams = frame_datagrams(payload, group=2)
    kinds = ["p" if parse_parity(d) else "d" for d in dgrams]
    assert "".join(kinds) == "ddpddpdp"     # 2+1, 2+1, then the short tail


@pytest.mark.parametrize("mutate", [
    lambda d: b"XXXX" + d[4:],                        # bad magic
    lambda d: d[:4] + bytes([99]) + d[5:],            # bad version
    lambda d: d[:-1],                                 # truncated payload
    lambda d: d[:8] + (100).to_bytes(2, "little") + d[10:],   # ccnt not parity
    lambda d: d[:5] + b"\x00" + d[6:],                # flags: not FEC
])
def test_parse_parity_rejects_malformed_datagrams(mutate):
    good = frame_datagrams(make_frame(2), group=2)[-1]
    assert parse_parity(good) is not None
    assert parse_parity(mutate(good)) is None


def test_parse_parity_rejects_a_group_past_the_chunk_limit():
    good = bytearray(frame_datagrams(make_frame(2), group=2)[-1])
    # start 2044, length 16 -> 2060 > 2048
    good[6:8] = ((2044 & 0x7FF) | (15 << 11)).to_bytes(2, "little")
    assert parse_parity(bytes(good)) is None


# --- reference receiver ---------------------------------------------------

def test_receiver_rebuilds_a_frame_with_one_loss_per_group():
    payload = make_frame(26)
    dgrams = frame_datagrams(payload, group=8)
    # Drop the first, the 9th and the last datagram (one per group at most:
    # groups are 0-7, 8-15, 16-23, 24-25 -> drop 0, 9, 24, and a data chunk
    # from the last group's tail would be two in the same group).
    drop = {0, 9, 24}
    r = FecReceiver(7, 26)
    for i, d in enumerate(dgrams):
        if i in drop:
            continue
        r.feed(d)
    assert not r.complete                    # 3 chunks missing
    assert r.recover() == 3
    assert r.complete
    assert r.frame == payload


def test_receiver_cannot_fix_two_losses_in_one_group():
    payload = make_frame(26)
    dgrams = frame_datagrams(payload, group=8)
    drop = {1, 3}                            # same group
    r = FecReceiver(7, 26)
    for i, d in enumerate(dgrams):
        if i in drop:
            continue
        r.feed(d)
    assert r.recover() == 0
    assert not r.complete
    assert r.frame is None


def test_receiver_recovers_a_lost_short_last_chunk_with_its_true_length():
    payload = make_frame(9)                  # 8 full chunks + 100 bytes
    assert len(payload) == 8 * VIDEO_CHUNK_PAYLOAD + 100
    dgrams = frame_datagrams(payload, group=4)
    # Datagrams: 4 data + parity, 4 data + parity, 1 data + parity.
    last_data = [i for i, d in enumerate(dgrams) if parse_chunk(d)][-1]
    r = FecReceiver(7, 9)
    for i, d in enumerate(dgrams):
        if i != last_data:
            r.feed(d)
    assert r.recover() == 1
    assert r.complete
    assert len(r.data[-1]) == 100
    assert r.frame == payload


def test_receiver_length_rule():
    payload = make_frame(5)
    parity = [d for d in frame_datagrams(payload, group=4) if parse_parity(d)]
    p_first = parse_parity(parity[0])
    p_last = parse_parity(parity[1])
    assert recovered_length(p_first, 0, 5) == VIDEO_CHUNK_PAYLOAD
    assert recovered_length(p_last, 4, 5) == len(payload) - 4 * VIDEO_CHUNK_PAYLOAD


def test_receiver_ignores_datagrams_from_another_frame():
    payload = make_frame(4)
    dgrams = frame_datagrams(payload, group=2)
    r = FecReceiver(7, 4)
    foreign = list(iter_fec_datagrams(99, payload, VIDEO_FLAG_H264, 2))
    for d in foreign:
        r.feed(d)
    assert r.received() == 0
    assert r.parity == []
    assert r.rejected == len(foreign)


def test_receiver_group_of_one_recovers_every_single_loss():
    payload = make_frame(6)
    dgrams = frame_datagrams(payload, group=1)
    for lost in range(len(dgrams) // 2):
        r = FecReceiver(7, 6)
        for i, d in enumerate(dgrams):
            if i == lost * 2:                # a data datagram, not a parity one
                continue
            r.feed(d)
        assert r.recover() == 1, lost
        assert r.frame == payload, lost


def test_payload_ending_in_zero_bytes_keeps_its_exact_length():
    body = bytes((i * 7) & 0xFF for i in range(VIDEO_CHUNK_PAYLOAD))
    payload = body + b"\x00" * 64            # last chunk is all zeros
    dgrams = frame_datagrams(payload, group=4)
    last_data = [i for i, d in enumerate(dgrams) if parse_chunk(d)][-1]
    r = FecReceiver(7, 2)
    for i, d in enumerate(dgrams):
        if i != last_data:
            r.feed(d)
    assert r.recover() == 1
    assert r.data[-1] == b"\x00" * 64


def test_keyframe_flagged_datagrams_keep_their_flags():
    payload = make_frame(3)
    flags = VIDEO_FLAG_H264 | VIDEO_FLAG_KEYFRAME
    dgrams = frame_datagrams(payload, group=2, flags=flags)
    data = [parse_chunk(d) for d in dgrams if parse_parity(d) is None]
    assert all(hdr.flags == flags for hdr, _ in data)
