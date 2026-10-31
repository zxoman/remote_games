# Reverse Remote Play — Protocol Specification

All multi-byte fields are **little-endian**. All packets are sent over UDP.
Default ports (configurable):

| Purpose            | Default port |
|--------------------|--------------|
| Input (PS4 → PC)   | 47001        |
| Feedback (PS4 → PC) | 47002       |
| Stats (PS4 → PC)   | 47002 (legacy `RP4S`, unused) |
| Log (PS4 → PC)     | 47003        |
| Video (PC → PS4)   | 9025        |
| Video control      | 47011 (future) |

---

## 1. Input packet (`RP4I`) — Phase 1

Fixed size: **18 bytes**. Sent by PS4 at ~250 Hz to the PC's input port.

| Offset | Size | Field     | Notes |
|--------|------|-----------|-------|
| 0      | 4    | magic     | ASCII `RP4I` (`0x52 0x50 0x34 0x49`) |
| 4      | 1    | version   | `0x01` |
| 5      | 1    | seq       | u8, wraps at 255; receiver may drop counting |
| 6      | 2    | buttons   | u16 bitmask, see below |
| 8      | 1    | l2        | analog trigger, 0–255 |
| 9      | 1    | r2        | analog trigger, 0–255 |
| 10     | 2    | lx        | i16, −32768…32767, left = negative |
| 12     | 2    | ly        | i16, −32768…32767, up = negative (SDL convention) |
| 14     | 2    | rx        | i16 |
| 16     | 2    | ry        | i16 |

### Button bitmask (`buttons`, u16)

| Bit | Mask   | Button |
|-----|--------|--------|
| 0   | 0x0001 | Square |
| 1   | 0x0002 | Cross |
| 2   | 0x0004 | Circle |
| 3   | 0x0008 | Triangle |
| 4   | 0x0010 | L1 |
| 5   | 0x0020 | R1 |
| 6   | 0x0040 | L2 (digital click) |
| 7   | 0x0080 | R2 (digital click) |
| 8   | 0x0100 | Share |
| 9   | 0x0200 | Options |
| 10  | 0x0400 | L3 |
| 11  | 0x0800 | R3 |
| 12  | 0x1000 | D-pad Up |
| 13  | 0x2000 | D-pad Down |
| 14  | 0x4000 | D-pad Left |
| 15  | 0x8000 | D-pad Right |

