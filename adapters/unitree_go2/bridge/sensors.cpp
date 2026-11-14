#include "sensors.h"

#include <unitree/idl/go2/Go2FrontVideoData_.hpp>
#include <unitree/idl/go2/HeightMap_.hpp>
#include <unitree/idl/go2/IMUState_.hpp>
#include <unitree/idl/go2/LidarState_.hpp>
#include <unitree/idl/go2/LowState_.hpp>
#include <unitree/idl/go2/SportModeState_.hpp>
#include <unitree/idl/go2/UwbState_.hpp>
#include <unitree/idl/go2/VoxelMapCompressed_.hpp>
#include <unitree/idl/go2/WirelessController_.hpp>
#include <unitree/idl/ros2/Odometry_.hpp>
#include <unitree/idl/ros2/PointCloud2_.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace {
constexpr auto kFreshness = std::chrono::seconds(2);
constexpr size_t kHeightMapLimit = 8192;
constexpr size_t kPointSampleLimit = 32;
constexpr uint8_t kFloat32 = 7;

using Steady = std::chrono::steady_clock;
using Wall = std::chrono::system_clock;

std::string format_utc(Wall::time_point now) {
  const auto seconds = Wall::to_time_t(now);
  std::tm tm{};
  gmtime_r(&seconds, &tm);
  char value[32];
  std::strftime(value, sizeof(value), "%Y-%m-%dT%H:%M:%S", &tm);
  const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(
                          now.time_since_epoch())
                          .count() %
                      1000;
  char result[40];
  std::snprintf(result, sizeof(result), "%s.%03lldZ", value, static_cast<long long>(millis < 0 ? 0 : millis));
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

class JsonWriter {
 public:
  void begin_object() { begin('{', '}'); }
  void end_object() { end('}'); }
  void begin_array() { begin('[', ']'); }
  void end_array() { end(']'); }

  void key(const char* name) {
    comma();
    write_string(name);
    out_ += ':';
    pending_value_ = true;
  }

  void null() { raw("null"); }
  void boolean(bool value) { raw(value ? "true" : "false"); }

  void integer(long long value) {
    char buffer[32];
    std::snprintf(buffer, sizeof(buffer), "%lld", value);
    raw(buffer);
  }

  void number(double value) {
    if (!std::isfinite(value)) {
      null();
      return;
    }
    char buffer[64];
    std::snprintf(buffer, sizeof(buffer), "%.9g", value);
    raw(buffer);
  }

  void string(const std::string& value) {
    if (!pending_value_) comma();
    pending_value_ = false;
    write_string(value);
  }

  const std::string& str() const { return out_; }

 private:
  struct Frame {
    char close;
    bool first;
  };

  void begin(char open, char close) {
    if (!pending_value_) comma();
    pending_value_ = false;
    out_ += open;
    stack_.push_back(Frame{close, true});
  }

  void end(char close) {
    if (stack_.empty() || stack_.back().close != close) return;
    out_ += close;
    stack_.pop_back();
  }

  void comma() {
    if (stack_.empty()) return;
    if (stack_.back().first) {
      stack_.back().first = false;
      return;
    }
    out_ += ',';
  }

  void raw(const char* text) {
    if (!pending_value_) comma();
    pending_value_ = false;
    out_ += text;
  }

  void write_string(const std::string& value) {
    out_ += '"';
    out_ += json_escape(value);
    out_ += '"';
  }

  std::vector<Frame> stack_;
  std::string out_;
  bool pending_value_ = false;
};

template <typename T>
void numbers(JsonWriter& json, const T& values) {
  json.begin_array();
  for (const auto value : values) json.number(static_cast<double>(value));
  json.end_array();
}

template <typename T>
void integers(JsonWriter& json, const T& values) {
  json.begin_array();
  for (const auto value : values) json.integer(static_cast<long long>(value));
  json.end_array();
}

void imu(JsonWriter& json, const unitree_go::msg::dds_::IMUState_& state) {
  json.begin_object();
  json.key("quaternion");
  numbers(json, state.quaternion());
  json.key("gyroscope");
  numbers(json, state.gyroscope());
  json.key("accelerometer");
  numbers(json, state.accelerometer());
  json.key("rpy");
  numbers(json, state.rpy());
  json.key("temperature");
  json.integer(state.temperature());
  json.end_object();
}

const char* joint_name(size_t index) {
  static const char* names[] = {
      "FR_hip", "FR_thigh", "FR_calf", "FL_hip", "FL_thigh", "FL_calf",
      "RR_hip", "RR_thigh", "RR_calf", "RL_hip", "RL_thigh", "RL_calf",
  };
  if (index < 12) return names[index];
  return nullptr;
}

struct CloudSample {
  int32_t stamp_sec = 0;
  uint32_t stamp_nanosec = 0;
  std::string frame_id;
  uint32_t height = 0;
  uint32_t width = 0;
  uint32_t point_step = 0;
  uint32_t row_step = 0;
  bool is_bigendian = false;
  bool is_dense = false;
  size_t data_bytes = 0;
  struct Field {
    std::string name;
    uint32_t offset = 0;
    uint8_t datatype = 0;
    uint32_t count = 0;
  };
  std::vector<Field> fields;
  std::vector<std::array<float, 3>> xyz;
};

struct HeightSample {
  double stamp = 0;
  std::string frame_id;
  float resolution = 0;
  uint32_t width = 0;
  uint32_t height = 0;
  std::array<float, 2> origin{};
  size_t data_count = 0;
  bool data_omitted = false;
  bool has_range = false;
  float minimum = 0;
  float maximum = 0;
  std::vector<float> data;
};

struct VideoSample {
  uint64_t time_frame = 0;
  size_t video720p_bytes = 0;
  size_t video360p_bytes = 0;
  size_t video180p_bytes = 0;
};

struct VoxelSample {
  double stamp = 0;
  std::string frame_id;
  double resolution = 0;
  std::array<double, 3> origin{};
  std::array<uint16_t, 3> width{};
  uint64_t src_size = 0;
  size_t data_bytes = 0;
};

template <typename Msg>
struct Slot {
  Msg message{};
  bool has = false;
  Steady::time_point received_at{};
  Wall::time_point observed_at{};
  std::string error;
  std::unique_ptr<unitree::robot::ChannelSubscriber<Msg>> subscriber;
};

float read_f32(const std::vector<uint8_t>& data, size_t offset) {
  if (offset + sizeof(float) > data.size()) return std::nanf("");
  float value = 0;
  std::memcpy(&value, data.data() + offset, sizeof(float));
  return value;
}

CloudSample summarize_cloud(const sensor_msgs::msg::dds_::PointCloud2_& cloud) {
  CloudSample sample;
  sample.stamp_sec = cloud.header().stamp().sec();
  sample.stamp_nanosec = cloud.header().stamp().nanosec();
  sample.frame_id = cloud.header().frame_id();
  sample.height = cloud.height();
  sample.width = cloud.width();
  sample.point_step = cloud.point_step();
  sample.row_step = cloud.row_step();
  sample.is_bigendian = cloud.is_bigendian();
  sample.is_dense = cloud.is_dense();
  sample.data_bytes = cloud.data().size();
  int x = -1;
  int y = -1;
  int z = -1;
  sample.fields.reserve(cloud.fields().size());
  for (const auto& field : cloud.fields()) {
    sample.fields.push_back(CloudSample::Field{field.name(), field.offset(), field.datatype(), field.count()});
    if (field.datatype() == kFloat32 && field.count() == 1) {
      if (field.name() == "x") x = static_cast<int>(field.offset());
      else if (field.name() == "y") y = static_cast<int>(field.offset());
      else if (field.name() == "z") z = static_cast<int>(field.offset());
    }
  }
  if (!sample.is_bigendian && x >= 0 && y >= 0 && z >= 0 && sample.point_step >= sizeof(float) * 3) {
    const auto points = static_cast<size_t>(sample.width) * std::max<uint32_t>(sample.height, 1);
    const size_t limit = std::min(points, kPointSampleLimit);
    for (size_t index = 0; index < limit; ++index) {
      const size_t base = index * sample.point_step;
      const float px = read_f32(cloud.data(), base + static_cast<size_t>(x));
      const float py = read_f32(cloud.data(), base + static_cast<size_t>(y));
      const float pz = read_f32(cloud.data(), base + static_cast<size_t>(z));
      if (!std::isfinite(px) || !std::isfinite(py) || !std::isfinite(pz)) continue;
      sample.xyz.push_back({px, py, pz});
    }
  }
  return sample;
}

HeightSample summarize_height(const unitree_go::msg::dds_::HeightMap_& map) {
  HeightSample sample;
  sample.stamp = map.stamp();
  sample.frame_id = map.frame_id();
  sample.resolution = map.resolution();
  sample.width = map.width();
  sample.height = map.height();
  sample.origin = map.origin();
  sample.data_count = map.data().size();
  sample.data_omitted = sample.data_count > kHeightMapLimit;
  const size_t kept = std::min(sample.data_count, kHeightMapLimit);
  sample.data.reserve(kept);
  for (size_t index = 0; index < sample.data_count; ++index) {
    const float value = map.data()[index];
    if (index < kept) sample.data.push_back(value);
    if (!std::isfinite(value)) continue;
    if (!sample.has_range) {
      sample.minimum = value;
      sample.maximum = value;
      sample.has_range = true;
    } else {
      sample.minimum = std::min(sample.minimum, value);
      sample.maximum = std::max(sample.maximum, value);
    }
  }
  return sample;
}

VideoSample summarize_video(const unitree_go::msg::dds_::Go2FrontVideoData_& video) {
  return VideoSample{video.time_frame(), video.video720p().size(), video.video360p().size(), video.video180p().size()};
}

VoxelSample summarize_voxel(const unitree_go::msg::dds_::VoxelMapCompressed_& map) {
  VoxelSample sample;
  sample.stamp = map.stamp();
  sample.frame_id = map.frame_id();
  sample.resolution = map.resolution();
  sample.origin = map.origin();
  sample.width = map.width();
  sample.src_size = map.src_size();
  sample.data_bytes = map.data().size();
  return sample;
}

void write_cloud(JsonWriter& json, const CloudSample& sample) {
  json.key("stamp_sec");
  json.integer(sample.stamp_sec);
  json.key("stamp_nanosec");
  json.integer(sample.stamp_nanosec);
  json.key("frame_id");
  json.string(sample.frame_id);
  json.key("height");
  json.integer(sample.height);
  json.key("width");
  json.integer(sample.width);
  json.key("point_step");
  json.integer(sample.point_step);
  json.key("row_step");
  json.integer(sample.row_step);
  json.key("is_bigendian");
  json.boolean(sample.is_bigendian);
  json.key("is_dense");
  json.boolean(sample.is_dense);
  json.key("data_bytes");
  json.integer(static_cast<long long>(sample.data_bytes));
  json.key("fields");
  json.begin_array();
  for (const auto& field : sample.fields) {
    json.begin_object();
    json.key("name");
    json.string(field.name);
    json.key("offset");
    json.integer(field.offset);
    json.key("datatype");
    json.integer(field.datatype);
    json.key("count");
    json.integer(field.count);
    json.end_object();
  }
  json.end_array();
  json.key("xyz_sample");
  json.begin_array();
  for (const auto& point : sample.xyz) {
    json.begin_array();
    json.number(point[0]);
    json.number(point[1]);
    json.number(point[2]);
    json.end_array();
  }
  json.end_array();
  json.key("payload_note");
  json.string("point bytes stay on the robot; xyz_sample holds the first finite points");
}

void write_sport(JsonWriter& json, const unitree_go::msg::dds_::SportModeState_& state) {
  json.key("stamp_sec");
  json.integer(state.stamp().sec());
  json.key("stamp_nanosec");
  json.integer(state.stamp().nanosec());
  json.key("error_code");
  json.integer(state.error_code());
  json.key("imu");
  imu(json, state.imu_state());
  json.key("mode");
  json.integer(state.mode());
  json.key("progress");
  json.number(state.progress());
  json.key("gait_type");
  json.integer(state.gait_type());
  json.key("foot_raise_height");
  json.number(state.foot_raise_height());
  json.key("position");
  numbers(json, state.position());
  json.key("body_height");
  json.number(state.body_height());
  json.key("velocity");
  numbers(json, state.velocity());
  json.key("yaw_speed");
  json.number(state.yaw_speed());
  json.key("range_obstacle");
  numbers(json, state.range_obstacle());
  json.key("foot_force");
  integers(json, state.foot_force());
  json.key("foot_position_body");
  numbers(json, state.foot_position_body());
  json.key("foot_speed_body");
  numbers(json, state.foot_speed_body());
  json.key("path_point");
  json.begin_array();
  for (const auto& point : state.path_point()) {
    json.begin_object();
    json.key("t_from_start");
    json.number(point.t_from_start());
    json.key("x");
    json.number(point.x());
    json.key("y");
    json.number(point.y());
    json.key("yaw");
    json.number(point.yaw());
    json.key("vx");
    json.number(point.vx());
    json.key("vy");
    json.number(point.vy());
    json.key("vyaw");
    json.number(point.vyaw());
    json.end_object();
  }
  json.end_array();
}

void write_low(JsonWriter& json, const unitree_go::msg::dds_::LowState_& state) {
  json.key("head");
  integers(json, state.head());
  json.key("level_flag");
  json.integer(state.level_flag());
  json.key("frame_reserve");
  json.integer(state.frame_reserve());
  json.key("sn");
  integers(json, state.sn());
  json.key("version");
  integers(json, state.version());
  json.key("bandwidth");
  json.integer(state.bandwidth());
  json.key("imu");
  imu(json, state.imu_state());
  json.key("motors");
  json.begin_array();
  for (size_t index = 0; index < state.motor_state().size(); ++index) {
    const auto& motor = state.motor_state()[index];
    json.begin_object();
    json.key("index");
    json.integer(static_cast<long long>(index));
    const char* name = joint_name(index);
    if (name != nullptr) {
      json.key("joint");
      json.string(name);
    }
    json.key("mode");
    json.integer(motor.mode());
    json.key("q");
    json.number(motor.q());
    json.key("dq");
    json.number(motor.dq());
    json.key("ddq");
    json.number(motor.ddq());
    json.key("tau_est");
    json.number(motor.tau_est());
    json.key("q_raw");
    json.number(motor.q_raw());
    json.key("dq_raw");
    json.number(motor.dq_raw());
    json.key("ddq_raw");
    json.number(motor.ddq_raw());
    json.key("temperature");
    json.integer(motor.temperature());
    json.key("lost");
    json.integer(motor.lost());
    json.end_object();
  }
  json.end_array();
  const auto& bms = state.bms_state();
  json.key("bms");
  json.begin_object();
  json.key("version_high");
  json.integer(bms.version_high());
  json.key("version_low");
  json.integer(bms.version_low());
  json.key("status");
  json.integer(bms.status());
  json.key("soc");
  json.integer(bms.soc());
  json.key("current");
  json.integer(bms.current());
  json.key("cycle");
  json.integer(bms.cycle());
  json.key("bq_ntc");
  integers(json, bms.bq_ntc());
  json.key("mcu_ntc");
  integers(json, bms.mcu_ntc());
  json.key("cell_vol");
  integers(json, bms.cell_vol());
  json.end_object();
  json.key("foot_force");
  integers(json, state.foot_force());
  json.key("foot_force_est");
  integers(json, state.foot_force_est());
  json.key("tick");
  json.integer(state.tick());
  json.key("wireless_remote");
  json.string("withheld");
  json.key("bit_flag");
  json.integer(state.bit_flag());
  json.key("adc_reel");
  json.number(state.adc_reel());
  json.key("temperature_ntc1");
  json.integer(state.temperature_ntc1());
  json.key("temperature_ntc2");
  json.integer(state.temperature_ntc2());
  json.key("power_v");
  json.number(state.power_v());
  json.key("power_a");
  json.number(state.power_a());
  json.key("fan_frequency");
  integers(json, state.fan_frequency());
  json.key("reserve");
  json.integer(state.reserve());
  json.key("crc");
  json.integer(state.crc());
}

void write_lidar_state(JsonWriter& json, const unitree_go::msg::dds_::LidarState_& state) {
  json.key("stamp");
  json.number(state.stamp());
  json.key("firmware_version");
  json.string(state.firmware_version());
  json.key("software_version");
  json.string(state.software_version());
  json.key("sdk_version");
  json.string(state.sdk_version());
  json.key("sys_rotation_speed");
  json.number(state.sys_rotation_speed());
  json.key("com_rotation_speed");
  json.number(state.com_rotation_speed());
  json.key("error_state");
  json.integer(state.error_state());
  json.key("dirty_percentage");
  json.integer(state.dirty_percentage());
  json.key("cloud_frequency");
  json.number(state.cloud_frequency());
  json.key("cloud_packet_loss_rate");
  json.number(state.cloud_packet_loss_rate());
  json.key("cloud_size");
  json.integer(state.cloud_size());
  json.key("cloud_scan_num");
  json.integer(state.cloud_scan_num());
  json.key("imu_frequency");
  json.number(state.imu_frequency());
  json.key("imu_packet_loss_rate");
  json.number(state.imu_packet_loss_rate());
  json.key("imu_rpy");
  numbers(json, state.imu_rpy());
  json.key("serial_recv_stamp");
  json.number(state.serial_recv_stamp());
  json.key("serial_buffer_size");
  json.integer(state.serial_buffer_size());
  json.key("serial_buffer_read");
  json.integer(state.serial_buffer_read());
}

void write_uwb(JsonWriter& json, const unitree_go::msg::dds_::UwbState_& state) {
  json.key("version");
  integers(json, state.version());
  json.key("channel");
  json.integer(state.channel());
  json.key("joy_mode");
  json.integer(state.joy_mode());
  json.key("orientation_est");
  json.number(state.orientation_est());
  json.key("pitch_est");
  json.number(state.pitch_est());
  json.key("distance_est");
  json.number(state.distance_est());
  json.key("yaw_est");
  json.number(state.yaw_est());
  json.key("tag_roll");
  json.number(state.tag_roll());
  json.key("tag_pitch");
  json.number(state.tag_pitch());
  json.key("tag_yaw");
  json.number(state.tag_yaw());
  json.key("base_roll");
  json.number(state.base_roll());
  json.key("base_pitch");
  json.number(state.base_pitch());
  json.key("base_yaw");
  json.number(state.base_yaw());
  json.key("joystick");
  numbers(json, state.joystick());
  json.key("error_state");
  json.integer(state.error_state());
  json.key("buttons");
  json.integer(state.buttons());
  json.key("enabled_from_app");
  json.integer(state.enabled_from_app());
}

void write_wireless(JsonWriter& json, const unitree_go::msg::dds_::WirelessController_& state) {
  json.key("lx");
  json.number(state.lx());
  json.key("ly");
  json.number(state.ly());
  json.key("rx");
  json.number(state.rx());
  json.key("ry");
  json.number(state.ry());
  json.key("keys");
  json.integer(state.keys());
}

void write_video(JsonWriter& json, const VideoSample& sample) {
  json.key("time_frame");
  json.integer(static_cast<long long>(sample.time_frame));
  json.key("video720p_bytes");
  json.integer(static_cast<long long>(sample.video720p_bytes));
  json.key("video360p_bytes");
  json.integer(static_cast<long long>(sample.video360p_bytes));
  json.key("video180p_bytes");
  json.integer(static_cast<long long>(sample.video180p_bytes));
  json.key("payload_note");
  json.string("encoded frames stay on the robot");
}

void write_height(JsonWriter& json, const HeightSample& sample) {
  json.key("stamp");
  json.number(sample.stamp);
  json.key("frame_id");
  json.string(sample.frame_id);
  json.key("resolution");
  json.number(sample.resolution);
  json.key("width");
  json.integer(sample.width);
  json.key("height");
  json.integer(sample.height);
  json.key("origin");
  numbers(json, sample.origin);
  json.key("data_count");
  json.integer(static_cast<long long>(sample.data_count));
  json.key("data_omitted");
  json.boolean(sample.data_omitted);
  json.key("minimum");
  if (sample.has_range) json.number(sample.minimum);
  else json.null();
  json.key("maximum");
  if (sample.has_range) json.number(sample.maximum);
  else json.null();
  json.key("data");
  numbers(json, sample.data);
}

void write_voxel(JsonWriter& json, const VoxelSample& sample) {
  json.key("stamp");
  json.number(sample.stamp);
  json.key("frame_id");
  json.string(sample.frame_id);
  json.key("resolution");
  json.number(sample.resolution);
  json.key("origin");
  numbers(json, sample.origin);
  json.key("width");
  integers(json, sample.width);
  json.key("src_size");
  json.integer(static_cast<long long>(sample.src_size));
  json.key("data_bytes");
  json.integer(static_cast<long long>(sample.data_bytes));
  json.key("payload_note");
  json.string("compressed bytes stay on the robot");
}

void write_odometry(JsonWriter& json, const nav_msgs::msg::dds_::Odometry_& state) {
  json.key("stamp_sec");
  json.integer(state.header().stamp().sec());
  json.key("stamp_nanosec");
  json.integer(state.header().stamp().nanosec());
  json.key("frame_id");
  json.string(state.header().frame_id());
  json.key("child_frame_id");
  json.string(state.child_frame_id());
  const auto& pose = state.pose().pose();
  json.key("position");
  json.begin_array();
  json.number(pose.position().x());
  json.number(pose.position().y());
  json.number(pose.position().z());
  json.end_array();
  json.key("orientation");
  json.begin_array();
  json.number(pose.orientation().x());
  json.number(pose.orientation().y());
  json.number(pose.orientation().z());
  json.number(pose.orientation().w());
  json.end_array();
  const auto& twist = state.twist().twist();
  json.key("linear");
  json.begin_array();
  json.number(twist.linear().x());
  json.number(twist.linear().y());
  json.number(twist.linear().z());
  json.end_array();
  json.key("angular");
  json.begin_array();
  json.number(twist.angular().x());
  json.number(twist.angular().y());
  json.number(twist.angular().z());
  json.end_array();
  json.key("pose_covariance");
  numbers(json, state.pose().covariance());
  json.key("twist_covariance");
  numbers(json, state.twist().covariance());
}

struct CloudSlot {
  CloudSample message{};
  bool has = false;
  Steady::time_point received_at{};
  Wall::time_point observed_at{};
  std::string error;
  std::unique_ptr<unitree::robot::ChannelSubscriber<sensor_msgs::msg::dds_::PointCloud2_>> subscriber;
};

struct HeightSlot {
  HeightSample message{};
  bool has = false;
  Steady::time_point received_at{};
  Wall::time_point observed_at{};
  std::string error;
  std::unique_ptr<unitree::robot::ChannelSubscriber<unitree_go::msg::dds_::HeightMap_>> subscriber;
};

struct VideoSlot {
  VideoSample message{};
  bool has = false;
  Steady::time_point received_at{};
  Wall::time_point observed_at{};
  std::string error;
  std::unique_ptr<unitree::robot::ChannelSubscriber<unitree_go::msg::dds_::Go2FrontVideoData_>> subscriber;
};

struct VoxelSlot {
  VoxelSample message{};
  bool has = false;
  Steady::time_point received_at{};
  Wall::time_point observed_at{};
  std::string error;
  std::unique_ptr<unitree::robot::ChannelSubscriber<unitree_go::msg::dds_::VoxelMapCompressed_>> subscriber;
};

bool fresh_sample(bool closed, bool has, Steady::time_point received_at) {
  return !closed && has && Steady::now() - received_at <= kFreshness;
}

void stream_header(JsonWriter& json, const char* id, const char* topic, const char* event_type,
                   bool language_summary, bool available, Wall::time_point observed_at, const std::string& error) {
  json.begin_object();
  json.key("id");
  json.string(id);
  json.key("topic");
  json.string(topic);
  json.key("event_type");
  json.string(event_type);
  json.key("language_summary");
  json.boolean(language_summary);
  json.key("available");
  json.boolean(available);
  json.key("observed_at");
  if (available) json.string(format_utc(observed_at));
  else json.null();
  json.key("error");
  if (error.empty()) json.null();
  else json.string(error);
  json.key("payload");
  if (!available) json.null();
  else json.begin_object();
}
}  // namespace

