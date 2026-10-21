// FaultBed node firmware.
//
// Control plane: newline-delimited JSON over USB serial (out-of-band, so
// logging and control never compete with the radio being measured).
// Data plane: whichever transport the controller selects at run time.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <esp_timer.h>

#include "config.h"
#include "node.h"
#include "transport.h"

#ifndef FB_LED_PIN
#define FB_LED_PIN 2
#endif
#ifndef FB_BAUD
#define FB_BAUD 921600
#endif
#define FB_FW_VERSION "0.1.0"

class EspClock : public fb::Clock {
 public:
  uint64_t nowUs() override { return (uint64_t)esp_timer_get_time(); }
};

class SerialSink : public fb::Sink {
 public:
  void line(const char* s) override {
    Serial.write((const uint8_t*)s, strlen(s));
    Serial.write('\n');
  }
};

static EspClock clock_;
static SerialSink sink_;
static fb::Node node_(clock_, sink_);
static Transport* transport_ = nullptr;
static String transportCfg_;  // kept so a hard-killed node can bring its radio back
static bool hardKilled_ = false;
static uint64_t ledOffUs_ = 0;

static char lineBuf_[6144];
static size_t lineLen_ = 0;

static void reply(const char* cmd, bool ok, const char* err = nullptr) {
  char b[160];
  if (err) snprintf(b, sizeof b, "{\"e\":\"ack\",\"c\":\"%s\",\"ok\":%d,\"err\":\"%s\"}", cmd, ok ? 1 : 0, err);
  else snprintf(b, sizeof b, "{\"e\":\"ack\",\"c\":\"%s\",\"ok\":%d}", cmd, ok ? 1 : 0);
  sink_.line(b);
}

static void sendHello() {
  JsonDocument d;
  d["e"] = "hello";
  d["fw"] = FB_FW_VERSION;
  d["mac"] = WiFi.macAddress();
  d["chip"] = ESP.getChipModel();
  d["id"] = node_.config().id;
  d["heap"] = ESP.getFreeHeap();
  JsonArray tr = d["transports"].to<JsonArray>();
  for (int i = 0; i < registry::count(); i++) tr.add(registry::nameAt(i));
  String out;
  serializeJson(d, out);
  sink_.line(out.c_str());
}

static bool startTransport() {
  JsonDocument d;
  if (deserializeJson(d, transportCfg_)) return false;
  JsonObjectConst t = d.as<JsonObjectConst>();
  return transport_ && transport_->begin(t, node_.config().id);
}

static void applyRule(int src, JsonVariantConst rule) {
  fb::FaultEngine& f = node_.faults();
  if (rule.isNull()) {
    if (src < 0) f.setDefaultRule(fb::LinkRule());
    else f.clearLinkRule((uint8_t)src);
    node_.logRule(src, fb::LinkRule(), true);
    return;
  }
  fb::LinkRule r = parseLinkRule(rule.as<JsonObjectConst>());
  if (src < 0) f.setDefaultRule(r);
  else f.setLinkRule((uint8_t)src, r);
  node_.logRule(src, r);
}

static void cmdConfig(JsonObjectConst j) {
  fb::NodeConfig cfg;
  const char* err = "invalid";
  if (!parseNodeConfig(j, cfg, &err)) return reply("config", false, err);

  JsonObjectConst tj = j["transport"];
  const char* tname = tj["name"] | "";
  if (transport_) {
    transport_->end();
    delete transport_;
    transport_ = nullptr;
  }
  transport_ = registry::create(tname);
  if (!transport_) return reply("config", false, "unknown transport");

  node_.configure(cfg);
  node_.setLink(transport_);
  hardKilled_ = false;

  // Faults: default rule first, then per-link overrides. Each change bumps the rule version.
  JsonObjectConst faults = j["faults"];
  applyRule(-1, faults["default"]);
  for (JsonPairConst kv : faults["links"].as<JsonObjectConst>()) applyRule(atoi(kv.key().c_str()), kv.value());

  transportCfg_ = "";
  serializeJson(tj, transportCfg_);
  bool ok = startTransport();
  reply("config", ok, ok ? nullptr : "transport begin failed");
}

