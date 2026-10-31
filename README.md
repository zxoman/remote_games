# Reverse Remote Play (PS4 homebrew ⇄ PC)

The PC streams its screen to a jailbroken PS4, and the PS4's DualShock 4
controls games running on the PC (virtual Xbox 360 pad on the PC).

```
 ┌──────────────┐   video (UDP 9025)           ┌──────────────┐
 │      PC      │  MJPEG: FFmpeg/jpegenc        │    PS4       │
 │  virtual 360 │  H.264: GStreamer/openh264    │ jpeg-turbo / │
 │  (Win/Linux) │                               │ FFmpeg h264  │
 │              │◀──── input RP4I @250 Hz ──────│  scePad read │
 │              │◀──── feedback RP4F 1 Hz ──────│  stats +     │
 │              │      (stats, keyframe req)    │  keyframe    │
 └──────────────┘                               └──────────────┘
```

**Status: Phase 1 (input), Phase 2 (MJPEG), Phase 3 (H.264) and Steps 1–4
(weak-network defaults, feedback channel, keyframe-on-demand, adaptive bitrate)
implemented.**
The video codec is selectable (`codec` in `pc/config.ini`): **auto** (the
default) picks H.264 when GStreamer can encode it and falls back to MJPEG
otherwise, so the same config works on Linux and on Windows without GStreamer.
H.264 gives far better quality per bit for Wi-Fi links. PC side runs on
Windows (ViGEmBus) and Linux (uinput).

## Repo layout

```
docs/protocol.md     wire formats (input, feedback, MJPEG/H.264 video)
install/             toolchain + libjpeg/FFmpeg cross-builds, one-command container build
ps4/                 OpenOrbis C/C++ app → PKG (Makefile); libjpeg-turbo + FFmpeg under third_party/
pc/                  Python receiver / senders / virtual pad
```

## PC setup (Windows or Linux)

The PC side runs on **both** Windows and Linux. Pick the backend:

| OS | Virtual pad backend | Requirement |
|----|--------------------|-------------|
| Windows | ViGEmBus (`vgamepad`) | install the ViGEmBus driver |
| Linux   | kernel `uinput` (`evdev`) | access to `/dev/uinput` |

1. Install **Python 3.10+**.
2. Install the dependencies (the right backend is chosen automatically by the
   `sys_platform` markers in `requirements.txt`):
   ```
   cd pc
   pip install -r requirements.txt
   ```
3. **Windows only:** install the ViGEmBus driver from
   <https://github.com/nefarius/ViGEmBus/releases>.
4. **Linux only:** make sure the `uinput` module is loaded and your user can
   access the device:
   ```
   sudo modprobe uinput                      # load the module now
   sudo usermod -aG input "$USER"            # then re-login
   # persistent rule so /dev/uinput stays accessible:
   echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | \
       sudo tee /etc/udev/rules.d/99-uinput.rules
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```
   (Reboot or reconnect the session after the group change.)
5. Edit `pc/config.ini` if you don't like the defaults (ports, debug flag).

**Windows shortcuts** (added from the Windows machine): `build.bat` in the repo
root and `pc\run.bat` open a small menu for installing requirements, running the
receiver, the fake PS4 input sender and the video sender. They are convenience
wrappers only — the real commands are the ones documented below.

**Codec on Windows:** `codec = auto` resolves to MJPEG when GStreamer (or the
`openh264enc` plugin) is missing, so Windows works out of the box. Force
`codec = h264` if you want a missing encoder to be a hard error. `openh264enc`
is the only H.264 encoder the PC side drives (FFmpeg here has no H.264 encoder).

## PS4 setup — building the PKG

The build is a **Makefile** flow (not CMake), modeled on the OpenOrbis samples.
Every push to `main` also builds the PKG in GitHub Actions (Actions → PS4 PKG
→ artifact `reverse-remote-play-ps4`). You can re-run it from **Actions →
PS4 PKG → Run workflow**.

There are two local ways to build:

### Option A — one command, no root (recommended, verified)

Uses podman to run the compiler (clang) and the packager in throwaway
containers, sidestepping two real gotchas on modern distros:
- OpenOrbis' `PkgTool.Core` needs **OpenSSL 1.1** (Fedora 44 only ships 3.x).
- You may not have clang/lld installed, or don't want to `sudo`.

