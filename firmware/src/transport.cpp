#include "transport.h"

#include <WiFi.h>
#include <esp_timer.h>
#include <string.h>

fb::Sink* Transport::sink_ = nullptr;

uint64_t Transport::nowUs() { return (uint64_t)esp_timer_get_time(); }

void Transport::setPeer(uint8_t id, const Peer& p) {
  if (id == 0 || id >= MAX_PEERS) return;
  peers_[id] = p;
}

const Peer* Transport::peer(uint8_t id) const {
  if (id == 0 || id >= MAX_PEERS) return nullptr;
  return &peers_[id];
}

int8_t Transport::peerIdByIp(const IPAddress& ip) const {
  for (int i = 1; i < MAX_PEERS; i++)
    if (peers_[i].hasIp && peers_[i].ip == ip) return (int8_t)i;
  return -1;
}

void Transport::emit(const char* json) {
  if (sink_) sink_->line(json);
}

void Transport::reportLink(bool up, const char* detail) {
  if (up == linkUp_) return;
  linkUp_ = up;
  if (up) counters_.linkUps++;
  else counters_.linkDowns++;
  char buf[200];
  if (up && WiFi.status() == WL_CONNECTED) {
    snprintf(buf, sizeof buf, "{\"e\":\"link\",\"t\":%llu,\"tr\":\"%s\",\"up\":1,\"ip\":\"%s\",\"ch\":%d,\"wrssi\":%d%s%s%s}",
             (unsigned long long)nowUs(), name(), WiFi.localIP().toString().c_str(), WiFi.channel(), WiFi.RSSI(),
             detail ? ",\"info\":\"" : "", detail ? detail : "", detail ? "\"" : "");
  } else {
    snprintf(buf, sizeof buf, "{\"e\":\"link\",\"t\":%llu,\"tr\":\"%s\",\"up\":%d%s%s%s}", (unsigned long long)nowUs(),
             name(), up ? 1 : 0, detail ? ",\"info\":\"" : "", detail ? detail : "", detail ? "\"" : "");
  }
  emit(buf);
}

void Transport::logCounters() {
  char buf[220];
  snprintf(buf, sizeof buf,
           "{\"e\":\"tstats\",\"t\":%llu,\"tr\":\"%s\",\"ok\":%lu,\"err\":%lu,\"ups\":%lu,\"downs\":%lu,\"retx\":%lu,"
           "\"giveup\":%lu,\"rxovf\":%lu}",
           (unsigned long long)nowUs(), name(), (unsigned long)counters_.sendOk, (unsigned long)counters_.sendErr,
           (unsigned long)counters_.linkUps, (unsigned long)counters_.linkDowns, (unsigned long)counters_.retransmits,
           (unsigned long)counters_.giveUps, (unsigned long)counters_.rxOverflow);
  emit(buf);
}

// ---- RxRing ----
bool RxRing::init(int depth) {
  if (!q_) q_ = xQueueCreate(depth, sizeof(Item));
  return q_ != nullptr;
}

void RxRing::deinit() {
  if (q_) vQueueDelete(q_);
  q_ = nullptr;
}

bool RxRing::pushFromTask(const uint8_t* data, size_t len, int8_t rssi, uint8_t src) {
  if (!q_ || len > fb::MAX_FRAME) return false;
  Item it;
  it.len = (uint8_t)len;
  it.rssi = rssi;
  it.src = src;
  memcpy(it.data, data, len);
  return xQueueSend(q_, &it, 0) == pdTRUE;
}

bool RxRing::pop(Item& out) { return q_ && xQueueReceive(q_, &out, 0) == pdTRUE; }

// ---- Wi-Fi ----
namespace wifi {
static String ssid_, pass_;

void start(JsonObjectConst cfg) {
  JsonObjectConst w = cfg["wifi"];
  ssid_ = (const char*)(w["ssid"] | "");
  pass_ = (const char*)(w["pass"] | "");
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // modem sleep adds 100+ ms of latency jitter
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid_.c_str(), pass_.c_str());
}

void stop() {
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
}

bool connected() { return WiFi.status() == WL_CONNECTED; }
}  // namespace wifi

// ---- registry ----
namespace registry {
struct Entry {
  const char* name;
  TransportFactory f;
};
static Entry* entries() {
  static Entry e[16];
  return e;
}
static int& n() {
  static int c = 0;
  return c;
}

void add(const char* name, TransportFactory f) {
  if (n() < 16) entries()[n()++] = {name, f};
}

Transport* create(const char* name) {
  for (int i = 0; i < n(); i++)
    if (strcmp(entries()[i].name, name) == 0) return entries()[i].f();
  return nullptr;
}

int count() { return n(); }
const char* nameAt(int i) { return (i >= 0 && i < n()) ? entries()[i].name : nullptr; }
}  // namespace registry
