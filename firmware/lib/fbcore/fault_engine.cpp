#include "fault_engine.h"

#include <string.h>

namespace fb {

FaultEngine::Compiled FaultEngine::compile(const LinkRule& r) {
  Compiled c;
  c.rule = r;
  c.tLoss = probThreshold(r.pLoss);
  c.tGB = probThreshold(r.geGoodToBad);
  c.tBG = probThreshold(r.geBadToGood);
  c.tLG = probThreshold(r.geLossGood);
  c.tLB = probThreshold(r.geLossBad);
  c.tCorrupt = probThreshold(r.pCorrupt);
  c.tDup = probThreshold(r.pDuplicate);
  return c;
}

void FaultEngine::reset(uint64_t seed, uint8_t self) {
  seed_ = seed;
  self_ = self;
  version_ = 0;
  default_ = compile(LinkRule());
  for (int i = 0; i < MAX_NODES; i++) links_[i] = LinkState();
}

// Changing a rule resets the Gilbert-Elliott state of the affected links to "good".
// The Python audit (faultbed/faults.py) mirrors this exactly.
void FaultEngine::setDefaultRule(const LinkRule& r) {
  default_ = compile(r);
  for (int i = 0; i < MAX_NODES; i++)
    if (!links_[i].hasOverride) links_[i].geBad = false;
  version_++;
}

void FaultEngine::setLinkRule(uint8_t src, const LinkRule& r) {
  if (!valid(src)) return;
  links_[src].hasOverride = true;
  links_[src].override_ = compile(r);
  links_[src].geBad = false;
  version_++;
}

void FaultEngine::clearLinkRule(uint8_t src) {
  if (!valid(src)) return;
  links_[src].hasOverride = false;
  links_[src].geBad = false;
  version_++;
}

Verdict FaultEngine::decide(uint8_t src, size_t frameLen) {
  Verdict v;
  v.ruleVersion = version_;
  if (!valid(src)) {
    v.lost = true;
    return v;
  }
  LinkState& ls = links_[src];
  const Compiled& c = ls.hasOverride ? ls.override_ : default_;
  const uint64_t idx = ls.arrivals++;
  v.index = (uint32_t)idx;

  uint64_t lossThreshold = 0;
  switch (c.rule.model) {
    case LOSS_BERNOULLI:
      lossThreshold = c.tLoss;
      break;
    case LOSS_GILBERT_ELLIOTT: {
      uint64_t r = draw(seed_, src, self_, STREAM_GE, idx);
      if (ls.geBad) {
        if (bernoulli(r, c.tBG)) ls.geBad = false;
      } else {
        if (bernoulli(r, c.tGB)) ls.geBad = true;
      }
      lossThreshold = ls.geBad ? c.tLB : c.tLG;
      break;
    }
    default:
      break;
  }
  v.geBad = ls.geBad;

  if (c.rule.blackout || bernoulli(draw(seed_, src, self_, STREAM_LOSS, idx), lossThreshold)) {
    v.lost = true;
    return v;
  }
  if (frameLen > 0 && bernoulli(draw(seed_, src, self_, STREAM_CORRUPT, idx), c.tCorrupt)) {
    v.corrupt = true;
    v.corruptBit = (uint16_t)(draw(seed_, src, self_, STREAM_CORRUPT_BIT, idx) % (frameLen * 8));
  }
  v.duplicate = bernoulli(draw(seed_, src, self_, STREAM_DUP, idx), c.tDup);
  v.delayUs = c.rule.delayUs;
  if (c.rule.jitterUs) v.delayUs += (uint32_t)(draw(seed_, src, self_, STREAM_JITTER, idx) % ((uint64_t)c.rule.jitterUs + 1));
  return v;
}

bool DelayQueue::push(uint64_t dueUs, uint8_t src, int8_t rssi, const uint8_t* data, size_t len) {
  if (len > MAX_FRAME) return false;
  for (int i = 0; i < SLOTS; i++) {
    Item& it = items_[i];
    if (it.used) continue;
    it.used = true;
    it.dueUs = dueUs;
    it.order = order_++;
    it.src = src;
    it.rssi = rssi;
    it.len = (uint16_t)len;
    memcpy(it.data, data, len);
    return true;
  }
  return false;
}

const DelayQueue::Item* DelayQueue::popDue(uint64_t nowUs) {
  int best = -1;
  for (int i = 0; i < SLOTS; i++) {
    const Item& it = items_[i];
    if (!it.used || it.dueUs > nowUs) continue;
    if (best < 0 || it.dueUs < items_[best].dueUs ||
        (it.dueUs == items_[best].dueUs && it.order < items_[best].order))
      best = i;
  }
  if (best < 0) return nullptr;
  out_ = items_[best];
  items_[best].used = false;
  return &out_;
}

int DelayQueue::size() const {
  int n = 0;
  for (int i = 0; i < SLOTS; i++) n += items_[i].used ? 1 : 0;
  return n;
}

void DelayQueue::clear() {
  for (int i = 0; i < SLOTS; i++) items_[i].used = false;
}

}  // namespace fb
