cmake_minimum_required(VERSION 3.16)
project(shesha-daemon C)

add_compile_options(-Wall -Wextra -Werror)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

set(STYX_DIR ${CMAKE_CURRENT_SOURCE_DIR}/platforms/styx/firmware/src)
set(DEIMOS_DIR ${CMAKE_CURRENT_SOURCE_DIR}/platforms/deimos/firmware/src)
set(LIB80211_DIR ${CMAKE_CURRENT_SOURCE_DIR}/extern/lib80211)

# fftw3.h for the shesha-local split FFT (src/wf_fft.c).  lib80211 links fftw3f
# PUBLIC, so the library is already transitive; only the header path is ours.
find_path(FFTW3F_INCLUDE fftw3.h)

# Force fftw3 backend for ARM cross-compile
set(LIB80211_FFT_BACKEND "fftw3" CACHE STRING "" FORCE)
set(LIB80211_BUILD_TESTS OFF CACHE BOOL "" FORCE)

# Build styx HAL + DMA as static library (maps all register regions
# including generic accelerator slots at REG_ACCEL_A/B_BASE)
add_library(styx_hal STATIC
    ${STYX_DIR}/hal.c
    ${STYX_DIR}/dma_rx.c
    ${STYX_DIR}/dma_tx.c
    ${STYX_DIR}/convert.c
)
target_include_directories(styx_hal PUBLIC ${STYX_DIR})

# Deimos accelerator library (deimos_rx — fabric WiFi decode via tag FIFO)
# Compiled from submodule; uses styx HAL for register access.
# PRIVATE: deimos hal.h is only needed internally by deimos_rx.c
# INTERFACE: consumers can #include "deimos_rx.h"
#
# deimos/firmware/src/hal.h is a forwarder to the nested platform/styx
# checkout deimos uses for standalone builds.  Shesha vendors styx itself at
# firmware/platforms/styx, so neutralize that forwarder (DEIMOS_HAL_H) and
# pull its two includes — styx's hal.h and deimos' hal_deimos.h — in directly.
set(DEIMOS_HAL_SHIM ${CMAKE_CURRENT_BINARY_DIR}/deimos_hal_shim.h)
file(WRITE ${DEIMOS_HAL_SHIM}
    "#include \"${STYX_DIR}/hal.h\"\n"
    "#include \"${DEIMOS_DIR}/hal_deimos.h\"\n")

add_library(deimos_rx STATIC ${DEIMOS_DIR}/deimos_rx.c)
target_include_directories(deimos_rx PRIVATE ${DEIMOS_DIR})
target_include_directories(deimos_rx INTERFACE ${DEIMOS_DIR})
target_compile_definitions(deimos_rx PRIVATE DEIMOS_HAL_H)
target_compile_options(deimos_rx PRIVATE -include ${DEIMOS_HAL_SHIM})
target_link_libraries(deimos_rx PUBLIC styx_hal)

# Build lib80211 (provides FFT + transitive fftw3f link)
add_subdirectory(${LIB80211_DIR} ${CMAKE_CURRENT_BINARY_DIR}/lib80211)

# Forward-only split-complex FFT for the waterfall producer.  Kept in shesha,
# not lib80211: the producer is its only consumer (see src/wf_fft.h).
add_library(wf_fft STATIC src/wf_fft.c)
target_include_directories(wf_fft PRIVATE ${FFTW3F_INCLUDE})
target_link_libraries(wf_fft PUBLIC lib80211)

# Build daemon
add_executable(shesha-daemon
    src/main.c
    src/waterfall.c
    src/adc_clip.c
    src/wf_claim.c
    src/wf_workers_policy.c
    src/protocol.c
    src/radio.c
    src/spsc_ring.c
    src/sendq.c
    src/window.c
    src/wifi_rx.c
    src/waterfall_mode.c
    src/wifi_mode.c
    src/wifi_fpga.c
    src/wifi_tx.c
    src/fm_mod.c
    src/rds_tx.c
    src/fm_tx.c
    src/fm_rx.c
    src/fm_mode.c
    src/fir.c
    src/rds_rx.c
    src/wifi_scan.c
    src/tx_scheduler.c
    src/thread_stats.c
    src/session.c
    src/tx_session.c
    src/producer.c
    src/mode_radio.c
    src/radio_txn.c
    src/iq_pack.c
    src/iq_mode.c
    src/iq_ring.c
    src/iq_tx.c
)
target_include_directories(shesha-daemon PRIVATE src)
target_link_libraries(shesha-daemon PRIVATE styx_hal deimos_rx wf_fft lib80211 m pthread)