```bash
bash install/setup-fedora.sh     # downloads toolchain to ~/openorbis (no root)
bash install/build-ps4.sh        # compiles + packages, needs podman
```
Output: `ps4/IV0000-BREW00300_00-REMOTEPLAYREV000.pkg`.

Requirements for this path: `podman` (`sudo dnf install podman`). Everything
else is fetched into containers; the toolchain install needs no root.

### Option B — build directly on the host

1. Install deps: `sudo dnf install clang lld llvm make` (Fedora) or the
   equivalent clang/lld for your distro.
2. Get the toolchain: `bash install/setup-fedora.sh`, then
   `export OO_PS4_TOOLCHAIN=$HOME/openorbis` (the script prints this).
3. `source install/ps4env.sh && cd ps4 && make`
   - Compiling and linking produce `eboot.bin`.
   - **Packaging caveat:** `PkgTool.Core pkg_build` requires `libssl1.1`, which
     is absent on Fedora 44. If `make` fails at the pkg step, either install an
     OpenSSL 1.1 compat library, or just use Option A for the final `.pkg`.

### Before building either way

- Set `CFG_PC_IP` in `ps4/include/config.h` to your PC's LAN IP, then rebuild.
- The one-command build auto-builds the static libjpeg-turbo the first time
  (`install/build-libjpeg-ps4.sh`) and the minimal FFmpeg H.264 decoder
  (`install/build-ffmpeg-ps4.sh`); no manual step needed.
- Install the resulting `.pkg` with your homebrew installer (GoldHEN pkg
  loader, pkg_loader, Itemzflow, ...) and launch it. On screen: **dark green**
  = pad open and sending, **dark red** = no pad. A white square follows the
  left stick so you can confirm the pad locally before involving the PC.
- For Phase 2 also set `ps4_host` in `pc/config.ini` to your PS4's LAN IP.

> The PS4 build was verified here end-to-end **inside containers** (clang 22 +
> lld, OpenOrbis v0.5.4): it compiles, links, and produces a 6.6 MB fake PKG.
> It has not been *run* on a console (no hardware available).

## Firewall (PC)

Allow inbound UDP **47001** (and 47002, 47003, 9025) on the PC, private
network profile.
- **Windows:** the firewall prompts on first run of `receive_input.py` —
  allow it on private networks only.
- **Linux (firewalld):** `sudo firewall-cmd --add-port=47001/udp --permanent &&
  sudo firewall-cmd --reload` (add 47002/47003/9025 as needed).

## Running Phase 1

**PC** (terminal 1):
```
cd pc
python receive_input.py            # or --debug for per-packet printout
```

**PS4** (terminal 2): launch the installed app. You should see
`rate=250 pkt/s` on the PC, and the virtual Xbox 360 pad moving in:
- Windows: "Setup USB Game Controllers" (`joy.cpl`)
- Linux: `evtest /dev/input/eventN` (pick the "Microsoft X-Box 360 pad")

**No PS4? Test the PC side alone** (terminal 2):
```
cd pc
python fake_sender.py              # simulates a DS4 on localhost
```

Another simulator, `pc/tools/fake_ps4_input.py`, sends real `RP4I` packets and
can hold buttons, move sticks or generate a circle, which is handy for checking
the virtual pad and the button mapping:

```
python tools/fake_ps4_input.py --btn cross --btn square
python tools/fake_ps4_input.py --stick-l 32767,0
python tools/fake_ps4_input.py --pattern circle
```

## Running Phase 2 (MJPEG video)

Requires **FFmpeg** on the PC (`ffmpeg` on `PATH`). The sender captures the
screen, encodes MJPEG, chunks each JPEG into ≤1200-byte datagrams and sends them
to the PS4's video port (9025). The PS4 reassembles and decodes with
libjpeg-turbo on a background thread, so input timing is unaffected.

**PC** (while `send_mjpeg.py` runs, keep `receive_input.py` running too):
```
cd pc
python send_mjpeg.py --host <PS4_IP>          # e.g. --host 192.168.1.20
```

Screen capture per OS is automatic (Linux `x11grab`, Windows `gdigrab`). Useful
flags:

```
--test-pattern          # stream FFmpeg testsrc (no display / no PS4 needed)
--no-send               # encode+parse only, verify locally without a PS4
--display :0.0          # X11 display (Linux)
--width/--height/--fps  # must match ps4/include/config.h (default 1280x720@30)
--quality 2..31         # FFmpeg -q:v (lower = better/bigger)
--duration 10           # stop after N seconds
```

