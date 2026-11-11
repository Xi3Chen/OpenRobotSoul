cmake_minimum_required(VERSION 3.16)
project(unitree_go2_bridge LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(UNITREE_SDK_ROOT "${CMAKE_CURRENT_LIST_DIR}/../../unitree_sdk2" CACHE PATH
    "Path to the pinned Unitree SDK 2 checkout")

if(NOT EXISTS "${UNITREE_SDK_ROOT}/CMakeLists.txt")
    message(FATAL_ERROR "Unitree SDK 2 checkout not found at ${UNITREE_SDK_ROOT}")
endif()

set(UNITREE_SDK_ARCH "${CMAKE_SYSTEM_PROCESSOR}" CACHE STRING
    "Architecture directory under the Unitree SDK 2 lib folders")
if(UNITREE_SDK_ARCH STREQUAL "AMD64" OR UNITREE_SDK_ARCH STREQUAL "amd64")
    set(UNITREE_SDK_ARCH "x86_64")
elseif(UNITREE_SDK_ARCH STREQUAL "aarch64")
    set(UNITREE_SDK_ARCH "aarch64")
endif()
set(UNITREE_SDK_LIBRARY "${UNITREE_SDK_ROOT}/lib/${UNITREE_SDK_ARCH}/libunitree_sdk2.a")
set(UNITREE_DDS_LIBRARY_DIR "${UNITREE_SDK_ROOT}/thirdparty/lib/${UNITREE_SDK_ARCH}")
if(NOT EXISTS "${UNITREE_SDK_LIBRARY}")
    message(FATAL_ERROR "Unitree SDK library not found: ${UNITREE_SDK_LIBRARY}")
endif()
if(NOT EXISTS "${UNITREE_DDS_LIBRARY_DIR}/libddsc.so" OR
   NOT EXISTS "${UNITREE_DDS_LIBRARY_DIR}/libddscxx.so")
    message(FATAL_ERROR "Unitree DDS libraries not found under ${UNITREE_DDS_LIBRARY_DIR}")
endif()

set(BUILD_EXAMPLES OFF CACHE BOOL "Do not build all Unitree examples for the bridge" FORCE)
add_subdirectory("${UNITREE_SDK_ROOT}" "${CMAKE_CURRENT_BINARY_DIR}/unitree-sdk2" EXCLUDE_FROM_ALL)

add_library(unitree_go2_bridge SHARED bridge.cpp)
target_include_directories(unitree_go2_bridge PRIVATE
    "${UNITREE_SDK_ROOT}/include"
    "${UNITREE_SDK_ROOT}/thirdparty/include"
    "${UNITREE_SDK_ROOT}/thirdparty/include/ddscxx")
target_link_libraries(unitree_go2_bridge PRIVATE unitree_sdk2)
target_compile_definitions(unitree_go2_bridge PRIVATE UNITREE_GO2_BRIDGE_BUILD)
set_target_properties(unitree_go2_bridge PROPERTIES
    OUTPUT_NAME openrobotsoul_go2_bridge
    CXX_VISIBILITY_PRESET hidden
    BUILD_RPATH "$ORIGIN"
    BUILD_WITH_INSTALL_RPATH TRUE
    INSTALL_RPATH "$ORIGIN")
