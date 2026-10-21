#include "packet.h"

#include <string.h>

namespace fb {

// Byte layout (little endian):
//  0 magic | 1 version | 2 type | 3 flags | 4 origin | 5 finalDst | 6 hopSrc | 7 hopDst
//  8 ttl | 9 hops | 10 flow | 11 reserved | 12..15 seq | 16..19 originUs | 20..21 payloadLen | 22..23 crc

static void put16(uint8_t* p, uint16_t v) { p[0] = v & 0xFF; p[1] = v >> 8; }
static void put32(uint8_t* p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = (v >> (8 * i)) & 0xFF; }
static uint16_t get16(const uint8_t* p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t get32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

uint16_t crc16(const uint8_t* data, size_t len, uint16_t crc) {
  // CRC-16/CCITT-FALSE
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; b++) crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
  }
  return crc;
}

static uint16_t frameCrc(const uint8_t* frame, size_t len) {
  uint16_t c = crc16(frame, 22);
  return crc16(frame + HEADER_LEN, len - HEADER_LEN, c);
}

size_t encodeFrame(const Header& h, const uint8_t* payload, uint8_t* out, size_t outCap) {
  size_t len = HEADER_LEN + h.payloadLen;
  if (h.payloadLen > MAX_PAYLOAD || len > outCap) return 0;
  out[0] = FRAME_MAGIC;
  out[1] = FRAME_VERSION;
  out[2] = h.type;
  out[3] = h.flags;
  out[4] = h.origin;
  out[5] = h.finalDst;
  out[6] = h.hopSrc;
  out[7] = h.hopDst;
  out[8] = h.ttl;
  out[9] = h.hops;
  out[10] = h.flow;
  out[11] = 0;
  put32(out + 12, h.seq);
  put32(out + 16, h.originUs);
  put16(out + 20, h.payloadLen);
  if (h.payloadLen) {
    if (payload) memcpy(out + HEADER_LEN, payload, h.payloadLen);
    else memset(out + HEADER_LEN, 0xA5, h.payloadLen);
  }
  put16(out + 22, frameCrc(out, len));
  return len;
}

DecodeResult decodeFrame(const uint8_t* f, size_t len, Header& h) {
  if (len < HEADER_LEN) return DECODE_SHORT;
  h.type = f[2];
  h.flags = f[3];
  h.origin = f[4];
  h.finalDst = f[5];
  h.hopSrc = f[6];
  h.hopDst = f[7];
  h.ttl = f[8];
  h.hops = f[9];
  h.flow = f[10];
  h.seq = get32(f + 12);
  h.originUs = get32(f + 16);
  h.payloadLen = get16(f + 20);
  // CRC first: a flipped bit anywhere (including magic/len) is reported as corruption.
  if (HEADER_LEN + (size_t)h.payloadLen != len) {
    return (f[0] == FRAME_MAGIC) ? DECODE_BAD_LEN : DECODE_BAD_MAGIC;
  }
  if (get16(f + 22) != frameCrc(f, len)) return DECODE_BAD_CRC;
  if (f[0] != FRAME_MAGIC || f[1] != FRAME_VERSION) return DECODE_BAD_MAGIC;
  return DECODE_OK;
}

void rewriteHop(uint8_t* f, size_t len, uint8_t hopSrc, uint8_t hopDst, uint8_t ttl, uint8_t hops) {
  f[6] = hopSrc;
  f[7] = hopDst;
  f[8] = ttl;
  f[9] = hops;
  put16(f + 22, frameCrc(f, len));
}

}  // namespace fb
