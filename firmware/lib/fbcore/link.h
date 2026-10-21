// The boundary between the protocol-independent node core and a transport.
//
// A transport only has to move opaque FaultBed frames to a next-hop node id.
// It hands every received frame to the IngressGate *before* doing any
// protocol-level acknowledgement, so injected faults also affect a protocol's
// own reliability mechanism (e.g. a dropped CoAP CON is not ACKed and gets
// retransmitted, exactly as on a lossy radio).
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace fb {

class IngressGate {
 public:
  virtual ~IngressGate() {}
  // A data frame arrived. Returns true if the frame "survived the link"
  // (the transport may acknowledge it); false if it should be treated as never received.
  virtual bool admit(const uint8_t* frame, size_t len, int8_t rssi) = 0;
  // A protocol control message (e.g. CoAP ACK) arrived from `src`. Only loss applies.
  virtual bool admitControl(uint8_t src) = 0;
};

class ILink {
 public:
  virtual ~ILink() {}
  // Send a frame to hopDst (fb::BROADCAST allowed if the transport supports it).
  virtual bool send(uint8_t hopDst, const uint8_t* frame, size_t len) = 0;
  virtual bool isUp() const = 0;
  void setGate(IngressGate* g) { gate_ = g; }

 protected:
  IngressGate* gate_ = nullptr;
};

class Clock {
 public:
  virtual ~Clock() {}
  virtual uint64_t nowUs() = 0;
};

class Sink {
 public:
  virtual ~Sink() {}
  virtual void line(const char* s) = 0;  // one JSON object, no trailing newline
};

}  // namespace fb
