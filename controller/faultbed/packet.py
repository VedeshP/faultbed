"""Python port of firmware/lib/fbcore/packet.cpp (used by the simulator)."""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC, VERSION, BROADCAST = 0xFB, 1, 0xFF
HEADER_LEN = 24
MAX_FRAME = 250
MAX_PAYLOAD = MAX_FRAME - HEADER_LEN
FT_DATA, FT_ECHO_REPLY = 1, 2
FF_ECHO_REQUEST = 0x01
_HDR = struct.Struct("<BBBBBBBBBBBBIIH")  # 22 bytes before the CRC


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass
class Header:
    type: int = FT_DATA
    flags: int = 0
    origin: int = 0
    final_dst: int = 0
    hop_src: int = 0
    hop_dst: int = 0
    ttl: int = 8
    hops: int = 0
    flow: int = 0
    seq: int = 0
    origin_us: int = 0
    payload_len: int = 0


def _crc(frame: bytes) -> int:
    return crc16(frame[HEADER_LEN:], crc16(frame[:22]))


def encode(h: Header, payload: bytes | None = None) -> bytes:
    if h.payload_len > MAX_PAYLOAD:
        raise ValueError("payload too large")
    body = payload if payload is not None else bytes([0xA5]) * h.payload_len
    head = _HDR.pack(MAGIC, VERSION, h.type, h.flags, h.origin, h.final_dst, h.hop_src, h.hop_dst, h.ttl, h.hops,
                     h.flow, 0, h.seq & 0xFFFFFFFF, h.origin_us & 0xFFFFFFFF, h.payload_len)
    frame = head + b"\x00\x00" + body
    return frame[:22] + struct.pack("<H", _crc(frame)) + frame[24:]


def decode(frame: bytes) -> tuple[bool, Header]:
    """(ok, header). Header is best-effort even when ok is False."""
    if len(frame) < HEADER_LEN:
        return False, Header()
    f = _HDR.unpack(frame[:22])
    h = Header(f[2], f[3], f[4], f[5], f[6], f[7], f[8], f[9], f[10], f[12], f[13], f[14])
    if HEADER_LEN + h.payload_len != len(frame):
        return False, h
    if struct.unpack("<H", frame[22:24])[0] != _crc(frame):
        return False, h
    return f[0] == MAGIC and f[1] == VERSION, h


def rewrite_hop(frame: bytes, hop_src: int, hop_dst: int, ttl: int, hops: int) -> bytes:
    b = bytearray(frame)
    b[6], b[7], b[8], b[9] = hop_src, hop_dst, ttl, hops
    b[22:24] = b"\x00\x00"
    b[22:24] = struct.pack("<H", _crc(bytes(b)))
    return bytes(b)


def flip_bit(frame: bytes, bit: int) -> bytes:
    b = bytearray(frame)
    b[bit // 8] ^= 1 << (bit % 8)
    return bytes(b)
