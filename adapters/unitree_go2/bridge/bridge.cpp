#include "bridge.h"
#include "sensors.h"

#include <unitree/idl/go2/LowState_.hpp>
#include <unitree/idl/go2/SportModeState_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/robot/go2/sport/sport_client.hpp>

#include <chrono>
#include <cinttypes>
#include <cmath>
#include <condition_variable>
#include <cstdint>
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
using LowStateMsg = unitree_go::msg::dds_::LowState_;
using LowSubscriber = unitree::robot::ChannelSubscriber<LowStateMsg>;
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
  std::mutex command_mutex;
  std::mutex mutex;
  std::unique_ptr<unitree::robot::go2::SportClient> client;
  std::unique_ptr<Subscriber> subscriber;
  std::unique_ptr<LowSubscriber> low_subscriber;
  State state;
  LowStateMsg low_state;
  std::chrono::steady_clock::time_point received_at{};
  std::chrono::steady_clock::time_point low_received_at{};
  std::chrono::system_clock::time_point state_observed_at{};
  std::chrono::system_clock::time_point low_observed_at{};
  std::condition_variable state_ready;
  bool has_state = false;
  bool has_low_state = false;
  bool closed = false;
  std::string client_version;
  std::string server_version;
  Go2SensorCache sensors;

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

  void on_low_state(const void* message) {
    if (!message) return;
    std::lock_guard<std::mutex> lock(mutex);
    if (closed) return;
    low_state = *static_cast<const LowStateMsg*>(message);
    low_received_at = std::chrono::steady_clock::now();
    low_observed_at = std::chrono::system_clock::now();
    has_low_state = true;
  }

  bool fresh_locked() const {
    return has_state && !closed &&
           std::chrono::steady_clock::now() - received_at <= kFreshness;
  }

  bool low_fresh_locked() const {
    return has_low_state && !closed &&
           std::chrono::steady_clock::now() - low_received_at <= kFreshness;
  }

  void close() noexcept {
    std::lock_guard<std::mutex> command_lock(command_mutex);
    {
      std::lock_guard<std::mutex> lock(mutex);
      if (closed) return;
      closed = true;
      has_state = false;
      has_low_state = false;
    }
    try {
      if (subscriber) subscriber->CloseChannel();
    } catch (...) {
    }
    subscriber.reset();
    try {
      if (low_subscriber) low_subscriber->CloseChannel();
    } catch (...) {
    }
    low_subscriber.reset();
    sensors.close();
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

using SportClient = unitree::robot::go2::SportClient;

double ranged_number(const double* numbers, size_t index, double minimum, double maximum,
                     const char* name) {
  const double value = numbers[index];
  if (!std::isfinite(value) || value < minimum || value > maximum) {
    throw std::runtime_error(std::string(name) + " is outside the Sport API range");
  }
  return value;
}

void require_shape(size_t number_count, size_t expected, int has_flag, bool flag_expected) {
  if (number_count != expected || static_cast<bool>(has_flag) != flag_expected) {
    throw std::runtime_error("unexpected sport parameters");
  }
}

struct SportResult {
  int32_t code = 0;
  std::string extra;
};

int32_t call_named(SportClient& client, const std::string& action) {
  using NoneCall = int32_t (SportClient::*)();
  static const struct {
    const char* name;
    NoneCall call;
  } actions[] = {
      {"stop_move", &SportClient::StopMove},
      {"balance_stand", &SportClient::BalanceStand},
      {"stand_up", &SportClient::StandUp},
      {"stand_down", &SportClient::StandDown},
      {"sit", &SportClient::Sit},
      {"rise_sit", &SportClient::RiseSit},
      {"hello", &SportClient::Hello},
      {"stretch", &SportClient::Stretch},
      {"content", &SportClient::Content},
      {"heart", &SportClient::Heart},
      {"scrape", &SportClient::Scrape},
      {"free_walk", &SportClient::FreeWalk},
      {"static_walk", &SportClient::StaticWalk},
      {"trot_run", &SportClient::TrotRun},
      {"economic_gait", &SportClient::EconomicGait},
      {"recovery_stand", &SportClient::RecoveryStand},
      {"front_flip", &SportClient::FrontFlip},
      {"front_jump", &SportClient::FrontJump},
      {"front_pounce", &SportClient::FrontPounce},
      {"dance1", &SportClient::Dance1},
      {"dance2", &SportClient::Dance2},
      {"left_flip", &SportClient::LeftFlip},
      {"back_flip", &SportClient::BackFlip},
      {"switch_avoid_mode", &SportClient::SwitchAvoidMode},
  };
  for (const auto& item : actions) {
    if (action == item.name) return (client.*item.call)();
  }
  throw std::runtime_error("unknown Go2 sport action");
}

int32_t call_flag(SportClient& client, const std::string& action, bool enabled) {
  using FlagCall = int32_t (SportClient::*)(bool);
  static const struct {
    const char* name;
    FlagCall call;
  } actions[] = {
      {"pose", &SportClient::Pose},
      {"classic_walk", &SportClient::ClassicWalk},
      {"switch_joystick", &SportClient::SwitchJoystick},
      {"handstand", &SportClient::HandStand},
      {"free_bound", &SportClient::FreeBound},
      {"free_jump", &SportClient::FreeJump},
      {"free_avoid", &SportClient::FreeAvoid},
      {"walk_upright", &SportClient::WalkUpright},
      {"cross_step", &SportClient::CrossStep},
      {"auto_recover_set", &SportClient::AutoRecoverSet},
  };
  for (const auto& item : actions) {
    if (action == item.name) return (client.*item.call)(enabled);
  }
  throw std::runtime_error("unknown Go2 sport action");
}

SportResult invoke_sport(SportClient& client, const std::string& action,
                         const double* numbers, size_t number_count,
                         int has_flag, int flag) {
  if (action == "damp") throw std::runtime_error("damp is not exposed");
  if (has_flag != 0 && has_flag != 1) throw std::runtime_error("unexpected sport parameters");
  if (flag != 0 && flag != 1) throw std::runtime_error("unexpected sport parameters");
  if ((number_count > 0 && numbers == nullptr) || action.empty() || action.size() > 64) {
    throw std::runtime_error("unexpected sport parameters");
  }

  SportResult result;
  if (action == "move") {
    require_shape(number_count, 3, has_flag, false);
    result.code = client.Move(
        static_cast<float>(ranged_number(numbers, 0, -2.5, 3.8, "vx")),
        static_cast<float>(ranged_number(numbers, 1, -1.0, 1.0, "vy")),
        static_cast<float>(ranged_number(numbers, 2, -4.0, 4.0, "vyaw")));
    return result;
  }
  if (action == "euler") {
    require_shape(number_count, 3, has_flag, false);
    result.code = client.Euler(
        static_cast<float>(ranged_number(numbers, 0, -0.75, 0.75, "roll")),
        static_cast<float>(ranged_number(numbers, 1, -0.75, 0.75, "pitch")),
        static_cast<float>(ranged_number(numbers, 2, -0.6, 0.6, "yaw")));
    return result;
  }
  if (action == "speed_level") {
    require_shape(number_count, 1, has_flag, false);
    const double level = ranged_number(numbers, 0, -1.0, 1.0, "level");
    if (level != std::trunc(level)) throw std::runtime_error("level must be an integer");
    result.code = client.SpeedLevel(static_cast<int>(level));
    return result;
  }
  if (action == "auto_recover_get") {
    require_shape(number_count, 0, has_flag, false);
    bool enabled = false;
    result.code = client.AutoRecoverGet(enabled);
    result.extra = std::string("\"enabled\":") + (enabled ? "true" : "false");
    return result;
  }
  if (has_flag) {
    require_shape(number_count, 0, has_flag, true);
    result.code = call_flag(client, action, flag == 1);
    return result;
  }
  require_shape(number_count, 0, has_flag, false);
  result.code = call_named(client, action);
  return result;
}

bool write_call_result(char* output, size_t capacity, const SportResult& result) {
  const int needed = result.extra.empty()
      ? std::snprintf(output, capacity, "{\"api_code\":%d}", result.code)
      : std::snprintf(output, capacity, "{\"api_code\":%d,%s}", result.code, result.extra.c_str());
  return needed > 0 && static_cast<size_t>(needed) < capacity;
}
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
    bridge->low_subscriber.reset(new LowSubscriber("rt/lowstate"));
    bridge->low_subscriber->InitChannel(
        [raw](const void* message) { raw->on_low_state(message); }, 1);
    bridge->sensors.open();
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

int go2_bridge_read_battery(void* handle, char* output, size_t output_capacity,
                            char* error, size_t error_capacity) {
  if (error && error_capacity) error[0] = '\0';
  auto* bridge = static_cast<Bridge*>(handle);
  if (!valid(bridge) || !output || !output_capacity) {
    set_error(error, error_capacity, "bridge handle and output buffer are required");
    return 0;
  }
  uint8_t soc = 0;
  uint8_t status = 0;
  int32_t current_ma = 0;
  std::chrono::system_clock::time_point observed_at{};
  {
    std::lock_guard<std::mutex> lock(bridge->mutex);
    if (!bridge->low_fresh_locked()) {
      set_error(error, error_capacity, "battery state is missing or stale");
      return 0;
    }
    const auto& bms = bridge->low_state.bms_state();
    soc = bms.soc();
    status = bms.status();
    current_ma = bms.current();
    observed_at = bridge->low_observed_at;
  }
  const std::string observed = format_utc(observed_at);
  const int needed = std::snprintf(
      output, output_capacity,
      "{\"soc\":%u,\"status\":%u,\"current_ma\":%" PRId32 ",\"observed_at\":\"%s\"}",
      static_cast<unsigned>(soc), static_cast<unsigned>(status), current_ma,
      observed.c_str());
  if (needed < 0 || static_cast<size_t>(needed) >= output_capacity) {
    set_error(error, error_capacity, "battery output buffer is too small");
    return 0;
  }
  return 1;
}

int go2_bridge_read_sensors(void* handle, char* output, size_t output_capacity,
                            char* error, size_t error_capacity) {
  if (error && error_capacity) error[0] = '\0';
  auto* bridge = static_cast<Bridge*>(handle);
  if (!valid(bridge) || !output || !output_capacity) {
    set_error(error, error_capacity, "bridge handle and output buffer are required");
    return 0;
  }
  try {
    State sport;
    LowStateMsg low;
    bool sport_fresh = false;
    bool low_fresh = false;
    std::chrono::system_clock::time_point sport_at{};
    std::chrono::system_clock::time_point low_at{};
    {
      std::lock_guard<std::mutex> lock(bridge->mutex);
      if (bridge->closed) {
        set_error(error, error_capacity, "bridge is closed");
        return 0;
      }
      sport = bridge->state;
      low = bridge->low_state;
      sport_fresh = bridge->fresh_locked();
      low_fresh = bridge->low_fresh_locked();
      sport_at = bridge->state_observed_at;
      low_at = bridge->low_observed_at;
    }
    const std::string body = bridge->sensors.to_json(
        sport_fresh, sport, sport_at, low_fresh, low, low_at);
    if (body.size() + 1 > output_capacity) {
      set_error(error, error_capacity, "sensor output buffer is too small");
      return 0;
    }
    std::memcpy(output, body.c_str(), body.size() + 1);
    return 1;
  } catch (const std::exception& e) {
    set_error(error, error_capacity, e.what());
  } catch (...) {
    set_error(error, error_capacity, "unable to read Go2 sensors");
  }
  return 0;
}

int go2_bridge_call(void* handle, const char* action, const double* numbers,
                    size_t number_count, int has_flag, int flag,
                    char* output, size_t output_capacity,
                    char* error, size_t error_capacity) {
  if (error && error_capacity) error[0] = '\0';
  auto* bridge = static_cast<Bridge*>(handle);
  if (!valid(bridge) || !action || !output || !output_capacity) {
    set_error(error, error_capacity, "bridge handle, action, and output buffer are required");
    return 0;
  }
  try {
    std::lock_guard<std::mutex> command_lock(bridge->command_mutex);
    SportClient* client = nullptr;
    {
      std::lock_guard<std::mutex> lock(bridge->mutex);
      const bool versions_match = !bridge->client_version.empty() &&
          bridge->client_version == bridge->server_version;
      if (bridge->closed || !bridge->client) {
        set_error(error, error_capacity, "bridge is closed");
        return 0;
      }
      if (!versions_match) {
        set_error(error, error_capacity, "client/server API versions do not match");
        return 0;
      }
      if (!bridge->fresh_locked()) {
        set_error(error, error_capacity, "sport mode state is missing or stale");
        return 0;
      }
      client = bridge->client.get();
    }
    const SportResult result = invoke_sport(*client, action, numbers, number_count, has_flag, flag);
    if (!write_call_result(output, output_capacity, result)) {
      set_error(error, error_capacity, "action output buffer is too small");
      return 0;
    }
    return 1;
  } catch (const std::exception& e) {
    set_error(error, error_capacity, e.what());
  } catch (...) {
    set_error(error, error_capacity, "unknown error calling Unitree Go2 sport action");
  }
  return 0;
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