**Design note:** D-pad is a bitmask, not a hat value. The PC side converts
mutually-exclusive bits to XInput hat values (conflicting bits resolved as
Up > Down > Left > Right priority order; normally the pad reports at most one
direction at a time on the DS4's digital d-pad).

**SHARE note:** OpenOrbis v0.5.4's `OrbisPadButton` enum does not name a SHARE
bit (its lowest defined bit is `0x0002` = L3). Bit 8 is therefore reserved in
this protocol and currently never set by the PS4 side. On a real DS4 the
unnamed `0x0001` bit is SHARE; once verified on hardware it can be mapped.

### PC → Xbox 360 mapping

| DS4                | X360                    |
|--------------------|-------------------------|
| Square             | X (A)                   |
| Cross              | A (B)                   |
| Circle             | B (Y)                   |
| Triangle           | Y (X)                   |
| L1 / R1            | LB / RB                 |
| L2 / R2 analog     | LT / RT (triggers)      |
| L3 / R3            | LS / RS click           |
| Share              | Back                    |
| Options            | Start                   |
| D-pad              | D-pad                   |
| Sticks             | Left/Right stick        |

*Note: face-button names above use Sony naming first; the "X/A/B/Y" column is
the X360 position names (bottom/right/left/top). See `pc/vigem_client.py` for
the canonical mapping actually used.*

---

## 2. Stats packet (`RP4S`) — legacy draft, not used

> **Superseded by §7.** `RP4S` was drafted in Phase 1 but nothing ever sent or
> received it. It is kept here only so the format is not reused by accident.
> The live feedback channel uses the `RP4F` magic instead.

Fixed size: **16 bytes**. Sent by PS4 → PC once per second.

| Offset | Size | Field        | Notes |
|--------|------|--------------|-------|
| 0      | 4    | magic        | ASCII `RP4S` |
| 4      | 1    | version      | `0x01` |
| 5      | 1    | reserved     | 0 |
| 6      | 2    | loss_permil  | u16, packet loss in per-mille (0–1000) |
| 8      | 2    | jitter_us    | u16, inter-arrival jitter in µs (clamped) |
| 10     | 2    | rtt_ms       | u16, round-trip estimate (reserved, 0 until implemented) |
| 12     | 4    | last_frame_seq | u32, last video frame id rendered |

The PC adjusts encoder bitrate/framerate/resolution based on these (Phase 4).

---

## 3. Video (Phase 2 MJPEG / Phase 3 H.264 over UDP)

Chunked datagrams, PC → PS4 port 9025.

| Offset | Size | Field     | Notes |
|--------|------|-----------|-------|
| 0      | 4    | magic     | ASCII `RP4V` |
| 4      | 1    | version   | `0x01` |
| 5      | 1    | flags     | bit0 = keyframe, bit1 = H.264 (see below) |
| 6      | 2    | chunk_idx | u16 |
| 8      | 2    | chunk_cnt | u16, total chunks in this frame |
| 10     | 4    | frame_id  | u32, increments per frame |
| 14     | 2    | payload_len | u16, bytes of encoded data in this datagram |
| 16     | N    | payload   | encoded frame fragment |

`flags`:

| Bit | Name                 | Meaning |
|-----|----------------------|---------|
| 0   | `VIDEO_FLAG_KEYFRAME`| MJPEG: always set. H.264: the access unit contains an IDR slice and is a safe resync point. |
| 1   | `VIDEO_FLAG_H264`    | Payload is H.264 (Annex-B access unit), not JPEG. |

One "frame" on the wire is one JPEG image (Phase 2) or one H.264 **access unit**
(Phase 3). H.264 requires the receiver to hold decoder state across frames and to
resynchronise on the next IDR after any gap; JPEG frames are independent.


Max datagram size 1200 bytes (safe under typical MTU 1500 − UDP/IP headers).

**Reassembly (fixed stride):** every chunk except the last MUST carry exactly
`1184` payload bytes (`1200 − 16`). The receiver then places chunk *N* at
offset `N × 1184` in the frame buffer, and the frame length is
`(chunk_cnt − 1) × 1184 + payload_len(last)`. Chunk order on the wire does not
matter; all chunks of a frame carry the same `chunk_cnt`.

Receiver drops the whole frame if: any chunk is missing when a newer `frame_id`
starts arriving, a chunk's `chunk_cnt` disagrees, or `chunk_idx ≥ chunk_cnt`.
Duplicate chunks (same `frame_id`, same `chunk_idx`) are ignored. The PC sender
is implemented in `pc/send_mjpeg.py` (capture via `grab_wayland.py` /
`grab_h264.py`); the PS4 receiver in `ps4/src/video.c` (reassembly + codec
dispatch) with `ps4/src/jpeg_dec.c` (libjpeg-turbo) and `ps4/src/h264_dec.c`
(FFmpeg software decoder + libswscale).

After a dropped H.264 frame the reference chain is broken, so `video.c` ignores
further P-frames until the next `VIDEO_FLAG_KEYFRAME` access unit (IDR) arrives.

> Note: the PS4 also ships a hardware JPEG decoder (`libSceJpegDec`, header
> `orbis/JpegDec.h`), but its API is only partially reverse-engineered and
> untested here. `VideoDecoder` is kept as a thin interface so it can be tried
> later.

---

## 4. Video (Phase 3 — H.264 over the same UDP framing)

H.264 reuses the §3 `RP4V` framing (the payload is an access unit instead of a
JPEG). Rationale: MJPEG needs far too much bandwidth for good 720p30 quality
over Wi-Fi, while H.264 reaches the same quality in a fraction of the bitrate.

* **PC encode:** GStreamer `openh264enc` (`pc/grab_h264.py`), no B-frames,
  `rate-control=bitrate`, default `constrained-baseline` for maximum
  compatibility. The default target is **2.5 Mbps** with a **~2 s GOP**
  (`h264_gop = 60` at 30 fps) as a safety net, plus an **on-demand IDR**
  whenever the PS4 asks (§7.3). `send_mjpeg.py` tags each access unit with
  `VIDEO_FLAG_H264`, plus `VIDEO_FLAG_KEYFRAME` when the unit contains an IDR
  slice. `h264_intra_refresh = true` requests intra-refresh, but `openh264enc`
  has no such property and the helper logs a clear fallback to periodic IDRs.
* **Transport:** same chunked UDP framing as §3. Losing any chunk loses the
  whole access unit; the receiver asks for an immediate IDR and also gets a
  scheduled one every ~2 s at worst.
* **PS4 decode:** FFmpeg software H.264 decoder + libswscale (YUV → RGB24),
  built for the PS4 by `install/build-ffmpeg-ps4.sh` and wrapped by
  `ps4/src/h264_dec.c`. Output is RGB24, identical to the JPEG path, so
  presentation (graphics.cpp `BlitRGB`) is unchanged.

Still planned: hardware `libSceVideodec2` decode and adaptive bitrate (Phase 4).

---

## 5. Versioning

Receivers must ignore packets whose `magic` doesn't match, and should log
(and drop) packets with unexpected `version` rather than crash.

---

## 6. Debug log (Phase 2, PS4 → PC)

Not a binary protocol: each UDP datagram on port 47003 is a single UTF-8 text
line, `"[<ms> ms] <message>\n"`, where `<ms>` is `sceKernelGetProcessTime()/1000`
(milliseconds since app start). The same line is written to klog via
`sceKernelDebugOutText`. Emitted **event-driven only** (startup, pad
connect/disconnect, first frame, ~1 Hz stats) from a dedicated non-blocking
socket so it never perturbs input or video. See `ps4/src/dlog.c` and
`pc/log_viewer.py`.

---

## 7. Feedback channel (`RP4F`) — Step 2

PS4 → PC, default port **47002** (`pc/config.ini` `[video] feedback_port`,
`ps4/include/config.h` `CFG_FEEDBACK_PORT`). Additive and independent of the
video (`RP4V`) and input (`RP4I`) streams: a PC that does not recognise `RP4F`
simply ignores these datagrams, and the PS4 runs fine when the PC is not
listening.

### 7.1 Common header

Every feedback packet begins with the same **8-byte** header.

| Offset | Size | Field   | Notes |
|--------|------|---------|-------|
| 0      | 4    | magic   | ASCII `RP4F` (`0x52 0x50 0x34 0x46`) |
| 4      | 1    | version | `0x01` |
| 5      | 1    | type    | `0x01` STATS, `0x02` KEYFRAME_REQUEST |
| 6      | 2    | seq     | u16, wraps at 65535; per-sender counter |

Receivers validate `magic`, `version` **and** `type`, then require the exact
packet length; anything else is silently ignored (malformed packets must never
raise or stall the sender).

### 7.2 STATS (`type = 0x01`) — 32 bytes, ~1 Hz

| Offset | Size | Field             | Notes |
|--------|------|-------------------|-------|
| 0      | 8    | header            | see 7.1 |
| 8      | 4    | packets_expected  | u32, sum of `chunk_cnt` over every frame the receiver started |
| 12     | 4    | packets_received  | u32, unique video chunks accepted |
| 16     | 4    | frames_completed  | u32, frames decoded and presented |
| 20     | 4    | frames_dropped    | u32, incomplete / stale / decode-failure frames |
| 24     | 2    | jitter_ms         | u16, rough inter-arrival jitter (ms) |
| 26     | 4    | last_frame_id     | u32, newest complete video `frame_id` |
| 30     | 2    | decoder_queue     | u16, frames waiting to decode (0 while decoding is synchronous) |

Packet loss is derived as
`loss = (packets_expected − packets_received) / packets_expected`; the counter
is cumulative since the PS4 receiver started, so the PC can compute a rate by
differencing consecutive samples. These are the inputs the adaptive controller
(Step 4) consumes.

### 7.3 KEYFRAME_REQUEST (`type = 0x02`) — 14 bytes

| Offset | Size | Field         | Notes |
|--------|------|---------------|-------|
| 0      | 8    | header        | see 7.1 |
| 8      | 4    | last_frame_id | u32, last video `frame_id` known when the loss was detected |
| 12     | 1    | reason        | `0` incomplete frame, `1` decode error, `2` receiver startup |
| 13     | 1    | reserved      | 0 |

The PS4 sends this immediately when an H.264 access unit is abandoned
incomplete, when reassembly succeeds but the decoder fails, or when the receiver
just started. It is **hard rate-limited to 2 per second** on the PS4 side so a
burst of losses cannot trigger a keyframe storm.

On the PC, `pc/send_mjpeg.py` polls the listener **once per frame** (not at the
1 Hz log boundary) and forwards each request to `grab_h264.py` by writing
`keyframe\n` to its stdin. The helper turns that into an upstream
`GstForceKeyUnit` event, so `openh264enc` emits an IDR within a frame or two.
The sender applies its **own 0.5 s cap** as well, so a buggy or hostile sender
cannot trigger a keyframe storm in either direction. Set
`h264_keyframe_on_demand = false` to disable the channel (the ~2 s scheduled
GOP then remains the only recovery point).

MJPEG frames are independent, so the PS4 only sends keyframe requests for
H.264 streams.

### 7.4 PC listener

`pc/feedback.py` (`FeedbackListener`) owns one daemon thread and exposes
`latest()` / `take_keyframe_request()` / `counters()` to `pc/send_mjpeg.py`.
The listener starts even under `--no-send`, which allows a full PC-only test
with `pc/tools/fake_ps4_stats.py`:

```bash
# terminal 1: sender listening for feedback, no video transmission needed
python3 send_mjpeg.py --test-pattern --no-send --feedback-port 47002

# terminal 2: fake PS4 with 4% loss, one KEYFRAME_REQUEST every 2 s
python3 tools/fake_ps4_stats.py --loss 4 --kf-every 2
```

Expected: the sender's 1 Hz line includes `fb_loss=4.0% fb_jit=..ms
fb_decq=..` and `kfreq=N` when requests were forwarded to the encoder; the
final summary reports the STATS / KEYFRAME_REQUEST / forwarded counts. Because
the fake tool asks every 2 s and the sender caps at 2/s, exactly one IDR per
request should be produced. On the PS4 side the channel is opened in
`video_start()` (`CFG_PC_IP:CFG_FEEDBACK_PORT`) and closed in `video_stop()`;
`send_stats()` runs at 1 Hz from the receive thread, and `request_keyframe()`
is called on incomplete frames, decode failures and startup.

### 7.5 ABR (Step 4) — PC-local, no wire change

The adaptive bitrate controller adds **no packets and changes no header**. The
PS4 keeps sending 7.2 STATS once a second; `pc/abr.py` differences the cumulative
counters to get the loss over that second and decides a new target. Two things
happen on the PC only:

* bitrate: `send_mjpeg.py` writes `bitrate <bps>\n` to `grab_h264.py`'s stdin,
  which applies `enc.set_property("bitrate", ...)` live — no restart, no
  renegotiation, no packet the PS4 can see;
* fps/resolution: the capture helper is restarted (at most once per 5 s) at the
  new geometry with the ABR's current bitrate. `frame_id` continues increasing,
  so the PS4 treats it as the same stream; the first access unit is an IDR.

Because the video framing is unchanged, an old PS4 build keeps working with a
new PC build and vice versa. The only observable difference is that the encoder
may change resolution mid-session. `ps4/src/video.c` presents whatever
`g_fw x g_fh` size the decoder returns, and the RGB buffers are allocated for
the largest configured geometry, so a downscaled frame fits; FFmpeg is expected
to pick up the in-band SPS and rebuild the swscale context, but this needs
confirmation on hardware (see the `TODO(verify-on-PS4)` in `video.c`).

### 7.6 Packet pacing (Step 5) — PC-local, no wire change

Pacing adds **no packets and changes no header**. The RP4V header in §3/§4 is
byte-for-byte the same, and chunk indices/counts are unchanged, so an old PS4
build and a new PC build remain interchangeable.

Only the *timing* of the datagrams changes. With `pacing_ratio = r` (default
`0.6`, `0` disables), one frame of `n` datagrams is sent over

```
budget = r / fps            # 20 ms at 30 fps with r = 0.6
gap    = budget / n         # per-datagram slot
```

and datagram `i` is handed to the kernel no earlier than
`frame_start + i * gap`. The receiver never has to wait for the frame any
longer than `budget` (well inside one frame interval), so buffering on the PS4
is unaffected: `video.c` already accumulates chunks until `chunk_cnt` of one
`frame_id` arrive.

Guarantees:

* **No catch-up bursts.** If the clock is already past a slot (slow encode,
  scheduler hiccup) the datagram is sent immediately and the schedule is
  re-anchored to `now + gap`; the frame is then counted once in `late_frames`.
  Pacing cannot add latency, only spread a burst.
* **Batching, not per-packet syscalls.** Slots closer than ~0.5 ms are merged
  (a ~250-chunk keyframe sleeps once every few datagrams) while the total time
  per frame still equals `budget`.
* **Non-blocking socket.** A full `SO_SNDBUF` still abandons the rest of the
  access unit (`frames dropped on a full buffer`), exactly as before pacing;
  `wait()` only decides *when* `sendto()` is called.
* **fps changes** (ABR restart) immediately update the budget.

Measured on loopback (720p30, ~14.7 KB access units, `--test-pattern`):

| `pacing_ratio` | median per-frame spread | median inter-packet gap | peak vs average |
|----------------|------------------------|------------------------|-----------------|
| `0` (off)      | 0.13 ms                | 0.007 ms               | ~1 frame       |
| `0.6`          | 18.4 ms                | 1.53 ms                | ~1.7×          |

Nothing in this section is PS4-specific; it is implemented once in
`pc/pacing.py` and driven by the shared send loop, so it also paces MJPEG.
