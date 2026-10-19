"""Reference reader / decoder for Shadow of War ANIX v44 animation libraries.

Pure Python + NumPy, no game data. See ../README.md for the format. The decoders reproduce the engine's float32
arithmetic, including its lack of clamping, so `strict_check` reports exactly the blocks that would make the game
assert ("EvaluateBlockAQ03 decoded NAN").

    python anix.py some_library.anix            # summary + strict check
"""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------------------------------------- hashing
CHAR_MAP = bytes.fromhex(
    "0000000000000000000000000000000000000000000000000000000000000000"
    "253a333c3d3e403242434128362737341b1c1d1e1f2021222324313038293935"
    "3b0102030405060708090a0b0c0d0e0f101112131415161718191a2c2a2d3f26"
    "440102030405060708090a0b0c0d0e0f101112131415161718191a2e2b2f4500")


def name_hash(name: str) -> int:
    """Case-insensitive 32-bit name hash used for clips, bones, channel types and block tags."""
    h = 0
    for c in name.encode("ascii"):
        h = (h * 0x397 + CHAR_MAP[c]) & 0xFFFFFFFF
    return h


ROTATION, POS, LITH, KEYSTRING = (name_hash(n) for n in ("Rotation", "Pos", "LITH", "KeyString"))
EVENT_CHANNEL = 0x210C8F5A          # channel type of STRI tracks (name unknown)
FLOAT_CHANNEL = 0x5F1D8EED          # channel type read by CAF4 (name unknown)


def align(v: int, n: int) -> int:
    return (v + n - 1) // n * n


# --------------------------------------------------------------------------------------------- packed quaternions
_INV = np.array([0x39000801], np.uint32).view(np.float32)[0]      # 1 / 8190
_SCALE = np.array([0x3FB504F3], np.uint32).view(np.float32)[0]    # sqrt(2)
_BIAS = np.array([0xBF3504F3], np.uint32).view(np.float32)[0]     # -sqrt(0.5)
IDENTITY_SLOT = 4095 | (4095 << 13) | (4095 << 26)                # native filler for unused slots (metadata 0)


def unpack_slot(word: int):
    """One 42-bit slot -> (xyzw float32, radicand). Engine formula, no clamp: a negative radicand gives NaN."""
    comps = np.array([(word >> s) & 8191 for s in (0, 13, 26)], np.float32) * _INV * _SCALE + _BIAS
    meta = (word >> 39) & 7
    radicand = np.float32(1.0) - np.dot(comps, comps).astype(np.float32)
    with np.errstate(invalid="ignore"):
        missing = np.sqrt(radicand, dtype=np.float32)
    if meta & 4:
        missing = -missing
    omitted = 3 - (meta & 3)
    return np.insert(comps, omitted, missing), float(radicand)


def unpack_row(blob: bytes, track_count: int, include_padding: bool = False):
    """Decode a packed row. Returns (quaternions[track_count, 4], radicands of every slot incl. padding if asked)."""
    words = (track_count + 2) // 3
    if len(blob) < words * 16:
        raise ValueError("packed row too short")
    quats, radicands = [], []
    for g in range(words):
        packed = int.from_bytes(blob[g * 16:(g + 1) * 16], "little")
        for s in range(3):
            q, r = unpack_slot((packed >> (42 * s)) & ((1 << 42) - 1))
            if g * 3 + s < track_count:
                quats.append(q)
                radicands.append(r)
            elif include_padding:
                radicands.append(r)
    return np.array(quats, np.float32).reshape(-1, 4), radicands


def pack_quaternion(q) -> int:
    """Encoder: omit the largest component, make it positive, quantise the other three to 13 bits."""
    q = np.asarray(q, np.float64)
    q = q / np.linalg.norm(q)
    omitted = int(np.argmax(np.abs(q)))
    if q[omitted] < 0:
        q = -q
    word = 0
    for slot, v in enumerate(q[i] for i in range(4) if i != omitted):
        u = int(round((v + np.sqrt(0.5)) / np.sqrt(2.0) * 8190.0))
        word |= max(0, min(8191, u)) << (13 * slot)
    return word | ((3 - omitted) << 39)


