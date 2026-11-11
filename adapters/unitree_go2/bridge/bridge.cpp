#include "bridge.h"

#include <unitree/idl/go2/SportModeState_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/robot/go2/sport/sport_client.hpp>

#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>

namespace {
using State = unitree_go::msg::dds_::SportModeState_;
using Subscriber = unitree::robot::ChannelSubscriber<State>;
constexpr auto kFreshness = std::chrono::seconds(2);

struct Bridge;
std::mutex g_factory_mutex;
Bridge* g_active_bridge = nullptr;

void set_error(char* buffer, size_t capacity, const std::string& message) {
  if (buffer && capacity) {
    std::snprintf(buffer, capacity, "%s", message.c_str());
  }
}

struct Bridge {
  std::mutex mutex;
  std::unique_ptr<unitree::robot::go2::SportClient> client;
  std::unique_ptr<Subscriber> subscriber;
  State state;
  std::chrono::steady_clock::time_point received_at{};
  std::chrono::system_clock::time_point state_observed_at{};
  std::condition_variable state_ready;
  bool has_state = false;
  bool closed = false;
  std::string client_version;
  std::string server_version;

  void on_state(const void* message) {
    if (!message) return;
    std::lock_guard<std::mutex> lock(mutex);
    if (closed) return;
    state = *static_cast<const State*>(message);
    received_at = std::chrono::steady_clock::now();
    state_observed_at = std::chrono::system_clock::now();
    has_state = true;
    state_ready.notify_all();
  }

  bool fresh_locked() const {
    return has_state && !closed &&
           std::chrono::steady_clock::now() - received_at <= kFreshness;
  }

  void close() noexcept {
    {
      std::lock_guard<std::mutex> lock(mutex);
      if (closed) return;
      closed = true;
      has_state = false;
    }
    try {
      if (subscriber) subscriber->CloseChannel();
    } catch (...) {
    }
    subscriber.reset();
    client.reset();
    try {
      unitree::robot::ChannelFactory::Instance()->Release();
    } catch (...) {
    }
  }
};

std::string format_utc(std::chrono::system_clock::time_point now) {
  const auto seconds = std::chrono::system_clock::to_time_t(now);
  std::tm tm{};
#if defined(_WIN32)
  gmtime_s(&tm, &seconds);
#else
  gmtime_r(&seconds, &tm);
#endif
  char value[32];
  std::strftime(value, sizeof(value), "%Y-%m-%dT%H:%M:%S", &tm);
  const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(
      now.time_since_epoch()).count() % 1000;
  char result[40];
  std::snprintf(result, sizeof(result), "%s.%03lldZ", value,
                static_cast<long long>(millis));
  return result;
}

std::string json_escape(const std::string& value) {
  static constexpr char hex[] = "0123456789abcdef";
  std::string escaped;
  escaped.reserve(value.size());
  for (unsigned char ch : value) {
    switch (ch) {
      case '"': escaped += "\\\""; break;
      case '\\': escaped += "\\\\"; break;
      case '\b': escaped += "\\b"; break;
      case '\f': escaped += "\\f"; break;
      case '\n': escaped += "\\n"; break;
      case '\r': escaped += "\\r"; break;
      case '\t': escaped += "\\t"; break;
      default:
        if (ch < 0x20) {
          escaped += "\\u00";
          escaped += hex[ch >> 4];
          escaped += hex[ch & 0x0f];
        } else {
          escaped += static_cast<char>(ch);
        }
    }
  }
  return escaped;
}

bool valid(const Bridge* bridge) { return bridge != nullptr; }
}  // namespace