static void cmdPeers(JsonObjectConst j) {
  if (!transport_) return reply("peers", false, "no transport");
  for (JsonPairConst kv : j["peers"].as<JsonObjectConst>()) {
    int id = atoi(kv.key().c_str());
    Peer p;
    const char* mac = kv.value()["mac"] | "";
    const char* ip = kv.value()["ip"] | "";
    p.hasMac = parseMac(mac, p.mac);
    p.hasIp = strlen(ip) > 0 && p.ip.fromString(ip);
    transport_->setPeer((uint8_t)id, p);
  }
  transport_->onPeersChanged();
  reply("peers", true);
}

static void handleCommand(const char* line) {
  JsonDocument d;
  if (deserializeJson(d, line)) return reply("?", false, "bad json");
  JsonObjectConst j = d.as<JsonObjectConst>();
  const char* c = j["c"] | "";

  if (!strcmp(c, "hello")) {
    sendHello();
  } else if (!strcmp(c, "config")) {
    cmdConfig(j);
  } else if (!strcmp(c, "peers")) {
    cmdPeers(j);
  } else if (!strcmp(c, "start")) {
    node_.start();
    reply(c, true);
  } else if (!strcmp(c, "stop")) {
    node_.stop();
    node_.logStats();
    if (transport_) transport_->logCounters();
    reply(c, true);
  } else if (!strcmp(c, "kill")) {
    bool hard = j["hard"] | false;
    node_.setDown(true);
    if (hard && transport_) {
      transport_->end();  // radio really goes away: peers see a dead node, reconnection is real
      hardKilled_ = true;
    }
    reply(c, true);
  } else if (!strcmp(c, "revive")) {
    if (hardKilled_) {
      startTransport();
      hardKilled_ = false;
    }
    node_.setDown(false);
    reply(c, true);
  } else if (!strcmp(c, "fault")) {
    applyRule(j["src"] | -1, j["rule"]);
    reply(c, true);
  } else if (!strcmp(c, "sync")) {
    char b[96];
    snprintf(b, sizeof b, "{\"e\":\"sync\",\"h\":%lu,\"t\":%llu}", (unsigned long)(j["h"] | 0UL),
             (unsigned long long)clock_.nowUs());
    sink_.line(b);
  } else if (!strcmp(c, "stats")) {
    node_.logStats();
    if (transport_) transport_->logCounters();
  } else if (!strcmp(c, "led")) {
    digitalWrite(FB_LED_PIN, HIGH);
    ledOffUs_ = clock_.nowUs() + (uint64_t)(j["ms"] | 2000) * 1000ULL;
    reply(c, true);
  } else if (!strcmp(c, "reboot")) {
    reply(c, true);
    Serial.flush();
    ESP.restart();
  } else {
    reply(c, false, "unknown command");
  }
}

static void pollSerial() {
  while (Serial.available()) {
    int ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (lineLen_) {
        lineBuf_[lineLen_] = 0;
        handleCommand(lineBuf_);
        lineLen_ = 0;
      }
    } else if (lineLen_ < sizeof lineBuf_ - 1) {
      lineBuf_[lineLen_++] = (char)ch;
    } else {
      lineLen_ = 0;  // oversize line: drop it
      reply("?", false, "line too long");
    }
  }
}

void setup() {
  Serial.setRxBufferSize(8192);
  Serial.setTxBufferSize(8192);
  Serial.begin(FB_BAUD);
  pinMode(FB_LED_PIN, OUTPUT);
  digitalWrite(FB_LED_PIN, LOW);
  Transport::setSink(&sink_);
  WiFi.mode(WIFI_STA);  // so the MAC address is valid for hello
  delay(50);
  sendHello();
}

void loop() {
  pollSerial();
  if (transport_) transport_->loop();
  node_.tick();
  if (ledOffUs_ && clock_.nowUs() > ledOffUs_) {
    digitalWrite(FB_LED_PIN, LOW);
    ledOffUs_ = 0;
  }
}