> The PS4 expects **1280×720** frames (`CFG_VIDEO_WIDTH/HEIGHT` in
> `ps4/include/config.h`). Other sizes still display (nearest-neighbour scaled)
> but waste bandwidth; keep them equal.

## Running Phase 3 (H.264 video)

H.264 is what `codec = auto` picks on any machine where GStreamer can encode
it, because it gives far better quality per bit than MJPEG over Wi-Fi. The PC
encodes with GStreamer's `openh264enc` (via `grab_h264.py`); the PS4 decodes
with FFmpeg + libswscale. `ffmpeg`/`ffmpeg-free` is **not** used for H.264
encoding.

```
cd pc
python send_mjpeg.py --host <PS4_IP>              # defaults: H.264 2.5 Mbps
```

Extra flags (also config keys `h264_*`):

```
--bitrate 2500000       # target bits/s (2.5 Mbps default; bounds in [adaptive])
--gop 60                # scheduled IDR interval (60 = 2 s at 30 fps)
--profile constrained-baseline
--complexity 0          # openh264: 0 low, 1 medium, 2 high
```

Loss behaviour: if a chunk is lost the whole access unit is dropped and the
decoder ignores P-frames until the next IDR. On demand the PS4 asks for an
immediate IDR (Step 3); the ~2 s GOP is only the worst-case safety net.

## Weak-network defaults (Step 1)

The defaults in `pc/config.ini` now target a weak/unstable Wi-Fi link:

| Setting | Default | Why |
|---------|---------|-----|
| `codec` | `auto` | H.264 (10× better per bit) with a MJPEG fallback |
| `h264_bitrate` | `2500000` | small keyframe bursts; fits a marginal link |
| `width`/`height`/`fps` | `1280x720` / `30` | the PS4's expected geometry |
| `h264_gop` | `60` (2 s) | safety net; on-demand IDRs recover faster (Step 3) |
| `h264_keyframe_on_demand` | `true` | the PS4 can request an IDR immediately |
| `[adaptive] enabled` | `true` | live bitrate control from PS4 feedback (Step 4) |
| `[adaptive] min_bitrate` | `600000` | floor before fps is reduced |
| `[adaptive] max_bitrate` | `6000000` | ceiling on a healthy link |
| `[adaptive] min_fps` | `15` | lowest fps the controller may pick |

**Quality scales (MJPEG only).** `quality` is FFmpeg `-q:v` (**lower = better**,
2..31) while `wayland_quality` is jpegenc quality (**higher = better**, 0..100).
Set the optional unified `quality_pct` (**0 = worst .. 100 = best**) in
`[video]`, or pass `--quality-pct`, and the sender maps it onto both.

## Feedback channel (Step 2)

The PS4 now reports back on UDP **47002** (config: `[video] feedback_port`),
using a new `RP4F` magic independent of the video/input streams:

* **STATS** once a second: packets expected/received, frames completed/dropped,
  jitter, last complete frame id, decoder queue depth.
* **KEYFRAME_REQUEST** immediately after an incomplete or undecodable H.264
  frame, rate-limited to 2/s. Step 3 turns these into immediate IDRs.

The sender starts a listener thread automatically (disable with
`--no-feedback`) and adds these fields to its 1 Hz line:

```
[send_mjpeg] frame=123 ... pkts/s=280 err/s=0 fb_loss=1.2% fb_jit=4ms fb_decq=0
```

Test the whole channel **without a PS4** (two terminals):

```
cd pc
# terminal 1: sender, no need to transmit video
python3 send_mjpeg.py --test-pattern --no-send --feedback-port 47002

# terminal 2: pretend to be the PS4, 4% packet loss, keyframe req every 2 s
python3 tools/fake_ps4_stats.py --loss 4 --kf-every 2
```

Terminal 1 should print `fb_loss=4.0% ...` and `kfreq=N` on lines where a
request was forwarded; on exit it prints `feedback: N STATS, M KEYFRAME_REQUEST
(K forwarded to encoder) ...`. `python3 protocol.py` also round-trips both new
packets and checks that malformed packets are ignored.

## Keyframe on demand (Step 3)

A fixed GOP means a lost frame can freeze the picture until the next scheduled
keyframe. Now the PS4's `KEYFRAME_REQUEST` is acted on immediately:

```
PS4 video.c  ──RP4F KEYFRAME_REQUEST──▶  send_mjpeg.py (polls every frame)
                                               │  "keyframe\n" on stdin
                                               ▼
                    grab_h264.py  ──GstForceKeyUnit──▶  openh264enc  ──IDR──▶
```