extern "C" {

void* go2_bridge_create(const char* network_interface, char* error,
                        size_t error_capacity) {
  std::lock_guard<std::mutex> factory_lock(g_factory_mutex);
  if (error && error_capacity) error[0] = '\0';
  if (g_active_bridge) {
    set_error(error, error_capacity,
              "only one Unitree Go2 bridge may be active in this process");
    return nullptr;
  }
  if (!network_interface || !*network_interface) {
    set_error(error, error_capacity, "network interface is required");
    return nullptr;
  }

  std::unique_ptr<Bridge> bridge(new Bridge());
  try {
    unitree::robot::ChannelFactory::Instance()->Init(0, network_interface);
    bridge->client.reset(new unitree::robot::go2::SportClient());
    bridge->client->SetTimeout(2.0f);
    bridge->client->Init();
    bridge->client_version = bridge->client->GetApiVersion();
    bridge->server_version = bridge->client->GetServerApiVersion();

    bridge->subscriber.reset(new Subscriber("rt/sportmodestate"));
    Bridge* raw = bridge.get();
    bridge->subscriber->InitChannel(
        [raw](const void* message) { raw->on_state(message); }, 1);
    {
      std::unique_lock<std::mutex> lock(bridge->mutex);
      if (!bridge->state_ready.wait_for(lock, std::chrono::seconds(3),
                                        [&bridge] { return bridge->has_state; })) {
        throw std::runtime_error("timed out waiting for rt/sportmodestate");
      }
    }
    if (bridge->client_version.empty() ||
        bridge->client_version != bridge->server_version) {
      throw std::runtime_error("client/server API versions do not match");
    }
    g_active_bridge = bridge.get();
    return bridge.release();
  } catch (const std::exception& e) {
    bridge->close();
    set_error(error, error_capacity, e.what());
  } catch (...) {
    bridge->close();
    set_error(error, error_capacity, "unknown error initializing Unitree Go2 bridge");
  }
  return nullptr;
}

int go2_bridge_health(void* handle, char* error, size_t error_capacity) {
  if (error && error_capacity) error[0] = '\0';
  auto* bridge = static_cast<Bridge*>(handle);
  if (!valid(bridge)) {
    set_error(error, error_capacity, "bridge handle is null");
    return 0;
  }
  try {
    const bool versions_match = !bridge->client_version.empty() &&
        bridge->client_version == bridge->server_version;
    std::lock_guard<std::mutex> lock(bridge->mutex);
    if (bridge->closed) set_error(error, error_capacity, "bridge is closed");
    else if (!versions_match) set_error(error, error_capacity, "client/server API versions do not match");
    else if (!bridge->fresh_locked()) set_error(error, error_capacity, "sport mode state is missing or stale");
    else return 1;
  } catch (const std::exception& e) {
    set_error(error, error_capacity, e.what());
  }
  return 0;
}

int go2_bridge_read_state(void* handle, char* output, size_t output_capacity,
                          char* error, size_t error_capacity) {
  if (error && error_capacity) error[0] = '\0';
  auto* bridge = static_cast<Bridge*>(handle);
  if (!valid(bridge) || !output || !output_capacity) {
    set_error(error, error_capacity, "bridge handle and output buffer are required");
    return 0;
  }
  State state;
  int32_t stamp_sec;
  uint32_t stamp_nanosec;
  std::chrono::system_clock::time_point state_observed_at;
  std::string client_version, server_version;
  {
    std::lock_guard<std::mutex> lock(bridge->mutex);
    if (!bridge->fresh_locked()) {
      set_error(error, error_capacity, "sport mode state is missing or stale");
      return 0;
    }
    state = bridge->state;
    stamp_sec = state.stamp().sec();
    stamp_nanosec = state.stamp().nanosec();
    state_observed_at = bridge->state_observed_at;
    client_version = bridge->client_version;
    server_version = bridge->server_version;
  }
  const auto& p = state.position();
  const auto& v = state.velocity();
  const std::string escaped_client_version = json_escape(client_version);
  const std::string escaped_server_version = json_escape(server_version);
  const std::string observed_at = format_utc(state_observed_at);
  const int needed = std::snprintf(
      output, output_capacity,
      "{\"api_version_client\":\"%s\",\"api_version_server\":\"%s\","
      "\"observed_at\":\"%s\",\"state_stamp_sec\":%d,"
      "\"state_stamp_nanosec\":%u,\"mode\":%u,\"velocity\":[%.9g,%.9g,%.9g],"
      "\"yaw_speed\":%.9g,\"position\":[%.9g,%.9g,%.9g],\"error_code\":%u}",
      escaped_client_version.c_str(), escaped_server_version.c_str(), observed_at.c_str(), stamp_sec,
      stamp_nanosec, static_cast<unsigned>(state.mode()), v[0], v[1], v[2],
      state.yaw_speed(), p[0], p[1], p[2], state.error_code());
  if (needed < 0 || static_cast<size_t>(needed) >= output_capacity) {
    set_error(error, error_capacity, "state output buffer is too small");
    return 0;
  }
  return 1;
}

int go2_bridge_close(void* handle, char* error, size_t error_capacity) {
  std::lock_guard<std::mutex> factory_lock(g_factory_mutex);
  if (error && error_capacity) error[0] = '\0';
  auto* bridge = static_cast<Bridge*>(handle);
  if (!valid(bridge)) {
    set_error(error, error_capacity, "bridge handle is null");
    return 0;
  }
  try {
    bridge->close();
    if (g_active_bridge == bridge) g_active_bridge = nullptr;
    return 1;
  } catch (const std::exception& e) {
    set_error(error, error_capacity, e.what());
    return 0;
  } catch (...) {
    set_error(error, error_capacity, "unknown error closing Unitree Go2 bridge");
    return 0;
  }
}

void go2_bridge_destroy(void* handle) {
  std::lock_guard<std::mutex> factory_lock(g_factory_mutex);
  auto* bridge = static_cast<Bridge*>(handle);
  if (!bridge) return;
  bridge->close();
  if (g_active_bridge == bridge) g_active_bridge = nullptr;
  delete bridge;
}

}  // extern "C"
