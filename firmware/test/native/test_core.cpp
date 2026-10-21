// Host-side unit tests for the portable firmware core (no Arduino, no board).
// Build & run:  python firmware/test/run_native_tests.py
#include <math.h>
#include <stdio.h>
#include <string.h>

#include <string>
#include <vector>

#include "coap.h"
#include "fault_engine.h"
#include "fb_hash.h"
#include "node.h"
#include "packet.h"

using namespace fb;

static int failures = 0, checks = 0;
#define CHECK(cond)                                                    \
  do {                                                                 \
    checks++;                                                          \
    if (!(cond)) {                                                     \
      failures++;                                                      \
      printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);         \
    }                                                                  \
  } while (0)

static void test_crc_and_frame() {
  const char* s = "123456789";
  CHECK(crc16((const uint8_t*)s, 9) == 0x29B1);  // CRC-16/CCITT-FALSE check value

  Header h = {};
  h.type = FT_DATA; h.origin = 1; h.finalDst = 3; h.hopSrc = 1; h.hopDst = 2;
  h.ttl = 8; h.flow = 4; h.seq = 123456; h.originUs = 0xDEADBEEF; h.payloadLen = 10;
  uint8_t buf[MAX_FRAME];
  size_t n = encodeFrame(h, nullptr, buf, sizeof buf);
  CHECK(n == HEADER_LEN + 10);
  Header d;
  CHECK(decodeFrame(buf, n, d) == DECODE_OK);
  CHECK(d.seq == 123456 && d.originUs == 0xDEADBEEF && d.flow == 4 && d.hopDst == 2);

  int detected = 0;
  for (size_t bit = 0; bit < n * 8; bit++) {
    uint8_t tmp[MAX_FRAME];
    memcpy(tmp, buf, n);
    tmp[bit / 8] ^= (uint8_t)(1 << (bit % 8));
    if (decodeFrame(tmp, n, d) != DECODE_OK) detected++;
  }
  CHECK(detected == (int)(n * 8));  // every single-bit error is caught

  rewriteHop(buf, n, 2, 3, 7, 1);
  CHECK(decodeFrame(buf, n, d) == DECODE_OK && d.hopSrc == 2 && d.hopDst == 3 && d.ttl == 7);
  CHECK(encodeFrame(h, nullptr, buf, HEADER_LEN + 5) == 0);
}

static void test_bernoulli_accuracy_and_determinism() {
  const double ps[] = {0.0, 0.01, 0.1, 0.3, 0.5, 0.9, 1.0};
  for (double p : ps) {
    FaultEngine a, b;
    a.reset(1234, 2);
    b.reset(1234, 2);
    LinkRule r;
    r.model = LOSS_BERNOULLI;
    r.pLoss = p;
    a.setDefaultRule(r);
    b.setDefaultRule(r);
    const int N = 100000;
    int lost = 0, same = 0;
    for (int i = 0; i < N; i++) {
      Verdict va = a.decide(1, 40), vb = b.decide(1, 40);
      lost += va.lost;
      same += va.lost == vb.lost;
    }
    double measured = (double)lost / N;
    CHECK(same == N);
    CHECK(fabs(measured - p) < 0.006);
    printf("  bernoulli p=%.2f measured=%.4f\n", p, measured);
  }
  // Different seeds give different (but equally accurate) patterns.
  FaultEngine a, b;
  a.reset(1, 2);
  b.reset(2, 2);
  LinkRule r;
  r.model = LOSS_BERNOULLI;
  r.pLoss = 0.5;
  a.setDefaultRule(r);
  b.setDefaultRule(r);
  int diff = 0;
  for (int i = 0; i < 1000; i++) diff += a.decide(1, 40).lost != b.decide(1, 40).lost;
  CHECK(diff > 400 && diff < 600);
}

static void test_independent_links() {
  // Traffic on link 1->2 must not change the decisions on link 3->2.
  FaultEngine a, b;
  a.reset(7, 2);
  b.reset(7, 2);
  LinkRule r;
  r.model = LOSS_BERNOULLI;
  r.pLoss = 0.4;
  a.setDefaultRule(r);
  b.setDefaultRule(r);
  for (int i = 0; i < 500; i++) a.decide(1, 40);
  int same = 0;
  for (int i = 0; i < 1000; i++) same += a.decide(3, 40).lost == b.decide(3, 40).lost;
  CHECK(same == 1000);
}