* The sender polls the listener **once per frame**, so recovery is not delayed
  by the 1 Hz log line.
* Both ends cap requests at **2/s** (PS4 hard limit + sender `0.5 s` cap), so a
  burst of losses cannot cause a keyframe storm.
* `h264_gop = 60` (2 s at 30 fps) is now only the worst-case safety net.
* `h264_keyframe_on_demand = true` (default) enables the channel;
  set it to `false` to fall back to scheduled IDRs only.
* `h264_intra_refresh = true` asks for intra-refresh, but `openh264enc` has no
  such property, so the helper logs
  `intra-refresh: encoder has no 'intra-refresh' property; falling back to
  periodic IDR (gop=60)` and keeps going.

Verify the encoder side directly (no PS4, no sender):

```bash
cd pc
python3 grab_h264.py --test-pattern --width 640 --height 360 \
    --fps 30 --bitrate 1500000 --gop 60 --control-stdin > /dev/null
# then type "keyframe" + Enter: an extra IDR appears immediately
```

Or end-to-end with the fake PS4 (shows `kfreq=1` and a jump in `last_au` size
when the forced IDR arrives):

```bash
python3 send_mjpeg.py --test-pattern --no-send
python3 tools/fake_ps4_stats.py --loss 3 --kf-every 1
```

## Adaptive bitrate (Step 4)

`pc/abr.py` is a pure, I/O-free controller. `send_mjpeg.py` feeds it **one
sample per PS4 STATS packet** (packet loss over that second, jitter, frames
dropped/completed) and applies the decision:

```
PS4 video.c ──RP4F STATS──▶ send_mjpeg.py ──▶ abr.py ──▶ decision
                                    │                      │
                          "bitrate N" on stdin      fps/res change?
                                    ▼                      ▼
                           openh264enc live         clean pipeline restart
```

Policy (`[adaptive]` in `pc/config.ini`):

| Rule | Default | Effect |
|------|---------|--------|
| loss > `loss_threshold` | 2 % | multiply bitrate by `decrease_factor` (0.75) |
| loss < `loss_recover` | 1 % | +`increase_step` (5 %) per `increase_interval` (5 s) |
| 1–2 % loss | hysteresis | hold: neither decrease nor increase |
| `cooldown` | 2 s | minimum gap between two decreases |
| at `min_bitrate` + `escalate_after` bad samples | 3 | fps 30→20→15, then 1280x720→960x540→640x360 |
| `restore_interval` clean seconds **and** bitrate back at its start | 20 s | raise resolution, then fps |

* **Bitrate changes never restart the pipeline**: `send_mjpeg.py` writes
  `bitrate <bps>` to `grab_h264.py`'s stdin and the helper calls
  `enc.set_property("bitrate", ...)` on the GLib main loop.
* **fps/resolution changes do restart** the capture helper, at most once every
  **5 s**. The restart reuses the ABR's *current* bitrate (not the configured
  one) and the new pipeline starts with a fresh IDR. `frame_id` keeps counting,
  so the PS4 sees one continuous stream.
* Every decision is logged with its reason and timestamp:
  `[abr +12.2s] loss 9.1% > 2.0% (jitter 2ms, drop 13.3%): bitrate 791016->600000 => bitrate=600000 1280x720@30`.
* `[adaptive] enabled = false` pins everything to the `[video]` values.
* The frame-drop trigger uses a deliberately high threshold (`drop_ratio_threshold
  = 0.45`): one lost chunk ruins a whole ~26-chunk access unit, so 1 % packet
  loss already loses ~23 % of frames. A low threshold would silently override
  the 1–2 % loss hysteresis band.
* `tools/fake_ps4_stats.py` models that same relationship, so a PC-only test
  produces realistic drop ratios (e.g. `--loss 1.5` → ~33 % of frames lost).

Unit tests (no PS4, no GStreamer, no sockets):

```bash
cd pc
python3 -m pip install --user pytest   # once
python3 -m pytest -q
```

They cover multiplicative decrease, cooldown, +5 % recovery and clamping, the
hysteresis band, jitter/frame-drop triggers, the fps→resolution ladder, the
conservative restore gate, and that an alternating/near-threshold loss pattern
does not oscillate.

End-to-end on the PC with a simulated bad link:

```bash
# terminal 1
python3 send_mjpeg.py --test-pattern --no-send --duration 45
# terminal 2 - 8 % loss for 20 s, then a clean link for 20 s
python3 tools/fake_ps4_stats.py --loss 8 --seconds 20
python3 tools/fake_ps4_stats.py --loss 0 --seconds 20
```

Expected: the bitrate steps down `2500000 -> 1875000 -> 1406250 -> 1054688 ->
791016 -> 600000`, then fps/resolution drop only if loss continues, and once
the link is clean it climbs back by 5 % every 5 s.

## Viewing the PS4 log

The app's `printf` is **not** visible on its own. Instead it logs through
`dlog()` (`ps4/src/dlog.c`), which sends each line as plain-text UDP to the PC
**and** mirrors it to klog (`sceKernelDebugOutText`). It is deliberately
event-driven and uses a separate **non-blocking** socket, so it does not affect
the input loop or video.

**PC** (a third terminal):
```
cd pc
python log_viewer.py                 # listens on udp 0.0.0.0:47003
```
You'll see boot info, the active pad user, the first decoded frame, pad
connect/disconnect, and a ~1 Hz stats line:
```
03:39:39 (+   0.90s) 192.168.1.20  [120 ms] boot: remote play rev; pc=... in=47001 vid=9025 log=47003
03:39:40 (+   1.10s) 192.168.1.20  [340 ms] video_start: OK (port 9025)
03:39:40 (+   1.30s) 192.168.1.20  [512 ms] first video frame 1280x720
03:39:40 (+   1.50s) 192.168.1.20  [1000 ms] stats sent=250 err=0 pad=OK frames=30 drops=0
```
If you prefer the kernel log, the same lines also appear in **klog** (view it
with your jailbreak's klog tool, e.g. GoldHEN's klog viewer).

## Protocol

See [docs/protocol.md](docs/protocol.md). Input packets are a fixed 18-byte
binary struct (`RP4I`), little-endian, sent at 250 Hz. Feedback is the additive
`RP4F` channel (`STATS` + `KEYFRAME_REQUEST`) on UDP 47002; video is chunked
`RP4V` on UDP 9025.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `rate=0 pkt/s` | Wrong PC IP in `ps4/include/config.h`, or firewall blocking UDP 47001, or PS4 and PC on different subnets/VPN. |
| `bad=N` counting up | Some other app is sending to port 47001, or a version mismatch after a protocol change (rebuild both sides). |
| `seq_gaps` high | Wi-Fi packet loss. Move closer / use 5 GHz / wired. Input tolerates loss (next packet supersedes). |
| Pad opens but nothing moves | Check the virtual pad appears (`joy.cpl` on Windows, `evtest` on Linux); some games need the pad enabled in their controller settings. On Windows the ViGEmBus service must run. |
| `vgamepad` import error (Windows) | ViGEmBus driver not installed, or wrong Python arch (use 64-bit). |
| `evdev` `PermissionError: /dev/uinput` (Linux) | User can't access `/dev/uinput`; see the Linux setup step (udev rule + `input` group). Also `sudo modprobe uinput`. |
| Virtual pad exists but no `/dev/input/eventN` (Linux) | `uinput` module not loaded. |
| High latency / stuttering | See Phase 2+ notes; for input-only, latency should be < 10 ms on LAN. |
| PS4 app shows `pad: MISSING` | No DualShock 4 paired/connected for the active user. |
| PS4 screen stays dark green/red, no video | `send_mjpeg.py` not running, wrong `ps4_host`, PC firewall blocking outbound UDP 9025, or size mismatch. Check the PS4 log line `vid=<n> drop=<m>`. |
| Sender shows `fb=waiting` | No STATS arriving: PS4 app not running, wrong `CFG_PC_IP` in `ps4/include/config.h`, or firewall blocking UDP 47002. |
| Build fails at `PkgTool.Core` with "No usable version of libssl" | Fedora 44 lacks OpenSSL 1.1. Use `install/build-ps4.sh` (Option A). |
| Build fails at `PkgTool.Core` with "Couldn't find a valid ICU package" | Set `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1` (already in `install/ps4env.sh`). |

## Honesty notes

