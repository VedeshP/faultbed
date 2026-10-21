// Minimal CoAP (RFC 7252) message codec: enough for POST /fb with CON/NON and
// empty ACKs. Kept Arduino-free so it is unit-tested on the host.
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace fb {
namespace coap {

enum Type : uint8_t { CON = 0, NON = 1, ACK = 2, RST = 3 };
static const uint8_t CODE_EMPTY = 0x00;
static const uint8_t CODE_POST = 0x02;    // 0.02
static const uint8_t CODE_CHANGED = 0x44; // 2.04

struct Message {
  Type type = NON;
  uint8_t code = CODE_EMPTY;
  uint16_t messageId = 0;
  const uint8_t* payload = nullptr;
  size_t payloadLen = 0;
};

// Encodes with Uri-Path "fb" when code is a request. Returns length or 0.
size_t encode(const Message& m, uint8_t* out, size_t cap);
// Parses; payload points into `in`. Returns false on malformed input.
bool decode(const uint8_t* in, size_t len, Message& m);

}  // namespace coap
}  // namespace fb
