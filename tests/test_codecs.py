"""Synthetic tests for the reference codecs (no game data). Run: python -m pytest tests  (or python tests/test_codecs.py)"""
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "reference"))
import anix  # noqa: E402


def random_quats(n, seed=1):
    q = np.random.default_rng(seed).normal(size=(n, 4))
    return q / np.linalg.norm(q, axis=1, keepdims=True)


def test_known_hashes():
    assert anix.name_hash("Rotation") == 0x27436396
    assert anix.name_hash("Pos") == 0x00CE66FC
    assert anix.name_hash("LITH") == 0x2B99DBC1
    assert anix.name_hash("KeyString") == 0xB9010BE0
    assert anix.name_hash("rotation") == anix.name_hash("ROTATION")          # case-insensitive


def test_quaternion_roundtrip():
    q = random_quats(200)
    row = anix.pack_row(q)
    back, _ = anix.unpack_row(row, len(q))
    dot = np.abs(np.sum(back * q, axis=1))
    assert np.all(dot > 1 - 1e-6)                                        # same rotation (sign may flip)


def test_padding_slots_are_identity_not_zero():
    row = anix.pack_row(random_quats(4))                                  # 4 tracks -> 2 words, 2 padding slots
    _, radicands = anix.unpack_row(row, 4, include_padding=True)
    assert len(radicands) == 6 and min(radicands[4:]) > 0.99            # padding = identity (radicand ~1)
    assert min(radicands) >= 0.25 - 1e-3                                  # real slots: largest component^2 >= 1/4
    zero_padded = bytearray(row)
    word = int.from_bytes(zero_padded[16:32], "little") & ((1 << 42) - 1)   # keep slot 0, zero slots 1-2
    zero_padded[16:32] = word.to_bytes(16, "little")
    _, radicands = anix.unpack_row(bytes(zero_padded), 4, include_padding=True)
    assert min(radicands) < 0                                             # what crashes the engine (NaN)


def test_aq03_block_and_nlerp():
    T, K = 5, 4
    ticks = [0, 33, 100, 200]
    keys = np.stack([random_quats(T, seed=k) for k in range(K)])
    head = struct.pack("<3I", ticks[0], ticks[-1], K) + struct.pack(f"<{K}H", 0, *np.diff(ticks))
    head += bytes(anix.align(len(head), 16) - len(head))
    block = head + b"".join(anix.pack_row(keys[k]) for k in range(K))
    t, decoded = anix.decode_aq03(block, T)
    assert list(t) == ticks
    mid = anix.sample_aq03(t, decoded, 66)
    assert np.allclose(np.linalg.norm(mid, axis=1), 1, atol=1e-6)
    assert np.allclose(np.abs(np.sum(anix.sample_aq03(t, decoded, 999) * decoded[-1], 1)), 1, atol=1e-6)   # clamped


def test_sv01():
    xyz = np.array([[0.0, 10.0, -100.0], [255.0, -255.0, 3.5]])
    u = np.round((xyz + 256) / 512 * 1022).astype(np.uint32)
    words = (u[:, 0] << 2) | (u[:, 1] << 12) | (u[:, 2] << 22)
    block = words.astype("<u4").tobytes()
    block += bytes(anix.align(len(block), 16) - len(block))
    assert np.allclose(anix.decode_sv01(block, 2), xyz, atol=0.26)


def test_uatr_exact_encoder():
    S, T = 7, 3
    ticks = [0, 33, 66, 100, 133, 166, 200]
    v = np.random.default_rng(3).normal(size=(S, T, 3)).astype(np.float32) * 50
    block = anix.uatr_exact_block(v)
    assert np.allclose(anix.decode_uatr(block, T, ticks, ticks), v, atol=1e-4)
    half = anix.decode_uatr(block, T, ticks, [16.5])[0]
    assert np.allclose(half, (v[0] + v[1]) / 2, atol=1e-3)               # linear between samples


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