- **PS4 code**: verified to **compile and link** against OpenOrbis v0.5.4
  headers inside a container, producing a fake PKG. It has **not been run on a
  console** (none available). The `scePad*` API signatures and `OrbisPadData`
  fields were checked against the shipped headers, not guessed. Remaining
  runtime unknowns: which user id is active, pad connect timing, and the
  unnamed DS4 SHARE bit.
  - Phase 2 adds **libjpeg-turbo** (cross-compiled, static, no SIMD — see
    `install/build-libjpeg-ps4.sh`) and a POSIX-threads decode thread. Both link
    cleanly (`jpeg_*` and `pthread_*` resolve); neither has run on hardware, so
    decode throughput on the Jaguar and the exact `VideoOut` blit are unverified.
  - Phase 3 adds **FFmpeg** (H.264 decoder + parser + libswscale, static, no asm,
    no threads — see `install/build-ffmpeg-ps4.sh`) and `ps4/src/h264_dec.c`; it
    links (`avcodec_*`/`sws_*` resolve) but has not run on hardware, so software
    720p decode throughput on the Jaguar is unverified.
  - Logging uses `sceKernelDebugOutText` (klog) + a UDP mirror; it links, but the
    exact klog visibility on your jailbreak is untested.
- **PC side**:
  - `protocol.py` round-trip and `fake_sender.py → receive_input.py` smoke-tested.
  - **Linux uinput backend fully tested here**: created a virtual 360 pad, fed
    packets, and read the emitted input events back — buttons, triggers, both
    sticks and the d-pad all mapped correctly, and `reset()` zeroes everything.
  - **Phase 2 sender loopback-tested here**: `send_mjpeg.py --test-pattern` →
    a receiver mirroring `video.c`'s fixed-stride reassembly → Pillow decoded
    **126/126 frames at 1280×720, 0 drops** (≈22 KB/frame, ~2500 datagrams over
    4 s). Real desktop capture (needs a display) and the live PS4 were not used.
  - **Phase 3 H.264 encode tested here**: `send_mjpeg.py --codec h264
    --test-pattern` → `openh264enc` at 30 fps (~3.9 Mbps at 640×360); the
    length-prefixed AU stream was reassembled and decoded cleanly by FFmpeg
    (110 AUs, 4 IDRs at gop=30). The PS4-side decode path has not run on
    hardware.
  - **Windows ViGEm backend not executed** — needs a real Windows machine
    (`vgamepad` is imported lazily, so nothing above depends on it).
  - **Step 1–2 (defaults + feedback) tested here**: `protocol.py` round-trips
    `STATS`/`KEYFRAME_REQUEST` and rejects malformed/unknown packets;
    `send_mjpeg.py --test-pattern --no-send` + `tools/fake_ps4_stats.py`
    showed live `fb_loss`/`fb_jit`/`fb_decq` fields and rate-limited keyframe
    requests. The PS4 side of the channel (`video.c` → `CFG_FEEDBACK_PORT`)
    compiles and links but has not run on hardware.
  - **Step 3 (keyframe on demand) tested here**: requests sent at 1.0 s and
    3.0 s produced IDRs at 1.06 s and 3.06 s (natural GOP was 2 s), using
    `grab_h264.py --test-pattern --gop 60 --control-stdin`. The full sender +
    fake PS4 forwarded 5/5 requests and logged `kfreq=1`. `GstVideo`'s helper
    is used when present, with a hand-built `GstForceKeyUnit` fallback. The
    intra-refresh fallback path was exercised and logs correctly. PS4-side
    request generation is still unverified on hardware.
  - **Step 4 (ABR) tested here**: `pc/abr.py` has 28 pytest cases (decrease,
    cooldown, hysteresis, +5 % recovery, clamps, jitter/drop triggers, the
    fps→resolution ladder, the restore gate, no oscillation). Live bitrate
    control was measured end-to-end: 4 Mbps→800 kbps dropped the average access
    unit from 16280 B to 4047 B with no restart. With
    `tools/fake_ps4_stats.py --loss 8` the sender walked 2.5 Mbps down to
    600 kbps, then 30→20→15 fps and 1280x720→960x540→640x360, with restarts
    **5.1 s apart**, each keeping the ABR bitrate and a monotonic `frame_id`;
    switching the fake to `--loss 0` produced +5 % steps every 5 s. The
    controller reacts to the PS4's real numbers only on hardware — the fake
    models frame loss as `loss × 1.5`.
- Container build artifacts were produced with clang 22 / lld 22 (Alpine) and
  packaged with the toolchain's own `PkgTool.Core` (Ubuntu 20.04 for OpenSSL
  1.1). Your OpenOrbis version may differ; if the sample Makefile flags change,
  mirror them in `ps4/Makefile`.
