#pragma once
#include "wled.h"
#include "command_watchdog.h"

// WLED 16+ registration is in bioglow_watchdog.cpp. Enable this directory
// with custom_usermods; see ADAPTIVE_LIGHTING.md for build instructions.
class BioGlowWatchdog : public Usermod {
  BioGlowCommandWatchdog watchdog;
  bool allowOutput = false;
public:
  void setup() override {
    // Start dark; loop also suppresses presets until an explicit Pi command.
    bri = 0;
    transitionDelay = 0;
    strip.setTransition(0);
    stateUpdated(CALL_MODE_NO_NOTIFY);
  }
  void loop() override {
    if (watchdog.expired(millis())) allowOutput = false;
    if (!allowOutput && (bri != 0 || briT != 0)) {
      bri = 0;
      transitionDelay = 0;
      strip.setTransition(0);
      stateUpdated(CALL_MODE_NO_NOTIFY);
    }
  }
  void readFromJsonState(JsonObject& root) override {
    JsonObject command = root["bioglow"];
    if (!command.isNull() && command["watchdog_ms"].is<unsigned int>()) {
      unsigned int ttl = command["watchdog_ms"];
      if (ttl == 0 || ttl == 3000) {
        allowOutput = true;
        watchdog.command(millis(), ttl);
      }
    }
  }
  void addToJsonInfo(JsonObject& root) override {
    root["bioglow_watchdog"] = 1;
  }
  void addToJsonState(JsonObject& root) override {
    JsonObject state = root.createNestedObject("bioglow");
    state["watchdog_ms"] = watchdog.armed() ? 3000 : 0;
  }
};
