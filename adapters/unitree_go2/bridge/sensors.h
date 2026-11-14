#pragma once

#include <chrono>
#include <string>

namespace unitree_go {
namespace msg {
namespace dds_ {
class SportModeState_;
class LowState_;
}
}
}

// Latest-value cache for Go2 sensor topics.
// Point-cloud, video, and compressed-map bytes are reduced in the callback.
class Go2SensorCache {
 public:
  Go2SensorCache();
  ~Go2SensorCache();

  Go2SensorCache(const Go2SensorCache&) = delete;
  Go2SensorCache& operator=(const Go2SensorCache&) = delete;

  void open();
  void close() noexcept;
  std::string to_json(
      bool sport_fresh,
      const unitree_go::msg::dds_::SportModeState_& sport,
      std::chrono::system_clock::time_point sport_observed_at,
      bool low_fresh,
      const unitree_go::msg::dds_::LowState_& low,
      std::chrono::system_clock::time_point low_observed_at) const;

 private:
  struct Impl;
  Impl* impl_;
};
