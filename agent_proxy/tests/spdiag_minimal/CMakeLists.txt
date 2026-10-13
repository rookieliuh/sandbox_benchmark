cmake_minimum_required(VERSION 3.16)
project(spdiag_minimal LANGUAGES CXX)

# Standalone: no Agent Proxy, protobuf, bRPC, backend, or reference-repository source.
find_package(SpDiag CONFIG REQUIRED)
if(NOT TARGET SpDiag::spdiag_lib)
  message(FATAL_ERROR "SpDiag::spdiag_lib is required")
endif()
add_executable(spdiag-minimal main.cc)
target_compile_features(spdiag-minimal PRIVATE cxx_std_17)
target_include_directories(spdiag-minimal PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
target_link_libraries(spdiag-minimal PRIVATE SpDiag::spdiag_lib)
