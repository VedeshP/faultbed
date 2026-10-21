// Per-link ingress fault injection.
//
// Faults are applied on the *receiving* side of each directed link (src -> me),
// right after the transport hands over a frame and before any protocol-level
// acknowledgement where the transport allows it (see CoAP). Each arrival on a
// link gets an index; the verdict is a pure function of (seed, src, me, index)
// and the active rule, which is what makes runs reproducible and auditable.
#pragma once
#include <stddef.h>
#include <stdint.h>

#include "fb_hash.h"
#include "packet.h"

namespace fb {

enum LossModel : uint8_t { LOSS_NONE = 0, LOSS_BERNOULLI = 1, LOSS_GILBERT_ELLIOTT = 2 };

struct LinkRule {
  LossModel model = LOSS_NONE;
  double pLoss = 0;         // Bernoulli loss probability
  double geGoodToBad = 0;   // Gilbert-Elliott transition probabilities
  double geBadToGood = 1;
  double geLossGood = 0;    // loss probability inside each state
  double geLossBad = 1;
  double pCorrupt = 0;      // flip one bit
  double pDuplicate = 0;    // deliver twice
  uint32_t delayUs = 0;     // fixed extra latency
  uint32_t jitterUs = 0;    // + uniform [0, jitterUs]
  bool blackout = false;    // drop everything (link cut)
};

struct Verdict {
  uint32_t index = 0;
  bool lost = false;
  bool corrupt = false;
  uint16_t corruptBit = 0;
  bool duplicate = false;
  uint32_t delayUs = 0;
  bool geBad = false;
  uint16_t ruleVersion = 0;
};

class FaultEngine {
 public:
  static const int MAX_NODES = 32;  // node ids 1..31

  void reset(uint64_t seed, uint8_t self);
  void setDefaultRule(const LinkRule& r);
  void setLinkRule(uint8_t src, const LinkRule& r);
  void clearLinkRule(uint8_t src);
  uint16_t ruleVersion() const { return version_; }

  // Decide the fate of the next frame arriving from `src`. frameLen bounds corruptBit.
  Verdict decide(uint8_t src, size_t frameLen);

  uint32_t arrivals(uint8_t src) const { return valid(src) ? links_[src].arrivals : 0; }

 private:
  struct Compiled {
    LinkRule rule;
    uint64_t tLoss, tGB, tBG, tLG, tLB, tCorrupt, tDup;
  };
  struct LinkState {
    bool hasOverride = false;
    Compiled override_;
    uint32_t arrivals = 0;
    bool geBad = false;
  };
  static bool valid(uint8_t id) { return id > 0 && id < MAX_NODES; }
  static Compiled compile(const LinkRule& r);

  uint64_t seed_ = 0;
  uint8_t self_ = 0;
  uint16_t version_ = 0;
  Compiled default_ = compile(LinkRule());
  LinkState links_[MAX_NODES];
};

// Fixed-capacity holding area for delayed frames (no heap, no blocking).
class DelayQueue {
 public:
  static const int SLOTS = 24;
  struct Item {
    bool used = false;
    uint64_t dueUs = 0;
    uint32_t order = 0;
    uint8_t src = 0;
    int8_t rssi = 0;
    uint16_t len = 0;
    uint8_t data[MAX_FRAME];
  };
  bool push(uint64_t dueUs, uint8_t src, int8_t rssi, const uint8_t* data, size_t len);
  // Pops the earliest item with dueUs <= now. Returns nullptr if none (item stays valid until next push/pop).
  const Item* popDue(uint64_t nowUs);
  int size() const;
  void clear();

 private:
  Item items_[SLOTS];
  Item out_;
  uint32_t order_ = 0;
};

}  // namespace fb