static void test_gilbert_elliott() {
  FaultEngine e;
  e.reset(99, 2);
  LinkRule r;
  r.model = LOSS_GILBERT_ELLIOTT;
  r.geGoodToBad = 0.05;
  r.geBadToGood = 0.25;
  r.geLossGood = 0.01;
  r.geLossBad = 0.8;
  e.setDefaultRule(r);
  const int N = 200000;
  int lost = 0, bursts = 0, burstLen = 0, totalBurst = 0;
  bool prev = false;
  for (int i = 0; i < N; i++) {
    bool l = e.decide(1, 40).lost;
    lost += l;
    if (l) burstLen++;
    if (!l && prev) { bursts++; totalBurst += burstLen; burstLen = 0; }
    prev = l;
  }
  double piBad = r.geGoodToBad / (r.geGoodToBad + r.geBadToGood);
  double expected = piBad * r.geLossBad + (1 - piBad) * r.geLossGood;
  double measured = (double)lost / N;
  printf("  gilbert-elliott expected=%.4f measured=%.4f mean_burst=%.2f\n", expected, measured,
         (double)totalBurst / bursts);
  CHECK(fabs(measured - expected) < 0.01);
  CHECK((double)totalBurst / bursts > 1.5);  // losses really are bursty
}

static void test_corrupt_dup_delay() {
  FaultEngine e;
  e.reset(5, 2);
  LinkRule r;
  r.pCorrupt = 0.2;
  r.pDuplicate = 0.1;
  r.delayUs = 5000;
  r.jitterUs = 2000;
  e.setDefaultRule(r);
  int cor = 0, dup = 0;
  uint32_t minD = 0xFFFFFFFF, maxD = 0;
  for (int i = 0; i < 50000; i++) {
    Verdict v = e.decide(1, 40);
    cor += v.corrupt;
    dup += v.duplicate;
    if (v.corrupt) CHECK(v.corruptBit < 320);
    if (v.delayUs < minD) minD = v.delayUs;
    if (v.delayUs > maxD) maxD = v.delayUs;
  }
  CHECK(fabs(cor / 50000.0 - 0.2) < 0.01);
  CHECK(fabs(dup / 50000.0 - 0.1) < 0.01);
  CHECK(minD >= 5000 && maxD <= 7000 && maxD > 6900);
}

static void test_delay_queue() {
  DelayQueue q;
  uint8_t d[4] = {1, 2, 3, 4};
  CHECK(q.push(300, 1, 0, d, 1));
  CHECK(q.push(100, 2, 0, d, 2));
  CHECK(q.push(100, 3, 0, d, 3));
  CHECK(q.popDue(50) == nullptr);
  const DelayQueue::Item* it = q.popDue(1000);
  CHECK(it && it->src == 2);  // earliest, then FIFO among equals
  it = q.popDue(1000);
  CHECK(it && it->src == 3);
  it = q.popDue(1000);
  CHECK(it && it->src == 1);
  for (int i = 0; i < DelayQueue::SLOTS; i++) CHECK(q.push(1, 1, 0, d, 1));
  CHECK(!q.push(1, 1, 0, d, 1));
}

static void test_coap() {
  uint8_t payload[30];
  for (int i = 0; i < 30; i++) payload[i] = (uint8_t)i;
  coap::Message m;
  m.type = coap::CON;
  m.code = coap::CODE_POST;
  m.messageId = 0xBEEF;
  m.payload = payload;
  m.payloadLen = 30;
  uint8_t buf[64];
  size_t n = coap::encode(m, buf, sizeof buf);
  CHECK(n == 4 + 3 + 1 + 30);
  coap::Message d;
  CHECK(coap::decode(buf, n, d));
  CHECK(d.type == coap::CON && d.code == coap::CODE_POST && d.messageId == 0xBEEF && d.payloadLen == 30);
  CHECK(memcmp(d.payload, payload, 30) == 0);

  coap::Message ack;
  ack.type = coap::ACK;
  ack.messageId = 0xBEEF;
  n = coap::encode(ack, buf, sizeof buf);
  CHECK(n == 4);
  CHECK(coap::decode(buf, n, d) && d.type == coap::ACK && d.payloadLen == 0);
  uint8_t bad[] = {0x80, 0, 0, 0};
  CHECK(!coap::decode(bad, 4, d));
}

// ---- end-to-end: three nodes over an in-memory link, line topology 1 - 2 - 3 ----
struct FakeClock : Clock {
  uint64_t t = 1000;
  uint64_t nowUs() override { return t; }
};
struct CaptureSink : Sink {
  std::vector<std::string> lines;
  void line(const char* s) override { lines.push_back(s); }
  int count(const char* ev) const {
    std::string key = std::string("\"e\":\"") + ev + "\"";
    int n = 0;
    for (auto& l : lines) n += l.find(key) != std::string::npos;
    return n;
  }
};
struct Bus;
struct BusLink : ILink {
  Bus* bus;
  uint8_t self;
  bool send(uint8_t hopDst, const uint8_t* frame, size_t len) override;
  bool isUp() const override { return true; }
  IngressGate* gate() { return gate_; }
};
struct Bus {
  BusLink* links[4] = {};
  struct Pending { uint8_t dst; std::vector<uint8_t> f; };
  std::vector<Pending> pending;
  void flush() {
    std::vector<Pending> p;
    p.swap(pending);
    for (auto& x : p)
      for (int i = 1; i <= 3; i++)  // shared medium: everyone hears everything
        if (links[i] && i != x.f[6]) links[i]->gate()->admit(x.f.data(), x.f.size(), -40);
  }
};
bool BusLink::send(uint8_t hopDst, const uint8_t* frame, size_t len) {
  bus->pending.push_back({hopDst, std::vector<uint8_t>(frame, frame + len)});
  return true;
}

