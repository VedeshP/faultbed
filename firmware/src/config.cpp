#include "config.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

using namespace fb;

LinkRule parseLinkRule(JsonObjectConst j) {
  LinkRule r;
  const char* model = j["model"] | "none";
  if (strcmp(model, "bernoulli") == 0) r.model = LOSS_BERNOULLI;
  else if (strcmp(model, "gilbert_elliott") == 0) r.model = LOSS_GILBERT_ELLIOTT;
  else r.model = LOSS_NONE;
  r.pLoss = j["p_loss"] | 0.0;
  r.geGoodToBad = j["ge_p_gb"] | 0.0;
  r.geBadToGood = j["ge_p_bg"] | 1.0;
  r.geLossGood = j["ge_loss_good"] | 0.0;
  r.geLossBad = j["ge_loss_bad"] | 1.0;
  r.pCorrupt = j["p_corrupt"] | 0.0;
  r.pDuplicate = j["p_duplicate"] | 0.0;
  r.delayUs = (uint32_t)((j["delay_ms"] | 0.0) * 1000.0);
  r.jitterUs = (uint32_t)((j["jitter_ms"] | 0.0) * 1000.0);
  r.blackout = j["blackout"] | false;
  return r;
}

bool parseNodeConfig(JsonObjectConst j, NodeConfig& c, const char** err) {
  c = NodeConfig();
  int id = j["id"] | 0;
  if (id <= 0 || id >= FaultEngine::MAX_NODES) {
    *err = "bad id";
    return false;
  }
  c.id = (uint8_t)id;
  // Seeds travel as strings: JSON numbers cannot carry a full uint64 safely.
  const char* seed = j["seed"] | "1";
  c.seed = strtoull(seed, nullptr, 10);
  c.ttl = j["ttl"] | 8;
  c.neighborFilter = j["neighbor_filter"] | true;
  for (JsonVariantConst n : j["neighbors"].as<JsonArrayConst>()) {
    int v = n.as<int>();
    if (v > 0 && v < FaultEngine::MAX_NODES) c.neighbors[v] = true;
  }
  for (JsonPairConst kv : j["routes"].as<JsonObjectConst>()) {
    int dst = atoi(kv.key().c_str());
    int hop = kv.value().as<int>();
    if (dst > 0 && dst < FaultEngine::MAX_NODES && hop > 0 && hop < FaultEngine::MAX_NODES) c.routes[dst] = hop;
  }
  for (JsonObjectConst f : j["flows"].as<JsonArrayConst>()) {
    if (c.flowCount >= NodeConfig::MAX_FLOWS) {
      *err = "too many flows";
      return false;
    }
    Flow& fl = c.flows[c.flowCount++];
    fl.id = f["id"] | c.flowCount;
    fl.dst = f["dst"] | 0;
    fl.intervalMs = f["interval_ms"] | 1000;
    fl.size = f["size"] | 32;
    fl.echo = f["echo"] | true;
    fl.poisson = strcmp(f["pattern"] | "periodic", "poisson") == 0;
    fl.count = f["count"] | 0;
    fl.startMs = f["start_ms"] | 0;
    if (fl.dst == 0 || fl.dst == c.id) {
      *err = "bad flow dst";
      return false;
    }
  }
  return true;
}

bool parseMac(const char* s, uint8_t mac[6]) {
  unsigned v[6];
  if (!s || sscanf(s, "%x:%x:%x:%x:%x:%x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) return false;
  for (int i = 0; i < 6; i++) mac[i] = (uint8_t)v[i];
  return true;
}
