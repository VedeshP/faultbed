// CoAP (RFC 7252) over UDP: POST /fb carrying the FaultBed frame.
// Confirmable mode implements CoAP's own reliability (ACK, exponential
// back-off, MAX_RETRANSMIT, duplicate detection). Faults are applied before an
// ACK is sent and when an ACK arrives, so injected loss genuinely triggers
// CoAP retransmissions - which is exactly what you want to measure.
// Config: {"name":"coap","confirmable":true,"ack_timeout_ms":2000,"max_retransmit":4,"port":5683,"wifi":{...}}
#include <WiFi.h>
#include <WiFiUdp.h>
#include <string.h>

#include "../transport.h"
#include "coap.h"
#include "fb_hash.h"

class CoapTransport : public Transport {
 public:
  const char* name() const override { return "coap"; }

  bool begin(JsonObjectConst cfg, uint8_t selfId) override {
    self_ = selfId;
    port_ = cfg["port"] | 5683;
    confirmable_ = cfg["confirmable"] | true;
    ackTimeoutUs_ = (uint64_t)(cfg["ack_timeout_ms"] | 2000) * 1000ULL;
    maxRetransmit_ = cfg["max_retransmit"] | 4;
    nextMid_ = (uint16_t)(selfId << 8);
    for (auto& p : pending_) p.used = false;
    for (auto& s : seen_) s.valid = false;
    wifi::start(cfg);
    started_ = true;
    return true;
  }

  void end() override {
    if (!started_) return;
    udp_.stop();
    listening_ = false;
    wifi::stop();
    started_ = false;
    reportLink(false);
  }

  bool isUp() const override { return started_ && listening_ && wifi::connected(); }

  bool send(uint8_t hopDst, const uint8_t* frame, size_t len) override {
    if (!isUp()) return false;
    const Peer* p = peer(hopDst);
    if (!p || !p->hasIp) {  // CoAP needs a unicast endpoint for ACKs
      counters_.sendErr++;
      return false;
    }
    coap::Message m;
    m.type = confirmable_ ? coap::CON : coap::NON;
    m.code = coap::CODE_POST;
    m.messageId = nextMid_++;
    m.payload = frame;
    m.payloadLen = len;
    if (confirmable_) {
      Pending* slot = freeSlot();
      if (!slot) {  // NSTART-style limit reached
        counters_.sendErr++;
        return false;
      }
      slot->used = true;
      slot->dst = p->ip;
      slot->dstId = hopDst;
      slot->mid = m.messageId;
      slot->attempts = 0;
      // ACK_TIMEOUT * [1, ACK_RANDOM_FACTOR=1.5), deterministic per message id
      slot->timeoutUs = ackTimeoutUs_ + ackTimeoutUs_ * (fb::splitmix64(m.messageId) % 500) / 1000;
      slot->deadlineUs = nowUs() + slot->timeoutUs;
      slot->len = coap::encode(m, slot->msg, sizeof slot->msg);
      return transmitRaw(p->ip, slot->msg, slot->len);
    }
    size_t n = coap::encode(m, buf_, sizeof buf_);
    return n && transmitRaw(p->ip, buf_, n);
  }

  void loop() override {
    if (!started_) return;
    bool up = wifi::connected();
    if (up && !listening_) listening_ = udp_.begin(port_);
    if (!up && listening_) {
      udp_.stop();
      listening_ = false;
    }
    reportLink(up && listening_);
    if (!listening_) return;

    int n;
    while ((n = udp_.parsePacket()) > 0) {
      IPAddress from = udp_.remoteIP();
      uint16_t fromPort = udp_.remotePort();
      int len = udp_.read(buf_, sizeof buf_);
      if (len > 0) handle(buf_, (size_t)len, from, fromPort);
    }
    retransmit();
  }

 private:
  struct Pending {
    bool used;
    IPAddress dst;
    uint8_t dstId;
    uint16_t mid;
    uint8_t attempts;
    uint64_t timeoutUs, deadlineUs;
    size_t len;
    uint8_t msg[fb::MAX_FRAME + 16];
  };
  struct Seen {
    bool valid;
    uint8_t src;
    uint16_t mid;
  };
  static const int MAX_PENDING = 8;
  // Must outlive the longest retransmission chain (ACK_TIMEOUT * 1.5 * 2^MAX_RETRANSMIT) at the expected rate,
  // otherwise a late retransmission is mistaken for a new message and delivered twice.
  static const int SEEN_SLOTS = 128;

