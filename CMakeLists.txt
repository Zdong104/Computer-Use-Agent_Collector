cmake_minimum_required(VERSION 3.18)
project(cua_capture CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# ── Dependencies ─────────────────────────────────────────────
find_package(PkgConfig REQUIRED)
pkg_check_modules(PIPEWIRE REQUIRED IMPORTED_TARGET libpipewire-0.3)
pkg_check_modules(LIBEVDEV REQUIRED IMPORTED_TARGET libevdev)
pkg_check_modules(TURBOJPEG REQUIRED IMPORTED_TARGET libturbojpeg)

# Python3 — prefer virtualenv over system/conda
set(Python3_FIND_VIRTUALENV FIRST)
find_package(Python3 REQUIRED COMPONENTS Interpreter Development)
message(STATUS "Python3 executable: ${Python3_EXECUTABLE}")

# pybind11 — try cmake config from pip install, then system
execute_process(
    COMMAND ${Python3_EXECUTABLE} -m pybind11 --cmakedir
    OUTPUT_VARIABLE PYBIND11_CMAKE_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
    RESULT_VARIABLE PYBIND11_CMAKE_RESULT
)
if(PYBIND11_CMAKE_RESULT EQUAL 0 AND PYBIND11_CMAKE_DIR)
    set(pybind11_DIR "${PYBIND11_CMAKE_DIR}" CACHE PATH "pybind11 cmake dir" FORCE)
    message(STATUS "pybind11 cmake dir from pip: ${PYBIND11_CMAKE_DIR}")
else()
    # Fallback: scan common venv site-packages locations
    file(GLOB _PB11_CANDIDATES
        "${Python3_STDLIB}/../site-packages/pybind11/share/cmake/pybind11"
        "${CMAKE_SOURCE_DIR}/.venv/lib/python*/site-packages/pybind11/share/cmake/pybind11"
    )
    if(_PB11_CANDIDATES)
        list(GET _PB11_CANDIDATES 0 _PB11_DIR)
        set(pybind11_DIR "${_PB11_DIR}" CACHE PATH "pybind11 cmake dir" FORCE)
        message(STATUS "pybind11 cmake dir from venv scan: ${_PB11_DIR}")
    endif()
endif()

find_package(pybind11 REQUIRED)

# ── pybind11 Python module ───────────────────────────────────
pybind11_add_module(cua_capture
    src/ring_buffer.cpp
    src/pipewire_capture.cpp
    src/input_monitor.cpp
    src/action_engine.cpp
    src/bindings.cpp
)

target_include_directories(cua_capture PRIVATE
    include/
)
target_link_libraries(cua_capture PRIVATE
    PkgConfig::PIPEWIRE
    PkgConfig::LIBEVDEV
    PkgConfig::TURBOJPEG
    pthread
)

# ── Standalone benchmark binary ──────────────────────────────
add_executable(benchmark
    tests/benchmark.cpp
    src/ring_buffer.cpp
    src/action_engine.cpp
    src/input_monitor.cpp
    src/pipewire_capture.cpp
)
target_include_directories(benchmark PRIVATE
    include/
)
target_link_libraries(benchmark PRIVATE
    PkgConfig::PIPEWIRE
    PkgConfig::LIBEVDEV
    PkgConfig::TURBOJPEG
    pthread
)