# Firmware build fingerprint (git SHA, injected by the Makefile).  Reported
# in RESP_CAPS so the client can spot a stale deployed daemon.
if(NOT FIRMWARE_BUILD_ID)
    set(FIRMWARE_BUILD_ID "00000000")
endif()
target_compile_definitions(shesha-daemon PRIVATE
    SHESHA_FW_BUILD_ID=0x${FIRMWARE_BUILD_ID}u)

if(NOT APPLE)
    set_target_properties(shesha-daemon PROPERTIES LINK_FLAGS "-static")
endif()

# ---- On-device diagnostic tools ----
# Single-purpose binaries that exercise the hardware in isolation, in the
# spirit of styxsdr's firmware/tools/platform/.  No daemon, no TCP, no
# threads — so a failure indicts exactly one subsystem.
#
# Deliberately compiles src/fm_mod.c, the same modulator the daemon uses.
# `--verify` and `--bench` are only meaningful if they measure the code
# that actually transmits.

add_executable(fm_stream_test tools/fm_stream_test.c src/fm_mod.c src/rds_tx.c)
target_include_directories(fm_stream_test PRIVATE src)
target_link_libraries(fm_stream_test PRIVATE styx_hal m)

if(NOT APPLE)
    set_target_properties(fm_stream_test PROPERTIES LINK_FLAGS "-static")
endif()

# Observe the radio exactly as the daemon left it.  No radio_init(), so it
# can measure a held DAC word without a sample-clock change hiding it.
add_executable(rx_probe tools/rx_probe.c)
target_include_directories(rx_probe PRIVATE src)
target_link_libraries(rx_probe PRIVATE styx_hal m)

if(NOT APPLE)
    set_target_properties(rx_probe PROPERTIES LINK_FLAGS "-static")
endif()

# Per-stage waterfall compute benchmark.  Links the real uncached DDR convert
# (styx_hal), the real window, and the real lib80211/fftw3f FFT; shares
# wf_magnitude_db with the daemon so no stage is a copy.
add_executable(wf_bench tools/wf_bench.c src/window.c src/radio.c)
target_include_directories(wf_bench PRIVATE src)
target_link_libraries(wf_bench PRIVATE styx_hal lib80211 wf_fft m)

if(NOT APPLE)
    set_target_properties(wf_bench PROPERTIES LINK_FLAGS "-static")
endif()

# Two-core scaling harness: the same waterfall pipeline on N workers pinned one
# per CPU, each consuming a disjoint block of the live DMA stream.  Decides
# whether a second-core redesign of the producer is worth it.
add_executable(wf_scale tools/wf_scale.c src/window.c src/radio.c)
target_include_directories(wf_scale PRIVATE src)
target_link_libraries(wf_scale PRIVATE styx_hal lib80211 wf_fft m pthread)

if(NOT APPLE)
    set_target_properties(wf_scale PROPERTIES LINK_FLAGS "-static")
endif()

# Coverage/duty envelope: what fraction of the sample stream reaches an FFT,
# and what that costs, per (fs, N).  The benefit side of the duty cycle --
# measured from WR_PTR, so it needs no signal and no detector.
add_executable(wf_cover tools/wf_cover.c src/window.c src/radio.c)
target_include_directories(wf_cover PRIVATE src)
target_link_libraries(wf_cover PRIVATE styx_hal lib80211 wf_fft m)

if(NOT APPLE)
    set_target_properties(wf_cover PROPERTIES LINK_FLAGS "-static")
endif()

# Does a SECOND producer core buy anything, and where?  Same pipeline as
# wf_cover but on 1 vs 2 pinned workers claiming disjoint blocks from one
# atomic, so it reports coverage gain against duty cost per (fs, N).  Also the
# prototype for the shared-claim-plus-latency-cap the production design needs.
add_executable(wf_workers tools/wf_workers.c src/window.c src/radio.c)
target_include_directories(wf_workers PRIVATE src)
target_link_libraries(wf_workers PRIVATE styx_hal lib80211 wf_fft m pthread)

if(NOT APPLE)
    set_target_properties(wf_workers PROPERTIES LINK_FLAGS "-static")
endif()

# Burst-detection harness: TX a cyclic gated tone into an attenuated loopback
# and count how many bursts the waterfall catches.  The empirical check that
# coverage predicts what is actually seen.
add_executable(wf_burst tools/wf_burst.c src/window.c src/radio.c)
target_include_directories(wf_burst PRIVATE src)
target_link_libraries(wf_burst PRIVATE styx_hal lib80211 wf_fft m)

if(NOT APPLE)
    set_target_properties(wf_burst PROPERTIES LINK_FLAGS "-static")
endif()
