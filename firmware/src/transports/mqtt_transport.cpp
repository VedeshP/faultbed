// MQTT over TCP through a broker on the controller PC (e.g. Mosquitto).
// Each node subscribes to fb/n/<id>; a hop to node X is a publish to fb/n/X.
// TCP already guarantees delivery to the broker, so injected faults act on the
// message as it is delivered to the application (after the broker).
// Config: {"name":"mqtt","host":"192.168.1.10","port":1883,"keepalive_s":5,"wifi":{...}}
#include <PubSubClient.h>
#include <WiFi.h>

#include "../transport.h"

class MqttTransport : public Transport {
 public:
  MqttTransport() : mqtt_(net_) {}

  const char* name() const override { return "mqtt"; }

  bool begin(JsonObjectConst cfg, uint8_t selfId) override {
    self_ = selfId;
    host_ = (const char*)(cfg["host"] | "");
    port_ = cfg["port"] | 1883;
    snprintf(clientId_, sizeof clientId_, "fb-node-%u", selfId);
    snprintf(topic_, sizeof topic_, "fb/n/%u", selfId);
    instance_ = this;
    mqtt_.setServer(host_.c_str(), port_);
    mqtt_.setBufferSize(512);
    mqtt_.setKeepAlive(cfg["keepalive_s"] | 5);
    mqtt_.setSocketTimeout(1);
    mqtt_.setCallback(&MqttTransport::onMessage);
    wifi::start(cfg);
    started_ = true;
    return host_.length() > 0;
  }

  void end() override {
    if (!started_) return;
    mqtt_.disconnect();
    net_.stop();
    wifi::stop();
    started_ = false;
    reportLink(false);
  }

  bool isUp() const override { return started_ && const_cast<PubSubClient&>(mqtt_).connected(); }

  bool send(uint8_t hopDst, const uint8_t* frame, size_t len) override {
    if (!isUp()) return false;
    char topic[24];
    if (hopDst == fb::BROADCAST) snprintf(topic, sizeof topic, "fb/n/all");
    else snprintf(topic, sizeof topic, "fb/n/%u", hopDst);
    bool ok = mqtt_.publish(topic, frame, (unsigned int)len, false);
    if (ok) counters_.sendOk++;
    else counters_.sendErr++;
    return ok;
  }

  void loop() override {
    if (!started_) return;
    if (!wifi::connected()) {
      reportLink(false, "wifi");
      return;
    }
    if (!mqtt_.connected()) {
      reportLink(false, "broker");
      uint64_t now = nowUs();
      if (now - lastAttemptUs_ > 1000000ULL) {  // connect() blocks up to the socket timeout
        lastAttemptUs_ = now;
        if (mqtt_.connect(clientId_)) {
          mqtt_.subscribe(topic_);
          mqtt_.subscribe("fb/n/all");
        }
      }
      if (!mqtt_.connected()) return;
    }
    reportLink(true);
    mqtt_.loop();  // invokes onMessage on this task
  }

 private:
  static void onMessage(char* topic, byte* payload, unsigned int length) {
    (void)topic;
    if (instance_ && instance_->gate_) instance_->gate_->admit(payload, length, (int8_t)WiFi.RSSI());
  }

  static MqttTransport* instance_;
  WiFiClient net_;
  PubSubClient mqtt_;
  String host_;
  uint16_t port_ = 1883;
  char clientId_[24];
  char topic_[24];
  bool started_ = false;
  uint64_t lastAttemptUs_ = 0;
};

MqttTransport* MqttTransport::instance_ = nullptr;

FB_REGISTER_TRANSPORT(MqttTransport, "mqtt");
