// JSON (from the controller) -> portable core structs.
#pragma once
#include <ArduinoJson.h>

#include "fault_engine.h"
#include "node.h"

bool parseNodeConfig(JsonObjectConst j, fb::NodeConfig& out, const char** err);
fb::LinkRule parseLinkRule(JsonObjectConst j);
bool parseMac(const char* s, uint8_t mac[6]);