  Pending* freeSlot() {
    for (auto& p : pending_)
      if (!p.used) return &p;
    return nullptr;
  }

  bool transmitRaw(const IPAddress& ip, const uint8_t* data, size_t len) {
    bool ok = udp_.beginPacket(ip, port_) && udp_.write(data, len) == len && udp_.endPacket();
    if (ok) counters_.sendOk++;
    else counters_.sendErr++;
    return ok;
  }

  bool seen(uint8_t src, uint16_t mid) const {
    for (auto& s : seen_)
      if (s.valid && s.src == src && s.mid == mid) return true;
    return false;
  }

  void remember(uint8_t src, uint16_t mid) {
    seen_[seenNext_] = {true, src, mid};
    seenNext_ = (seenNext_ + 1) % SEEN_SLOTS;
  }

  void sendAck(const IPAddress& ip, uint16_t port, uint16_t mid) {
    coap::Message ack;
    ack.type = coap::ACK;
    ack.code = coap::CODE_CHANGED;
    ack.messageId = mid;
    uint8_t out[8];
    size_t n = coap::encode(ack, out, sizeof out);
    udp_.beginPacket(ip, port);
    udp_.write(out, n);
    udp_.endPacket();
  }

  void handle(const uint8_t* data, size_t len, const IPAddress& from, uint16_t fromPort) {
    coap::Message m;
    if (!coap::decode(data, len, m)) return;
    if (m.type == coap::ACK || m.type == coap::RST) {
      int8_t src = peerIdByIp(from);
      // The ACK travels over the faulty link too.
      if (src > 0 && gate_ && !gate_->admitControl((uint8_t)src)) return;
      for (auto& p : pending_)
        if (p.used && p.mid == m.messageId) p.used = false;
      return;
    }
    if (m.code != coap::CODE_POST || !m.payload || m.payloadLen < fb::HEADER_LEN) return;
    uint8_t src = m.payload[6];  // FaultBed hopSrc
    if (m.type == coap::CON && seen(src, m.messageId)) {
      // Retransmission of something we already accepted: our ACK was lost. Re-ACK, don't deliver twice.
      if (gate_ && gate_->admitControl(src)) sendAck(from, fromPort, m.messageId);
      return;
    }
    bool admitted = gate_ && gate_->admit(m.payload, m.payloadLen, (int8_t)WiFi.RSSI());
    if (!admitted) return;  // "lost on the air": no ACK, sender will retransmit
    if (m.type == coap::CON) {
      remember(src, m.messageId);
      sendAck(from, fromPort, m.messageId);
    }
  }

  void retransmit() {
    uint64_t now = nowUs();
    for (auto& p : pending_) {
      if (!p.used || now < p.deadlineUs) continue;
      if (p.attempts >= maxRetransmit_) {
        p.used = false;
        counters_.giveUps++;
        char b[110];
        snprintf(b, sizeof b, "{\"e\":\"txerr\",\"t\":%llu,\"tr\":\"coap\",\"why\":\"giveup\",\"mid\":%u,\"to\":%u}",
                 (unsigned long long)now, p.mid, p.dstId);
        emit(b);
        continue;
      }
      p.attempts++;
      p.timeoutUs *= 2;
      p.deadlineUs = now + p.timeoutUs;
      counters_.retransmits++;
      transmitRaw(p.dst, p.msg, p.len);
    }
  }

  WiFiUDP udp_;
  uint16_t port_ = 5683;
  bool started_ = false, listening_ = false, confirmable_ = true;
  uint64_t ackTimeoutUs_ = 2000000ULL;
  uint8_t maxRetransmit_ = 4;
  uint16_t nextMid_ = 0;
  Pending pending_[MAX_PENDING];
  Seen seen_[SEEN_SLOTS];
  int seenNext_ = 0;
  uint8_t buf_[fb::MAX_FRAME + 32];
};

FB_REGISTER_TRANSPORT(CoapTransport, "coap");
