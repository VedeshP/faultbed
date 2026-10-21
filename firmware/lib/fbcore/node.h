// Protocol-independent node logic: virtual topology, routing, traffic
// generation, echo/RTT, fault-injected ingress and structured event logging.
#pragma once
#include <stddef.h>
#include <stdint.h>

#include "fault_engine.h"
#include "link.h"
#include "packet.h"

namespace fb {

struct Flow {
  uint8_t id = 0;
  uint8_t dst = 0;
  uint32_t intervalMs = 1000;
  uint16_t size = 32;        // payload bytes
  bool echo = true;          // request an echo reply (RTT)
  bool poisson = false;      // exponential inter-arrival instead of periodic
  uint32_t count = 0;        // 0 = until stopped
  uint32_t startMs = 0;      // offset from run start
};

struct NodeConfig {
  static const int MAX_FLOWS = 8;
  uint8_t id = 0;
  uint64_t seed = 1;
  uint8_t ttl = 8;
  bool neighborFilter = true;                     // enforce the virtual topology
  bool neighbors[FaultEngine::MAX_NODES] = {};    // who this node can "hear"
  uint8_t routes[FaultEngine::MAX_NODES] = {};    // next hop per final destination (0 = direct)
  Flow flows[MAX_FLOWS];
  int flowCount = 0;
};

struct NodeStats {
  uint32_t generated = 0, sendFail = 0, delivered = 0, forwarded = 0, echoes = 0;
  uint32_t arrivals = 0, injLoss = 0, injCorrupt = 0, injDup = 0;
  uint32_t dropCrc = 0, dropTtl = 0, dropNoRoute = 0, dropQueue = 0, dropDown = 0, malformed = 0;
  uint32_t overheard = 0, filtered = 0;
};

class Node : public IngressGate {
 public:
  Node(Clock& clock, Sink& sink) : clock_(clock), sink_(sink) {}

  void configure(const NodeConfig& cfg);
  const NodeConfig& config() const { return cfg_; }
  void setLink(ILink* link);
  FaultEngine& faults() { return faults_; }

  void start();
  void stop();
  bool running() const { return running_; }
  void setDown(bool down);  // node failure: silent in both directions
  bool down() const { return down_; }

  // Call as often as possible from the main loop.
  void tick();

  // IngressGate
  bool admit(const uint8_t* frame, size_t len, int8_t rssi) override;
  bool admitControl(uint8_t src) override;

  const NodeStats& stats() const { return stats_; }
  void logStats();
  void logRule(int src, const LinkRule& r, bool cleared = false);  // src < 0 = default rule

 private:
  struct FlowState {
    uint32_t seq = 0;
    uint64_t nextUs = 0;
    bool done = false;
  };

  void sendData(int flowIdx);
  void process(const uint8_t* frame, size_t len, int8_t rssi);
  bool transmit(uint8_t hopDst, const uint8_t* frame, size_t len);
  uint8_t nextHop(uint8_t finalDst) const;
  uint64_t nextInterval(int flowIdx, uint32_t seq) const;
  void logDrop(const char* why, const Header& h);

  Clock& clock_;
  Sink& sink_;
  ILink* link_ = nullptr;
  NodeConfig cfg_;
  FaultEngine faults_;
  DelayQueue queue_;
  NodeStats stats_;
  FlowState flowState_[NodeConfig::MAX_FLOWS];
  uint64_t runStartUs_ = 0;
  bool running_ = false;
  bool down_ = false;
  uint8_t txBuf_[MAX_FRAME];
  char line_[384];
};

}  // namespace fb