def pack_row(quaternions) -> bytes:
    """Three quaternions per 16-byte word; unused slots get the identity filler (never zero bits)."""
    out = bytearray()
    n = len(quaternions)
    for g in range(0, max(n, 1), 3):
        packed = 0
        for s in range(3):
            i = g + s
            packed |= (pack_quaternion(quaternions[i]) if i < n else IDENTITY_SLOT) << (42 * s)
        out += packed.to_bytes(16, "little")
    return bytes(out)


# ---------------------------------------------------------------------------------------------------------- codecs
def aq03_layout(blob: bytes, track_count: int):
    start, end, count = struct.unpack_from("<3I", blob)
    deltas = np.frombuffer(blob, "<u2", count, 12).astype(np.int64)
    if count < 2 or deltas[0] != 0 or np.any(deltas[1:] <= 0):
        raise ValueError("bad AQ03 key deltas")
    times = start + np.cumsum(deltas)
    if times[-1] != end:
        raise ValueError("AQ03 key times do not reach the end tick")
    data = align(12 + 2 * count, 16)
    stride = ((track_count + 2) // 3) * 16
    if data + count * stride > len(blob) or any(blob[data + count * stride:]):
        raise ValueError("AQ03 rows do not fit the block (only zero alignment padding may follow)")
    return times, data, stride


def decode_aq03(blob: bytes, track_count: int):
    """-> (key times [K], quaternions [K, T, 4])."""
    times, data, stride = aq03_layout(blob, track_count)
    keys = np.stack([unpack_row(blob[data + k * stride:data + (k + 1) * stride], track_count)[0] for k in range(len(times))])
    return times, keys


def sample_aq03(times, keys, t):
    """Engine evaluation: clamp, bracketing keys, shortest path, nlerp (not slerp)."""
    t = min(max(t, times[0]), times[-1])
    hi = int(np.clip(np.searchsorted(times, t, side="left"), 1, len(times) - 1))
    lo = hi - 1
    f = np.float32((t - times[lo]) / (times[hi] - times[lo]))
    a, b = keys[lo], keys[hi].copy()
    b[np.sum(a * b, -1) < 0] *= -1
    q = (1 - f) * a + f * b
    return q / np.linalg.norm(q, axis=-1, keepdims=True)


def decode_sq03(blob: bytes, track_count: int):
    return unpack_row(blob, track_count)[0]


def decode_sv01(blob: bytes, track_count: int):
    words = np.frombuffer(blob, "<u4", track_count)
    comps = np.stack([(words >> s) & 1023 for s in (2, 12, 22)], -1).astype(np.float32)
    return comps / np.float32(1022.0) * np.float32(512.0) - np.float32(256.0)


def decode_uatr(blob: bytes, track_count: int, lith_ticks, sample_ticks):
    """UATR uses the segment's LITH ticks as its key times. -> [len(sample_ticks), T, 3]."""
    keys = np.asarray(lith_ticks, np.float64)
    starts = list(struct.unpack_from(f"<{track_count}H", blob)) + [len(blob)]
    t = np.clip(np.asarray(sample_ticks, np.float64), keys[0], keys[-1])
    hi = np.clip(np.searchsorted(keys, t, side="left"), 1, len(keys) - 1)
    lo = hi - 1
    f = ((t - keys[lo]) / (keys[hi] - keys[lo])).astype(np.float32)
    out = np.empty((len(t), track_count, 3), np.float32)
    for tr in range(track_count):
        a, b = starts[tr], starts[tr + 1]
        codes = np.frombuffer(blob, np.uint8, 2 * len(keys), a).reshape(-1, 2)
        ctl_at = align(a + 2 * len(keys), 4)
        n = (b - ctl_at) // 12
        ctl = np.frombuffer(blob, "<f4", 3 * n, ctl_at).reshape(-1, 3)
        idx = codes[:, 0].astype(np.int64)
        w_lo = codes[lo, 1].astype(np.float32) / 255
        w_hi = np.where(idx[lo] == idx[hi], codes[hi, 1].astype(np.float32) / 255, np.float32(1.0))
        w = w_lo + (w_hi - w_lo) * f
        out[:, tr] = ctl[idx[lo]] + (ctl[idx[lo] + 1] - ctl[idx[lo]]) * w[:, None]
    return out


def uatr_exact_block(vectors) -> bytes:
    """Encoder: vectors [S, T, 3] sampled at the S LITH ticks (2 <= S <= 256) -> UATR block reproducing them exactly."""
    vectors = np.asarray(vectors, np.float32)
    S, T = vectors.shape[:2]
    if not 2 <= S <= 256:
        raise ValueError("2..256 samples per segment with this layout")
    codes = bytearray()
    for i in range(S):
        codes += bytes((i, 0)) if i < S - 1 else bytes((S - 2, 255))
    table = align(2 * T, 4)
    per_track = align(2 * S, 4) + 12 * S
    out = bytearray(struct.pack(f"<{T}H", *[table + per_track * t for t in range(T)]))
    out += bytes(table - len(out))
    for t in range(T):
        out += codes + bytes(align(2 * S, 4) - 2 * S) + vectors[:, t].astype("<f4").tobytes()
    return bytes(out)


def decode_stri(blob: bytes, keystring: bytes):
    """-> [(tick, text)]; strings come from the clip's KeyString descriptor extra."""
    _, _, count, _ = struct.unpack_from("<HHIH", blob)
    out = []
    for k in range(count):
        tick, off, ln = struct.unpack_from("<IHH", blob, 10 + 8 * k)
        out.append((tick, keystring[off:off + ln].split(b"\0")[0].decode("ascii", "replace")))
    return out


# -------------------------------------------------------------------------------------------------------- container
@dataclass
class Segment:
    first: int
    last: int
    blocks: dict            # codec -> bytes
    extras: dict            # hash -> bytes (LITH...)
    lith_ticks: list


@dataclass
class Clip:
    name: str
    hash: int
    flags: int
    duration: int
    unknown116: int
    root_start: tuple
    root_end: tuple
    codecs: list
    tracks: list            # (codec, channel index)
    segments: list = field(default_factory=list)
    descriptor_extras: dict = field(default_factory=dict)


@dataclass
class Library:
    channels: list          # (type hash, bone hash)
    clips: list
    footer: dict


def read_anix(raw: bytes) -> Library:
    if raw[:4] != b"ANIX":
        raise ValueError("not an ANIX file")
    version, hsize, size, count, start, payload, psize, meta, msize, _, _ = struct.unpack_from("<11I", raw, 4)
    if (version, hsize, size) != (44, 48, len(raw)) or payload + psize != meta or meta + msize != size:
        raise ValueError("unsupported or inconsistent ANIX header")
    channels, cur = [], start
    while len(channels) < count:
        th, n = struct.unpack_from("<II", raw, cur)
        channels += [(th, h) for h in struct.unpack_from(f"<{n}I", raw, cur + 8)]
        cur += 8 + 4 * n
    table, nclips, _, footer_at = struct.unpack_from("<4I", raw, meta)
    clips = []
    for i in range(nclips):
        h, off = struct.unpack_from("<II", raw, meta + table + 8 * i)
        p = meta + off
        name = raw[p:p + 48].split(b"\0")[0].decode("ascii")
        _, flags, nseg, nextra, ncodec, ntrack = struct.unpack_from("<6H", raw, p + 48)
        unk, duration = struct.unpack_from("<II", raw, p + 116)
        codecs = [raw[p + 124 + 4 * k:p + 128 + 4 * k].decode("ascii") for k in range(ncodec)]
        ce = p + 124 + 4 * ncodec
        types = raw[ce:ce + ntrack]
        ids_at = align(ce + ntrack, 4)
        ids = struct.unpack_from(f"<{ntrack}H", raw, ids_at)
        seg_at = align(ids_at + 2 * ntrack, 4)
        clip = Clip(name, h, flags, duration, unk, struct.unpack_from("<7f", raw, p + 60), struct.unpack_from("<7f", raw, p + 88),
                    codecs, [(codecs[t], c) for t, c in zip(types, ids)])
        ex_at = seg_at + 16 * nseg
        for k in range(nextra):
            eh, eo = struct.unpack_from("<II", raw, ex_at + 8 * k)
            clip.descriptor_extras[eh] = p + eo
        for s in range(nseg):
            so, ln, first, last = struct.unpack_from("<4I", raw, seg_at + 16 * s)
            blob = raw[payload + so:payload + so + ln]
            e = struct.unpack_from("<I", blob)[0]
            offs = struct.unpack_from(f"<{ncodec + e}H", blob, 4)
            hashes = struct.unpack_from(f"<{e}I", blob, align(4 + 2 * (ncodec + e), 4))
            bounds = list(offs) + [ln]
            blocks = {codecs[k]: blob[bounds[k]:bounds[k + 1]] for k in range(ncodec)}
            extras = {hashes[k]: blob[offs[ncodec + k]:bounds[ncodec + k + 1]] for k in range(e)}
            ticks = []
            if LITH in extras:
                n = struct.unpack_from("<I", extras[LITH])[0]
                ticks = list(struct.unpack_from(f"<{n}I", extras[LITH], 12))
            clip.segments.append(Segment(first, last, blocks, extras, ticks))
        clips.append(clip)
    return Library(channels, clips, read_footer(raw[meta + footer_at:]))


def read_footer(foot: bytes) -> dict:
    h = struct.unpack_from("<6I", foot)
    body = foot[24:]
    names = body[:h[4]].split(b"\0")
    strings = [s.decode("ascii", "replace") for s in body[h[4]:h[0]].split(b"\0")[:-1]]
    cur, end, records = h[0] + 41, h[0] + h[5] - 12, []
    while cur < end:
        ext = struct.unpack_from("<3f", body, cur)
        name_off, u1, u2, u3, flag, frames, n = struct.unpack_from("<7I", body, cur + 12)
        marks = struct.unpack_from(f"<{n}I", body, cur + 40)
        markers = struct.unpack_from(f"<{n}I", body, cur + 40 + 4 * n)
        records.append(dict(name=body[name_off:body.index(b"\0", name_off)].decode("ascii", "replace"), extents=ext,
                            frames=frames, marks=marks,
                            markers=[(m & 0xFFFFFF, None if m >> 24 == 0xFF else strings[m >> 24]) for m in markers]))
        cur += 40 + 8 * n
    return dict(header=h, library_path=names[0].decode("ascii", "replace"), event_strings=strings, records=records)


def strict_check(lib: Library) -> list:
    """Every AQ03/SQ03 slot (padding included) decoded like the engine: returns the offending blocks."""
    bad = []
    for c in lib.clips:
        counts = {}
        for codec, _ in c.tracks:
            counts[codec] = counts.get(codec, 0) + 1
        for si, s in enumerate(c.segments):
            for codec in ("SQ03", "AQ03"):
                if codec not in s.blocks:
                    continue
                n = counts[codec]
                blob = s.blocks[codec]
                rows = [blob] if codec == "SQ03" else None
                if codec == "AQ03":
                    times, data, stride = aq03_layout(blob, n)
                    rows = [blob[data + k * stride:data + (k + 1) * stride] for k in range(len(times))]
                worst = min(min(unpack_row(r, n, include_padding=True)[1]) for r in rows)
                if worst < 0:
                    bad.append((c.name, si, codec, worst))
    return bad


if __name__ == "__main__":
    lib = read_anix(open(sys.argv[1], "rb").read())
    print(f"{len(lib.channels)} channels, {len(lib.clips)} clips, footer path {lib.footer['library_path']}")
    for c in lib.clips[:20]:
        print(f"  {c.name:40s} {c.duration:6d} ms  {len(c.segments)} seg  {'/'.join(c.codecs)}")
    bad = strict_check(lib)
    print("strict check:", "OK" if not bad else f"{len(bad)} blocks would assert: {bad[:5]}")
