#pragma once
#include <stdint.h>

// Shared by the WLED usermod and host-side simulated clock tests.
class BioGlowCommandWatchdog {
  uint32_t received = 0;
  uint32_t timeout = 0;
public:
  void command(uint32_t now, uint32_t ttl) { received = now; timeout = ttl; }
  bool armed() const { return timeout != 0; }
  bool expired(uint32_t now) {
    if (timeout && uint32_t(now - received) >= timeout) {
      timeout = 0;
      return true;
    }
    return false;
  }
};
