#include "core/model.h"

#include <ctime>
#include <iomanip>
#include <sstream>
namespace ap {
std::string utc_now() {
  auto time = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
  std::tm tm{};
  gmtime_r(&time, &tm);
  std::ostringstream out;
  out << std::put_time(&tm, "%Y-%m-%dT%H:%M:%SZ");
  return out.str();
}
}  // namespace ap
