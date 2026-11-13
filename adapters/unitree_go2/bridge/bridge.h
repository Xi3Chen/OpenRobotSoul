#pragma once

#include <stddef.h>

#if defined(_WIN32)
#  if defined(UNITREE_GO2_BRIDGE_BUILD)
#    define GO2_BRIDGE_API __declspec(dllexport)
#  else
#    define GO2_BRIDGE_API __declspec(dllimport)
#  endif
#else
#  define GO2_BRIDGE_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

GO2_BRIDGE_API void* go2_bridge_create(const char* network_interface,
                                        char* error, size_t error_capacity);
GO2_BRIDGE_API int go2_bridge_health(void* bridge, char* error, size_t error_capacity);
GO2_BRIDGE_API int go2_bridge_read_state(void* bridge, char* output,
                                          size_t output_capacity,
                                          char* error, size_t error_capacity);
GO2_BRIDGE_API int go2_bridge_read_battery(void* bridge, char* output,
                                            size_t output_capacity,
                                            char* error, size_t error_capacity);
GO2_BRIDGE_API int go2_bridge_call(void* bridge, const char* action,
                                   const double* numbers, size_t number_count,
                                   int has_flag, int flag,
                                   char* output, size_t output_capacity,
                                   char* error, size_t error_capacity);
GO2_BRIDGE_API int go2_bridge_close(void* bridge, char* error, size_t error_capacity);
GO2_BRIDGE_API void go2_bridge_destroy(void* bridge);

#ifdef __cplusplus
}
#endif