struct Go2SensorCache::Impl {
  mutable std::mutex mutex;
  bool closed = false;
  Slot<unitree_go::msg::dds_::LidarState_> lidar_state;
  Slot<unitree_go::msg::dds_::UwbState_> uwb;
  Slot<unitree_go::msg::dds_::WirelessController_> wireless;
  Slot<nav_msgs::msg::dds_::Odometry_> odometry;
  CloudSlot cloud;
  CloudSlot cloud_deskewed;
  CloudSlot voxel_map;
  HeightSlot height_map;
  VoxelSlot voxel_compressed;
  VideoSlot front_video;

  template <typename Msg, typename Reduce>
  void subscribe(Slot<Msg>& slot, const char* topic, Reduce reduce) {
    try {
      auto subscriber = std::make_unique<unitree::robot::ChannelSubscriber<Msg>>(topic);
      subscriber->InitChannel(
          [this, &slot, reduce](const void* message) {
            if (!message) return;
            auto sample = reduce(*static_cast<const Msg*>(message));
            std::lock_guard<std::mutex> lock(mutex);
            if (closed) return;
            slot.message = std::move(sample);
            slot.has = true;
            slot.received_at = Steady::now();
            slot.observed_at = Wall::now();
          },
          1);
      slot.subscriber = std::move(subscriber);
    } catch (const std::exception& exc) {
      slot.error = exc.what();
    } catch (...) {
      slot.error = "unable to subscribe";
    }
  }
};

