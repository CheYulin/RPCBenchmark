cmake_minimum_required(VERSION 3.20)
project(rpc_framework_lab LANGUAGES C CXX)

set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_CONFIGURATION_TYPES AND NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()
# Apache bRPC 1.15's own minimum policy version predates CMake 4.
set(CMAKE_POLICY_VERSION_MINIMUM 3.5)

if(POLICY CMP0135)
  cmake_policy(SET CMP0135 NEW)
endif()
include(FetchContent)
find_package(Threads REQUIRED)
option(RPC_LAB_BUILD_BRPC "Build the bRPC adapter" ON)
option(RPC_LAB_BUILD_CORO "Build the coro_rpc adapter" ON)
option(RPC_LAB_BUILD_PIPELINE_EXPERIMENT "Build the independent native coro_rpc pipeline experiment" OFF)
set(RPC_LAB_BRPC_ARCHIVE "https://archive.apache.org/dist/brpc/1.15.0/apache-brpc-1.15.0-src.tar.gz"
    CACHE STRING "Local path or URL of the exact Apache bRPC 1.15.0 release archive")
set(RPC_LAB_YLT_ARCHIVE "https://codeload.github.com/alibaba/yalantinglibs/tar.gz/45c6463c0afbcc46262e373effa30635be3846a0"
    CACHE STRING "Local path or URL of the exact Mooncake-pinned yaLanTingLibs archive")
set(RPC_LAB_BRPC_ROOT "" CACHE PATH "Optional prefix of an already-built, shared bRPC 1.15.0 install")

add_library(rpc_lab_common STATIC src/lab.cpp)
target_include_directories(rpc_lab_common PUBLIC src)
target_link_libraries(rpc_lab_common PUBLIC Threads::Threads)

if(RPC_LAB_BUILD_BRPC)
  find_package(Protobuf REQUIRED)
  if(RPC_LAB_BRPC_ROOT)
    find_path(RPC_LAB_BRPC_HEADERS brpc/channel.h
              PATHS "${RPC_LAB_BRPC_ROOT}/include" NO_DEFAULT_PATH REQUIRED)
    find_file(RPC_LAB_BRPC_SHARED libbrpc.so
              PATHS "${RPC_LAB_BRPC_ROOT}/lib" "${RPC_LAB_BRPC_ROOT}/lib64"
                    "${RPC_LAB_BRPC_ROOT}/lib/${CMAKE_LIBRARY_ARCHITECTURE}"
              NO_DEFAULT_PATH REQUIRED)
    add_library(lab_brpc_shared SHARED IMPORTED)
    set_target_properties(lab_brpc_shared PROPERTIES IMPORTED_LOCATION "${RPC_LAB_BRPC_SHARED}")
  else()
    set(BUILD_SHARED_LIBS ON CACHE BOOL "Build shared bRPC" FORCE)
    set(BUILD_BRPC_TOOLS OFF CACHE BOOL "Build bRPC tools" FORCE)
    set(BUILD_UNIT_TESTS OFF CACHE BOOL "Build bRPC tests" FORCE)
    set(WITH_DEBUG_SYMBOLS OFF CACHE BOOL "Build bRPC with debug symbols" FORCE)
    FetchContent_Declare(lab_brpc_upstream
      URL "${RPC_LAB_BRPC_ARCHIVE}"
      URL_HASH SHA256=0bc8c2aee810c96e6c77886f828fbfdf32ae353ce997eb46f2772c0088010c35)
    FetchContent_MakeAvailable(lab_brpc_upstream)
    add_library(lab_brpc_shared ALIAS brpc-shared)
    set(RPC_LAB_BRPC_HEADERS "${lab_brpc_upstream_SOURCE_DIR}/src"
                            "${lab_brpc_upstream_BINARY_DIR}")
  endif()
  protobuf_generate_cpp(ECHO_PROTO_SOURCES ECHO_PROTO_HEADERS src/echo.proto)
  add_executable(rpc_brpc src/brpc.cpp ${ECHO_PROTO_SOURCES})
  target_include_directories(rpc_brpc PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
  target_include_directories(rpc_brpc SYSTEM PRIVATE ${RPC_LAB_BRPC_HEADERS})
  target_link_libraries(rpc_brpc PRIVATE rpc_lab_common lab_brpc_shared protobuf::libprotobuf)
endif()

if(RPC_LAB_BUILD_CORO)
  FetchContent_Declare(lab_ylt_upstream
    URL "${RPC_LAB_YLT_ARCHIVE}"
    URL_HASH SHA256=baa6b5c9ffcc0af8252742bba8f0fb9c6d37387c13b2ff5b26d29c6aaff6ee37
    SOURCE_SUBDIR include)
  FetchContent_MakeAvailable(lab_ylt_upstream)
  add_executable(rpc_coro src/coro.cpp)
  target_include_directories(rpc_coro SYSTEM PRIVATE
    "${lab_ylt_upstream_SOURCE_DIR}/include"
    "${lab_ylt_upstream_SOURCE_DIR}/include/ylt/thirdparty"
    "${lab_ylt_upstream_SOURCE_DIR}/include/ylt/standalone")
  target_link_libraries(rpc_coro PRIVATE rpc_lab_common)
  if(RPC_LAB_BUILD_PIPELINE_EXPERIMENT)
    add_executable(rpc_coro_pipeline src/coro_pipeline.cpp)
    target_include_directories(rpc_coro_pipeline SYSTEM PRIVATE
      "${lab_ylt_upstream_SOURCE_DIR}/include"
      "${lab_ylt_upstream_SOURCE_DIR}/include/ylt/thirdparty"
      "${lab_ylt_upstream_SOURCE_DIR}/include/ylt/standalone")
    target_link_libraries(rpc_coro_pipeline PRIVATE rpc_lab_common)
  endif()
endif()

include(CTest)
if(BUILD_TESTING)
  find_package(Python3 REQUIRED COMPONENTS Interpreter)
  foreach(adapter rpc_brpc rpc_coro)
    if(TARGET ${adapter})
      add_test(NAME ${adapter}_smoke
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/smoke.py"
                "$<TARGET_FILE:${adapter}>")
      set_tests_properties(${adapter}_smoke PROPERTIES TIMEOUT 25)
    endif()
  endforeach()
  if(TARGET rpc_coro)
    add_test(NAME rpc_coro_pooled_smoke
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/smoke.py"
              "$<TARGET_FILE:rpc_coro>" --handler-workers 4)
    set_tests_properties(rpc_coro_pooled_smoke PROPERTIES TIMEOUT 25)
  endif()
  if(TARGET rpc_coro_pipeline)
    add_test(NAME rpc_coro_pipeline_checks
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/pipeline_smoke.py"
              "$<TARGET_FILE:rpc_coro_pipeline>")
    set_tests_properties(rpc_coro_pipeline_checks PROPERTIES TIMEOUT 20)
    add_test(NAME rpc_coro_pipeline_lifecycle
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/pipeline_lifecycle.py"
              "$<TARGET_FILE:rpc_coro_pipeline>")
    set_tests_properties(rpc_coro_pipeline_lifecycle PROPERTIES TIMEOUT 40)
  endif()
endif()
