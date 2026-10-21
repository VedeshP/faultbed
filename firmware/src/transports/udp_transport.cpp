// Raw UDP over Wi-Fi (via the access point). No acknowledgements, no retries:
// the baseline "best effort" IP transport.
// Config: {"name":"udp","port":4210,"wifi":{"ssid":"...","pass":"..."}}
#include <WiFi.h>
#include <WiFiUdp.h>

#include "../transport.h"

class UdpTransport : public Transport {
 public:
  const char* name() const override { return "udp"; }

  bool begin(JsonObjectConst cfg, uint8_t selfId) override {
    self_ = selfId;
    port_ = cfg["port"] | 4210;
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
    IPAddress dst = (p && p->hasIp) ? p->ip : WiFi.broadcastIP();
    bool ok = udp_.beginPacket(dst, port_) && udp_.write(frame, len) == len && udp_.endPacket();
    if (ok) counters_.sendOk++;
    else counters_.sendErr++;
    return ok;
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
      int len = udp_.read(buf_, sizeof buf_);
      if (len > 0 && gate_) gate_->admit(buf_, (size_t)len, (int8_t)WiFi.RSSI());
    }
  }

 private:
  WiFiUDP udp_;
  uint16_t port_ = 4210;
  bool started_ = false;
  bool listening_ = false;
  uint8_t buf_[fb::MAX_FRAME];
};

FB_REGISTER_TRANSPORT(UdpTransport, "udp");
