#include <chrono>
#include <fstream>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>

#define SPDIAG_PERF_DEF_FILE "perf_points.def"
#define SPDIAG_PROGRAM_NAME "spdiag-minimal"
#include "spdiag/auto_perf.h"

void measure(PerfKey key, const char* name, int milliseconds) {
  // Same global mode as the six RPCs, but Start/End stay on this one thread.
  // Construct before starting the reference clock to exclude SDK calibration.
  SpDiag::PerfPoint point(key, SpDiag::global_perf, SpDiag::PerfLevel::SUB_SYSTEM);
  const auto begin = std::chrono::steady_clock::now();
  point.Start();
  try {
    std::this_thread::sleep_for(std::chrono::milliseconds(milliseconds));
  } catch (...) {
    point.End(-1);
    throw;
  }
  point.End(0);
  const auto elapsed =
      std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - begin)
          .count();
  std::cout << "SAMPLE\t" << name << '\t' << milliseconds << '\t' << elapsed << std::endl;
}

int main(int argc, char** argv) {
  const std::string name = argc == 2 ? argv[1] : "";
  if (name.rfind("/spdiag_minimal_", 0) != 0 || name.size() > 128 ||
      name.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789_", 1) != std::string::npos) {
    std::cerr << "Expected a dedicated /spdiag_minimal_* SHM name\n";
    return 2;
  }
  // Record the actual mapped SDK, not just the configured link search path.
  std::ifstream maps("/proc/self/maps");
  std::set<std::string> libraries;
  for (std::string line; std::getline(maps, line);) {
    const auto path = line.find('/');
    if (path != std::string::npos && line.find("libspdiag.so", path) != std::string::npos)
      libraries.insert(line.substr(path));
  }
  for (const auto& library : libraries)
    std::cout << "SDK_MAPPING\t" << library << std::endl;

  auto& manager = SpDiag::PerfManager::Instance();
  try {
    SpDiag::PerfConfig config;
    config.shmName = name;
    config.programName = SPDIAG_PROGRAM_NAME;
    config.slotCount = static_cast<uint32_t>(PerfKey::COUNT);
    config.levelGate = 1;
    config.dumpIntervalMs = 1000;
    manager.Init(config);
    if (!manager.IsInitialized())
      throw std::runtime_error("dedicated SpDiag SHM unavailable");
    measure(PerfKey::SLEEP_1S, "Sleep1s", 1000);
    measure(PerfKey::SLEEP_5S, "Sleep5s", 5000);
    manager.Shutdown();  // Flush the last interval before the separate CLI reads it.
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << std::endl;
    manager.Shutdown();
    return 1;
  }
}