namespace {
template <typename SlotT, typename Msg, typename Reduce>
void subscribe_reduced(Go2SensorCache::Impl* impl, SlotT& slot, const char* topic, Reduce reduce) {
  try {
    auto subscriber = std::make_unique<unitree::robot::ChannelSubscriber<Msg>>(topic);
    subscriber->InitChannel(
        [impl, &slot, reduce](const void* message) {
          if (!message) return;
          auto sample = reduce(*static_cast<const Msg*>(message));
          std::lock_guard<std::mutex> lock(impl->mutex);
          if (impl->closed) return;
          slot.message = std::move(sample);
          slot.has = true;
          slot.received_at = Steady::now();
          slot.observed_at = Wall::now();
        },
        1);
    slot.subscriber = std::move(subscriber);
  } catch (const std::exception& exc) {
    slot.error = exc.what();
  } catch (...) {
    slot.error = "unable to subscribe";
  }
}
}  // namespace

Go2SensorCache::Go2SensorCache() : impl_(new Impl) {}

Go2SensorCache::~Go2SensorCache() {
  close();
  delete impl_;
  impl_ = nullptr;
}

void Go2SensorCache::open() {
  auto* impl = impl_;
  impl->subscribe(impl->lidar_state, "rt/utlidar/lidar_state",
                  [](const unitree_go::msg::dds_::LidarState_& message) { return message; });
  impl->subscribe(impl->uwb, "rt/uwbstate",
                  [](const unitree_go::msg::dds_::UwbState_& message) { return message; });
  impl->subscribe(impl->wireless, "rt/wirelesscontroller",
                  [](const unitree_go::msg::dds_::WirelessController_& message) { return message; });
  impl->subscribe(impl->odometry, "rt/utlidar/robot_odom",
                  [](const nav_msgs::msg::dds_::Odometry_& message) { return message; });
  subscribe_reduced<CloudSlot, sensor_msgs::msg::dds_::PointCloud2_>(
      impl, impl->cloud, "rt/utlidar/cloud", summarize_cloud);
  subscribe_reduced<CloudSlot, sensor_msgs::msg::dds_::PointCloud2_>(
      impl, impl->cloud_deskewed, "rt/utlidar/cloud_deskewed", summarize_cloud);
  subscribe_reduced<CloudSlot, sensor_msgs::msg::dds_::PointCloud2_>(
      impl, impl->voxel_map, "rt/utlidar/voxel_map", summarize_cloud);
  subscribe_reduced<HeightSlot, unitree_go::msg::dds_::HeightMap_>(
      impl, impl->height_map, "rt/utlidar/height_map_array", summarize_height);
  subscribe_reduced<VoxelSlot, unitree_go::msg::dds_::VoxelMapCompressed_>(
      impl, impl->voxel_compressed, "rt/utlidar/voxel_map_compressed", summarize_voxel);
  subscribe_reduced<VideoSlot, unitree_go::msg::dds_::Go2FrontVideoData_>(
      impl, impl->front_video, "rt/frontvideostream", summarize_video);
}

