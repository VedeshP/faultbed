#include "coap.h"

#include <string.h>

namespace fb {
namespace coap {

static const uint8_t OPT_URI_PATH = 11;

size_t encode(const Message& m, uint8_t* out, size_t cap) {
  bool request = m.code != CODE_EMPTY && m.code < 0x20;
  size_t need = 4 + (request ? 3 : 0) + (m.payloadLen ? 1 + m.payloadLen : 0);
  if (need > cap) return 0;
  size_t n = 0;
  out[n++] = (uint8_t)(0x40 | ((m.type & 0x3) << 4));  // ver=1, TKL=0
  out[n++] = m.code;
  out[n++] = (uint8_t)(m.messageId >> 8);
  out[n++] = (uint8_t)(m.messageId & 0xFF);
  if (request) {
    out[n++] = (uint8_t)((OPT_URI_PATH << 4) | 2);
    out[n++] = 'f';
    out[n++] = 'b';
  }
  if (m.payloadLen) {
    out[n++] = 0xFF;
    memcpy(out + n, m.payload, m.payloadLen);
    n += m.payloadLen;
  }
  return n;
}

static bool readExt(const uint8_t* in, size_t len, size_t& pos, uint32_t& v) {
  if (v == 13) {
    if (pos >= len) return false;
    v = 13 + in[pos++];
  } else if (v == 14) {
    if (pos + 1 >= len) return false;
    v = 269 + ((uint32_t)in[pos] << 8) + in[pos + 1];
    pos += 2;
  } else if (v == 15) {
    return false;
  }
  return true;
}

bool decode(const uint8_t* in, size_t len, Message& m) {
  if (len < 4 || (in[0] >> 6) != 1) return false;
  m.type = (Type)((in[0] >> 4) & 0x3);
  uint8_t tkl = in[0] & 0x0F;
  if (tkl > 8) return false;
  m.code = in[1];
  m.messageId = (uint16_t)((in[2] << 8) | in[3]);
  size_t pos = 4 + tkl;
  if (pos > len) return false;
  m.payload = nullptr;
  m.payloadLen = 0;
  while (pos < len) {
    if (in[pos] == 0xFF) {
      pos++;
      if (pos >= len) return false;  // marker with empty payload is malformed
      m.payload = in + pos;
      m.payloadLen = len - pos;
      return true;
    }
    uint32_t delta = in[pos] >> 4, olen = in[pos] & 0x0F;
    pos++;
    if (!readExt(in, len, pos, delta) || !readExt(in, len, pos, olen)) return false;
    if (pos + olen > len) return false;
    pos += olen;
  }
  return true;
}

}  // namespace coap
}  // namespace fb
