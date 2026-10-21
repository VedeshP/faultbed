// FaultBed frame: the same 24-byte header rides inside every transport
// (ESP-NOW payload, UDP datagram, MQTT message, CoAP payload), so routing,
// fault injection and measurement are identical regardless of protocol.
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace fb {

static const uint8_t FRAME_MAGIC = 0xFB;
static const uint8_t FRAME_VERSION = 1;
static const uint8_t BROADCAST = 0xFF;
static const uint8_t NO_NODE = 0x00;
static const size_t HEADER_LEN = 24;
static const size_t MAX_FRAME = 250;  // ESP-NOW limit; the strictest transport wins
static const size_t MAX_PAYLOAD = MAX_FRAME - HEADER_LEN;

enum FrameType : uint8_t {
  FT_DATA = 1,
  FT_ECHO_REPLY = 2,
};

enum FrameFlags : uint8_t {
  FF_ECHO_REQUEST = 0x01,
};

struct Header {
  uint8_t type;
  uint8_t flags;
  uint8_t origin;     // end-to-end source
  uint8_t finalDst;   // end-to-end destination
  uint8_t hopSrc;     // transmitter of this hop
  uint8_t hopDst;     // intended receiver of this hop (BROADCAST allowed)
  uint8_t ttl;
  uint8_t hops;       // hops travelled so far
  uint8_t flow;
  uint32_t seq;
  uint32_t originUs;  // origin's clock (truncated) - used for RTT on echo replies
  uint16_t payloadLen;
};

uint16_t crc16(const uint8_t* data, size_t len, uint16_t crc = 0xFFFF);

// Writes header + payload into out (must hold HEADER_LEN + payloadLen). Returns frame length or 0.
size_t encodeFrame(const Header& h, const uint8_t* payload, uint8_t* out, size_t outCap);

enum DecodeResult { DECODE_OK, DECODE_SHORT, DECODE_BAD_MAGIC, DECODE_BAD_LEN, DECODE_BAD_CRC };

// Parses without trusting the frame. hdr is filled as far as possible even on CRC failure.
DecodeResult decodeFrame(const uint8_t* frame, size_t len, Header& hdr);

// Rewrites hop fields and recomputes CRC in place (forwarding).
void rewriteHop(uint8_t* frame, size_t len, uint8_t hopSrc, uint8_t hopDst, uint8_t ttl, uint8_t hops);

}  // namespace fb