namespace {
template <typename Subscriber>
void close_subscriber(std::unique_ptr<Subscriber>& subscriber) noexcept {
  if (!subscriber) return;
  try {
    subscriber->CloseChannel();
  } catch (...) {
  }
  subscriber.reset();
}
}  // namespace

void Go2SensorCache::close() noexcept {
  if (impl_ == nullptr) return;
  decltype(impl_->lidar_state.subscriber) lidar;
  decltype(impl_->uwb.subscriber) uwb;
  decltype(impl_->wireless.subscriber) wireless;
  decltype(impl_->odometry.subscriber) odometry;
  decltype(impl_->cloud.subscriber) cloud;
  decltype(impl_->cloud_deskewed.subscriber) cloud_deskewed;
  decltype(impl_->voxel_map.subscriber) voxel_map;
  decltype(impl_->height_map.subscriber) height_map;
  decltype(impl_->voxel_compressed.subscriber) voxel_compressed;
  decltype(impl_->front_video.subscriber) front_video;
  {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    if (impl_->closed) return;
    impl_->closed = true;
    lidar = std::move(impl_->lidar_state.subscriber);
    uwb = std::move(impl_->uwb.subscriber);
    wireless = std::move(impl_->wireless.subscriber);
    odometry = std::move(impl_->odometry.subscriber);
    cloud = std::move(impl_->cloud.subscriber);
    cloud_deskewed = std::move(impl_->cloud_deskewed.subscriber);
    voxel_map = std::move(impl_->voxel_map.subscriber);
    height_map = std::move(impl_->height_map.subscriber);
    voxel_compressed = std::move(impl_->voxel_compressed.subscriber);
    front_video = std::move(impl_->front_video.subscriber);
  }
  close_subscriber(lidar);
  close_subscriber(uwb);
  close_subscriber(wireless);
  close_subscriber(odometry);
  close_subscriber(cloud);
  close_subscriber(cloud_deskewed);
  close_subscriber(voxel_map);
  close_subscriber(height_map);
  close_subscriber(voxel_compressed);
  close_subscriber(front_video);
}

