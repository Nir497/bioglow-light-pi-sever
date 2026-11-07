#include "usermod_bioglow_watchdog.h"

// WLED 16+: enabled through custom_usermods in platformio_override.ini.
static BioGlowWatchdog bioglow_watchdog;
REGISTER_USERMOD(bioglow_watchdog);
