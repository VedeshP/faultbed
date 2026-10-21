// Transport plug-in interface.
//
// Adding a protocol = one new file in src/transports/ that subclasses
// Transport and ends with FB_REGISTER_TRANSPORT(MyTransport, "myproto").
// Nothing else in the firmware or controller needs to change: the node core
// only ever sees fb::ILink, and the controller discovers the transport name
// from the node's `hello` reply.
//
// Contract for implementers:
//  * send() moves one opaque FaultBed frame (<= fb::MAX_FRAME bytes) to the
//    node with id `hopDst`; resolve the id via peer(hopDst) (MAC / IP).
//  * Every received frame goes through gate_->admit() on the main task
//    (call it from loop(); if your radio delivers on another task, buffer with
//    RxRing first). If the protocol acknowledges messages, consult admit()
//    *before* acknowledging, and admitControl() when an ACK arrives.
//  * Call reportLink() whenever the underlying connection goes up or down so
//    recovery time can be measured.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <IPAddress.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include "fault_engine.h"
#include "link.h"
#include "packet.h"

struct Peer {
  bool hasMac = false;
  bool hasIp = false;
  uint8_t mac[6] = {0};
  IPAddress ip;
};

struct TransportCounters {
  uint32_t sendOk = 0, sendErr = 0, linkUps = 0, linkDowns = 0, retransmits = 0, giveUps = 0, rxOverflow = 0;
};

class Transport : public fb::ILink {
 public:
  static const int MAX_PEERS = fb::FaultEngine::MAX_NODES;

  virtual ~Transport() {}
  virtual const char* name() const = 0;
  // Bring the transport up (non-blocking where possible). cfg is the transport's JSON section.
  virtual bool begin(JsonObjectConst cfg, uint8_t selfId) = 0;
  virtual void end() = 0;
  virtual void loop() = 0;
  // Called when the peer table changes (e.g. ESP-NOW must register MACs).
  virtual void onPeersChanged() {}

  void setPeer(uint8_t id, const Peer& p);
  const Peer* peer(uint8_t id) const;
  int8_t peerIdByIp(const IPAddress& ip) const;
  const TransportCounters& counters() const { return counters_; }
  void logCounters();

  static void setSink(fb::Sink* s) { sink_ = s; }
  static uint64_t nowUs();

 protected:
  void reportLink(bool up, const char* detail = nullptr);
  void emit(const char* json);
  bool linkUp_ = false;
  uint8_t self_ = 0;
  TransportCounters counters_;
  Peer peers_[MAX_PEERS];
  static fb::Sink* sink_;
};

// Thread-safe hand-off for transports whose radio callbacks run on another task.
class RxRing {
 public:
  struct Item {
    uint8_t len;
    int8_t rssi;
    uint8_t src;  // transport-level sender id if known (0 = unknown)
    uint8_t data[fb::MAX_FRAME];
  };
  bool init(int depth = 16);
  void deinit();
  bool pushFromTask(const uint8_t* data, size_t len, int8_t rssi, uint8_t src = 0);
  bool pop(Item& out);

 private:
  QueueHandle_t q_ = nullptr;
};

// Shared Wi-Fi station helper used by the IP-based transports.
namespace wifi {
void start(JsonObjectConst cfg);  // reads cfg["wifi"]: ssid, pass
void stop();
bool connected();
}  // namespace wifi

// ---- registry ----
typedef Transport* (*TransportFactory)();
namespace registry {
void add(const char* name, TransportFactory f);
Transport* create(const char* name);
int count();
const char* nameAt(int i);
}  // namespace registry

struct TransportRegistrar {
  TransportRegistrar(const char* name, TransportFactory f) { registry::add(name, f); }
};

#define FB_REGISTER_TRANSPORT(cls, nm)                      \
  static Transport* fbMake_##cls() { return new cls(); }   \
  static TransportRegistrar fbRegistrar_##cls(nm, fbMake_##cls)