std::string Go2SensorCache::to_json(
    bool sport_fresh,
    const unitree_go::msg::dds_::SportModeState_& sport,
    Wall::time_point sport_observed_at,
    bool low_fresh,
    const unitree_go::msg::dds_::LowState_& low,
    Wall::time_point low_observed_at) const {
  std::lock_guard<std::mutex> lock(impl_->mutex);
  JsonWriter json;
  json.begin_object();
  json.key("streams");
  json.begin_array();

  auto emit = [&json](const char* id, const char* topic, const char* event_type, bool language, bool available,
                      Wall::time_point observed_at, const std::string& error, const auto& write_payload) {
    stream_header(json, id, topic, event_type, language, available, observed_at, error);
    if (available) {
      write_payload();
      json.end_object();
    }
    json.end_object();
  };

  emit("sport", "rt/sportmodestate", "robot.sport_state", true, sport_fresh, sport_observed_at, "",
       [&] { write_sport(json, sport); });
  emit("low", "rt/lowstate", "robot.low_state", true, low_fresh, low_observed_at, "",
       [&] { write_low(json, low); });

  auto emit_slot = [&](const char* id, const char* topic, const char* event_type, bool language, auto& slot,
                       const auto& writer) {
    const bool available = fresh_sample(impl_->closed, slot.has, slot.received_at);
    emit(id, topic, event_type, language, available, slot.observed_at, slot.error,
         [&] { writer(json, slot.message); });
  };

  emit_slot("lidar_state", "rt/utlidar/lidar_state", "robot.lidar_state", true, impl_->lidar_state, write_lidar_state);
  emit_slot("uwb", "rt/uwbstate", "robot.uwb_state", true, impl_->uwb, write_uwb);
  emit_slot("wireless_controller", "rt/wirelesscontroller", "robot.wireless_controller", false, impl_->wireless,
            write_wireless);
  emit_slot("odometry", "rt/utlidar/robot_odom", "robot.lidar_odometry", true, impl_->odometry, write_odometry);
  emit_slot("cloud", "rt/utlidar/cloud", "robot.lidar_cloud", true, impl_->cloud, write_cloud);
  emit_slot("cloud_deskewed", "rt/utlidar/cloud_deskewed", "robot.lidar_cloud_deskewed", true, impl_->cloud_deskewed,
            write_cloud);
  emit_slot("voxel_map", "rt/utlidar/voxel_map", "robot.voxel_map", true, impl_->voxel_map, write_cloud);
  emit_slot("height_map", "rt/utlidar/height_map_array", "robot.height_map", true, impl_->height_map, write_height);
  emit_slot("voxel_map_compressed", "rt/utlidar/voxel_map_compressed", "robot.voxel_map_compressed", false,
            impl_->voxel_compressed, write_voxel);
  emit_slot("front_video", "rt/frontvideostream", "robot.front_video", false, impl_->front_video, write_video);

  json.end_array();
  json.end_object();
  return json.str();
}
