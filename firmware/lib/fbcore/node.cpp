#include "node.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

namespace fb {

typedef unsigned long long ull;

void Node::configure(const NodeConfig& cfg) {
  cfg_ = cfg;
  faults_.reset(cfg.seed, cfg.id);
  queue_.clear();
  stats_ = NodeStats();
  running_ = false;
  down_ = false;
  for (int i = 0; i < NodeConfig::MAX_FLOWS; i++) flowState_[i] = FlowState();
}

void Node::setLink(ILink* link) {
  link_ = link;
  if (link_) link_->setGate(this);
}

void Node::start() {
  runStartUs_ = clock_.nowUs();
  for (int i = 0; i < cfg_.flowCount; i++) {
    flowState_[i] = FlowState();
    flowState_[i].nextUs = runStartUs_ + (uint64_t)cfg_.flows[i].startMs * 1000ULL;
  }
  running_ = true;
  snprintf(line_, sizeof line_, "{\"e\":\"start\",\"t\":%llu}", (ull)runStartUs_);
  sink_.line(line_);
}

void Node::stop() {
  running_ = false;
  snprintf(line_, sizeof line_, "{\"e\":\"stop\",\"t\":%llu}", (ull)clock_.nowUs());
  sink_.line(line_);
}

void Node::setDown(bool d) {
  down_ = d;
  if (d) queue_.clear();  // a failed node loses whatever it was holding
  snprintf(line_, sizeof line_, "{\"e\":\"%s\",\"t\":%llu}", d ? "down" : "up", (ull)clock_.nowUs());
  sink_.line(line_);
}

uint8_t Node::nextHop(uint8_t finalDst) const {
  if (finalDst >= FaultEngine::MAX_NODES) return 0;
  uint8_t r = cfg_.routes[finalDst];
  return r ? r : finalDst;
}

uint64_t Node::nextInterval(int i, uint32_t seq) const {
  const Flow& f = cfg_.flows[i];
  uint64_t meanUs = (uint64_t)f.intervalMs * 1000ULL;
  if (!f.poisson) return meanUs;
  // Exponential inter-arrival from a counter-based draw: reproducible per (seed, node, flow, seq).
  uint64_t r = draw(cfg_.seed, cfg_.id, f.id, STREAM_INTERVAL, seq);
  double u = (double)(r >> 11) / 9007199254740992.0;  // [0,1)
  double x = -log(1.0 - u) * (double)meanUs;
  if (x > 20.0 * meanUs) x = 20.0 * meanUs;
  return (uint64_t)x;
}

void Node::tick() {
  uint64_t now = clock_.nowUs();
  // 1. Release delayed / queued arrivals.
  const DelayQueue::Item* it;
  while ((it = queue_.popDue(now)) != nullptr) process(it->data, it->len, it->rssi);

  // 2. Generate traffic.
  if (!running_ || down_) return;
  for (int i = 0; i < cfg_.flowCount; i++) {
    FlowState& fs = flowState_[i];
    if (fs.done || now < fs.nextUs) continue;
    sendData(i);
    fs.nextUs += nextInterval(i, fs.seq);
    if (fs.nextUs < now) fs.nextUs = now;  // never burst to catch up after a stall
    if (cfg_.flows[i].count && fs.seq >= cfg_.flows[i].count) fs.done = true;
  }
}

bool Node::transmit(uint8_t hopDst, const uint8_t* frame, size_t len) {
  if (!link_ || !link_->isUp()) return false;
  return link_->send(hopDst, frame, len);
}

void Node::sendData(int i) {
  const Flow& f = cfg_.flows[i];
  FlowState& fs = flowState_[i];
  uint64_t now = clock_.nowUs();
  Header h;
  h.type = FT_DATA;
  h.flags = f.echo ? FF_ECHO_REQUEST : 0;
  h.origin = cfg_.id;
  h.finalDst = f.dst;
  h.hopSrc = cfg_.id;
  h.hopDst = nextHop(f.dst);
  h.ttl = cfg_.ttl;
  h.hops = 0;
  h.flow = f.id;
  h.seq = fs.seq++;
  h.originUs = (uint32_t)now;
  h.payloadLen = f.size > MAX_PAYLOAD ? MAX_PAYLOAD : f.size;
  size_t len = encodeFrame(h, nullptr, txBuf_, sizeof txBuf_);
  bool ok = len && transmit(h.hopDst, txBuf_, len);
  stats_.generated++;
  if (!ok) stats_.sendFail++;
  snprintf(line_, sizeof line_, "{\"e\":\"tx\",\"t\":%llu,\"f\":%u,\"s\":%lu,\"d\":%u,\"n\":%u,\"l\":%u,\"ok\":%d}",
           (ull)now, f.id, (unsigned long)h.seq, f.dst, h.hopDst, (unsigned)len, ok ? 1 : 0);
  sink_.line(line_);
}

bool Node::admit(const uint8_t* frame, size_t len, int8_t rssi) {
  Header h;
  DecodeResult dr = decodeFrame(frame, len, h);
  if (dr != DECODE_OK) {  // natural corruption or foreign traffic; no trustworthy link id
    stats_.malformed++;
    return false;
  }
  if (h.hopDst != cfg_.id && h.hopDst != BROADCAST) {  // overheard on a shared medium
    stats_.overheard++;
    return false;
  }
  if (h.hopSrc == 0 || h.hopSrc >= FaultEngine::MAX_NODES ||
      (cfg_.neighborFilter && !cfg_.neighbors[h.hopSrc])) {  // not a neighbour in the virtual topology
    stats_.filtered++;
    return false;
  }
  if (down_) {
    stats_.dropDown++;
    return false;
  }

  Verdict v = faults_.decide(h.hopSrc, len);
  stats_.arrivals++;
  const char* verdict = v.lost ? "loss" : v.corrupt ? "cor" : v.duplicate ? "dup" : "ok";
  snprintf(line_, sizeof line_,
           "{\"e\":\"arr\",\"t\":%llu,\"src\":%u,\"i\":%lu,\"rv\":%u,\"v\":\"%s\",\"dl\":%lu,\"o\":%u,\"f\":%u,\"s\":%lu,"
           "\"ty\":%u,\"rssi\":%d}",
           (ull)clock_.nowUs(), h.hopSrc, (unsigned long)v.index, v.ruleVersion, verdict, (unsigned long)v.delayUs,
           h.origin, h.flow, (unsigned long)h.seq, h.type, rssi);
  sink_.line(line_);

  if (v.lost) {
    stats_.injLoss++;
    return false;
  }
  uint64_t due = clock_.nowUs() + v.delayUs;
  if (v.corrupt) {
    stats_.injCorrupt++;
    uint8_t tmp[MAX_FRAME];
    memcpy(tmp, frame, len);
    tmp[v.corruptBit / 8] ^= (uint8_t)(1u << (v.corruptBit % 8));
    if (!queue_.push(due, h.hopSrc, rssi, tmp, len)) logDrop("queue", h);
    return false;  // a corrupted frame would fail the link checksum: not acknowledged
  }
  if (!queue_.push(due, h.hopSrc, rssi, frame, len)) {
    stats_.dropQueue++;
    logDrop("queue", h);
    return true;
  }
  if (v.duplicate) {
    stats_.injDup++;
    if (!queue_.push(due, h.hopSrc, rssi, frame, len)) stats_.dropQueue++;
  }
  return true;
}

bool Node::admitControl(uint8_t src) {
  if (down_ || src == 0 || src >= FaultEngine::MAX_NODES) return false;
  if (cfg_.neighborFilter && !cfg_.neighbors[src]) return false;
  Verdict v = faults_.decide(src, 0);
  snprintf(line_, sizeof line_, "{\"e\":\"arr\",\"t\":%llu,\"src\":%u,\"i\":%lu,\"rv\":%u,\"v\":\"%s\",\"ctl\":1}",
           (ull)clock_.nowUs(), src, (unsigned long)v.index, v.ruleVersion, v.lost ? "loss" : "ok");
  sink_.line(line_);
  if (v.lost) stats_.injLoss++;
  return !v.lost;
}

void Node::logDrop(const char* why, const Header& h) {
  snprintf(line_, sizeof line_, "{\"e\":\"drop\",\"t\":%llu,\"why\":\"%s\",\"o\":%u,\"f\":%u,\"s\":%lu,\"ty\":%u}",
           (ull)clock_.nowUs(), why, h.origin, h.flow, (unsigned long)h.seq, h.type);
  sink_.line(line_);
}

void Node::process(const uint8_t* frame, size_t len, int8_t rssi) {
  (void)rssi;
  Header h;
  if (decodeFrame(frame, len, h) != DECODE_OK) {
    stats_.dropCrc++;
    snprintf(line_, sizeof line_, "{\"e\":\"drop\",\"t\":%llu,\"why\":\"crc\"}", (ull)clock_.nowUs());
    sink_.line(line_);
    return;
  }
  if (down_) {
    stats_.dropDown++;
    return;
  }
  uint64_t now = clock_.nowUs();

  if (h.finalDst == cfg_.id) {
    if (h.type == FT_ECHO_REPLY) {
      stats_.echoes++;
      uint32_t rtt = (uint32_t)now - h.originUs;
      snprintf(line_, sizeof line_, "{\"e\":\"rtt\",\"t\":%llu,\"f\":%u,\"s\":%lu,\"rtt\":%lu,\"hops\":%u}", (ull)now,
               h.flow, (unsigned long)h.seq, (unsigned long)rtt, h.hops + 1);
      sink_.line(line_);
      return;
    }
    stats_.delivered++;
    snprintf(line_, sizeof line_, "{\"e\":\"rx\",\"t\":%llu,\"o\":%u,\"f\":%u,\"s\":%lu,\"l\":%u,\"hops\":%u}", (ull)now,
             h.origin, h.flow, (unsigned long)h.seq, h.payloadLen, h.hops + 1);
    sink_.line(line_);
    if (h.flags & FF_ECHO_REQUEST) {
      Header r = h;
      r.type = FT_ECHO_REPLY;
      r.flags = 0;
      r.origin = cfg_.id;
      r.finalDst = h.origin;
      r.hopSrc = cfg_.id;
      r.hopDst = nextHop(h.origin);
      r.ttl = cfg_.ttl;
      r.hops = 0;
      size_t n = encodeFrame(r, frame + HEADER_LEN, txBuf_, sizeof txBuf_);
      bool ok = n && transmit(r.hopDst, txBuf_, n);
      if (!ok) stats_.sendFail++;
      snprintf(line_, sizeof line_, "{\"e\":\"etx\",\"t\":%llu,\"o\":%u,\"f\":%u,\"s\":%lu,\"n\":%u,\"ok\":%d}",
               (ull)clock_.nowUs(), h.origin, h.flow, (unsigned long)h.seq, r.hopDst, ok ? 1 : 0);
      sink_.line(line_);
    }
    return;
  }

  // Forward towards the final destination.
  if (h.ttl <= 1) {
    stats_.dropTtl++;
    logDrop("ttl", h);
    return;
  }
  uint8_t nh = nextHop(h.finalDst);
  if (nh == 0 || nh == cfg_.id || nh >= FaultEngine::MAX_NODES) {
    stats_.dropNoRoute++;
    logDrop("noroute", h);
    return;
  }
  memcpy(txBuf_, frame, len);
  rewriteHop(txBuf_, len, cfg_.id, nh, h.ttl - 1, h.hops + 1);
  bool ok = transmit(nh, txBuf_, len);
  if (ok) stats_.forwarded++;
  else stats_.sendFail++;
  snprintf(line_, sizeof line_, "{\"e\":\"fwd\",\"t\":%llu,\"o\":%u,\"f\":%u,\"s\":%lu,\"ty\":%u,\"n\":%u,\"ok\":%d}",
           (ull)now, h.origin, h.flow, (unsigned long)h.seq, h.type, nh, ok ? 1 : 0);
  sink_.line(line_);
}

void Node::logStats() {
  const NodeStats& s = stats_;
  snprintf(line_, sizeof line_,
           "{\"e\":\"stats\",\"t\":%llu,\"gen\":%lu,\"sendfail\":%lu,\"rx\":%lu,\"fwd\":%lu,\"echo\":%lu,\"arr\":%lu,"
           "\"loss\":%lu,\"cor\":%lu,\"dup\":%lu,\"crc\":%lu,\"ttl\":%lu,\"noroute\":%lu,\"queue\":%lu,\"down\":%lu,"
           "\"malformed\":%lu,\"overheard\":%lu,\"filtered\":%lu}",
           (ull)clock_.nowUs(), (unsigned long)s.generated, (unsigned long)s.sendFail, (unsigned long)s.delivered,
           (unsigned long)s.forwarded, (unsigned long)s.echoes, (unsigned long)s.arrivals, (unsigned long)s.injLoss,
           (unsigned long)s.injCorrupt, (unsigned long)s.injDup, (unsigned long)s.dropCrc, (unsigned long)s.dropTtl,
           (unsigned long)s.dropNoRoute, (unsigned long)s.dropQueue, (unsigned long)s.dropDown,
           (unsigned long)s.malformed, (unsigned long)s.overheard, (unsigned long)s.filtered);
  sink_.line(line_);
}

void Node::logRule(int src, const LinkRule& r, bool cleared) {
  snprintf(line_, sizeof line_,
           "{\"e\":\"rule\",\"t\":%llu,\"rv\":%u,\"src\":%d,\"model\":%u,\"p\":%.17g,\"gb\":%.17g,\"bg\":%.17g,\"lg\":%.17g,"
           "\"lb\":%.17g,\"cor\":%.17g,\"dup\":%.17g,\"delay\":%lu,\"jit\":%lu,\"bo\":%d,\"clr\":%d}",
           (ull)clock_.nowUs(), faults_.ruleVersion(), src, r.model, r.pLoss, r.geGoodToBad, r.geBadToGood,
           r.geLossGood, r.geLossBad, r.pCorrupt, r.pDuplicate, (unsigned long)r.delayUs, (unsigned long)r.jitterUs,
           r.blackout ? 1 : 0, cleared ? 1 : 0);
  sink_.line(line_);
}

}  // namespace fb