static void test_three_node_line() {
  FakeClock clk;
  CaptureSink s1, s2, s3;
  Node n1(clk, s1), n2(clk, s2), n3(clk, s3);
  Bus bus;
  BusLink l1, l2, l3;
  l1.bus = l2.bus = l3.bus = &bus;
  bus.links[1] = &l1; bus.links[2] = &l2; bus.links[3] = &l3;

  NodeConfig c1, c2, c3;
  c1.id = 1; c2.id = 2; c3.id = 3;
  c1.neighbors[2] = true;
  c2.neighbors[1] = c2.neighbors[3] = true;
  c3.neighbors[2] = true;
  c1.routes[3] = 2;
  c3.routes[1] = 2;
  c1.flows[0].id = 1; c1.flows[0].dst = 3; c1.flows[0].intervalMs = 100; c1.flows[0].count = 50;
  c1.flowCount = 1;
  n1.configure(c1); n2.configure(c2); n3.configure(c3);
  n1.setLink(&l1); n2.setLink(&l2); n3.setLink(&l3);

  LinkRule lossy;
  lossy.model = LOSS_BERNOULLI;
  lossy.pLoss = 0.2;
  n3.faults().setLinkRule(2, lossy);  // only the 2 -> 3 direction is lossy

  n1.start(); n2.start(); n3.start();
  for (int step = 0; step < 6000; step++) {
    clk.t += 1000;
    n1.tick(); n2.tick(); n3.tick();
    bus.flush();
  }
  int rx = s3.count("rx"), rtt = s1.count("rtt"), fwd = s2.count("fwd");
  printf("  line 1-2-3: tx=%d fwd@2=%d rx@3=%d rtt@1=%d overheard@3=%u\n", s1.count("tx"), fwd, rx, rtt,
         n3.stats().overheard);

  // Virtual topology: a frame from 1 addressed straight to 3 is rejected because 1 is not 3's neighbour.
  Header h = {};
  h.type = FT_DATA; h.origin = 1; h.finalDst = 3; h.hopSrc = 1; h.hopDst = 3; h.ttl = 4; h.payloadLen = 4;
  uint8_t f[MAX_FRAME];
  size_t fl = encodeFrame(h, nullptr, f, sizeof f);
  CHECK(!n3.admit(f, fl, -40));
  CHECK(n3.stats().filtered == 1);
  CHECK(s1.count("tx") == 50);
  CHECK(rx > 30 && rx < 50);           // ~20% loss on 2->3
  CHECK(rtt == rx);                    // reverse path is clean
  CHECK(n3.stats().overheard > 0);     // 3 physically heard 1's frames addressed to 2 and ignored them
  CHECK(n3.stats().injLoss + rx == 50);

  n2.setDown(true);
  size_t before = s3.lines.size();
  NodeConfig again = c1;
  n1.configure(again);
  n1.start();
  for (int step = 0; step < 2000; step++) { clk.t += 1000; n1.tick(); n2.tick(); n3.tick(); bus.flush(); }
  int rxAfter = 0;
  for (size_t i = before; i < s3.lines.size(); i++) rxAfter += s3.lines[i].find("\"e\":\"rx\"") != std::string::npos;
  CHECK(rxAfter == 0);  // relay is down: nothing gets through
}

static void print_vectors() {
  // Cross-language test vectors, mirrored in controller/tests/test_hashing.py
  printf("  vectors:");
  for (uint64_t i = 0; i < 3; i++) printf(" %llu", (unsigned long long)draw(42, 1, 2, STREAM_LOSS, i));
  printf(" thr(0.3)=%llu\n", (unsigned long long)probThreshold(0.3));
}

int main() {
  printf("fbcore native tests\n");
  test_crc_and_frame();
  test_bernoulli_accuracy_and_determinism();
  test_independent_links();
  test_gilbert_elliott();
  test_corrupt_dup_delay();
  test_delay_queue();
  test_coap();
  test_three_node_line();
  print_vectors();
  printf("%d checks, %d failures\n", checks, failures);
  return failures ? 1 : 0;
}
