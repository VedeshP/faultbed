// ESP-NOW: connectionless 802.11 vendor frames, no access point.
// Unicast frames get MAC-layer ACKs and retries; broadcast frames do not.
// Config: {"name":"espnow","channel":1,"unicast":true}
#include <WiFi.h>
#include <esp_idf_version.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <string.h>

#include "../transport.h"

class EspNowTransport : public Transport {
 public:
  const char* name() const override { return "espnow"; }

  bool begin(JsonObjectConst cfg, uint8_t selfId) override {
    self_ = selfId;
    channel_ = cfg["channel"] | 1;
    unicast_ = cfg["unicast"] | true;
    if (!ring_.init(24)) return false;
    instance_ = this;
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_channel(channel_, WIFI_SECOND_CHAN_NONE);
    esp_wifi_set_promiscuous(false);
    if (esp_now_init() != ESP_OK) {
      reportLink(false, "esp_now_init failed");
      return false;
    }
    esp_now_register_recv_cb(&EspNowTransport::onRecv);
    esp_now_register_send_cb(&EspNowTransport::onSent);
    addPeer(BCAST);
    onPeersChanged();
    started_ = true;
    reportLink(true, "espnow");
    return true;
  }

  void end() override {
    if (!started_) return;
    esp_now_unregister_recv_cb();
    esp_now_unregister_send_cb();
    esp_now_deinit();
    WiFi.mode(WIFI_OFF);
    started_ = false;
    reportLink(false);
  }

  void onPeersChanged() override {
    if (!unicast_) return;
    for (int i = 1; i < MAX_PEERS; i++)
      if (peers_[i].hasMac && i != self_) addPeer(peers_[i].mac);
  }

  bool isUp() const override { return started_; }

  bool send(uint8_t hopDst, const uint8_t* frame, size_t len) override {
    if (!started_) return false;
    const uint8_t* mac = BCAST;
    const Peer* p = peer(hopDst);
    if (unicast_ && p && p->hasMac) mac = p->mac;
    bool ok = esp_now_send(mac, frame, len) == ESP_OK;
    if (ok) counters_.sendOk++;
    else counters_.sendErr++;
    return ok;
  }

  void loop() override {
    RxRing::Item it;
    while (ring_.pop(it))
      if (gate_) gate_->admit(it.data, it.len, it.rssi);
    // MAC-layer delivery failures are counted in the radio task; report them here.
    uint32_t nack = macNack_;
    if (nack != reportedNack_) {
      counters_.sendErr += nack - reportedNack_;
      char buf[96];
      snprintf(buf, sizeof buf, "{\"e\":\"txerr\",\"t\":%llu,\"tr\":\"espnow\",\"why\":\"mac_nack\",\"n\":%lu}",
               (unsigned long long)nowUs(), (unsigned long)(nack - reportedNack_));
      emit(buf);
      reportedNack_ = nack;
    }
    if (ringOverflow_ != counters_.rxOverflow) counters_.rxOverflow = ringOverflow_;
  }

 private:
  static constexpr uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

  void addPeer(const uint8_t* mac) {
    if (esp_now_is_peer_exist(mac)) return;
    esp_now_peer_info_t info;
    memset(&info, 0, sizeof info);
    memcpy(info.peer_addr, mac, 6);
    info.channel = channel_;
    info.ifidx = WIFI_IF_STA;
    info.encrypt = false;
    esp_now_add_peer(&info);
  }

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
  static void onRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
    int8_t rssi = info && info->rx_ctrl ? (int8_t)info->rx_ctrl->rssi : 0;
#else
  static void onRecv(const uint8_t* mac, const uint8_t* data, int len) {
    (void)mac;
    int8_t rssi = 0;
#endif
    if (instance_ && !instance_->ring_.pushFromTask(data, (size_t)len, rssi)) instance_->ringOverflow_++;
  }

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
  static void onSent(const esp_now_send_info_t* info, esp_now_send_status_t status) {
    (void)info;
#else
  static void onSent(const uint8_t* mac, esp_now_send_status_t status) {
    (void)mac;
#endif
    if (status != ESP_NOW_SEND_SUCCESS && instance_) instance_->macNack_++;
  }

  static EspNowTransport* instance_;
  RxRing ring_;
  uint8_t channel_ = 1;
  bool unicast_ = true;
  bool started_ = false;
  volatile uint32_t macNack_ = 0;
  volatile uint32_t ringOverflow_ = 0;
  uint32_t reportedNack_ = 0;
};

constexpr uint8_t EspNowTransport::BCAST[6];
EspNowTransport* EspNowTransport::instance_ = nullptr;

FB_REGISTER_TRANSPORT(EspNowTransport, "espnow");
