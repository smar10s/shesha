PLUTO_IP ?= 192.168.2.1
PLUTO_USER ?= root
PLUTO_PASS ?= analog
DOCKER_IMAGE ?= styx-firmware

# Firmware build fingerprint: first 8 hex of the git SHA, baked into the
# daemon and reported in RESP_CAPS.  All zeros outside a git checkout.
FW_BUILD_ID ?= $(shell git -C $(CURDIR) rev-parse HEAD 2>/dev/null | cut -c1-8)
FW_BUILD_ID := $(if $(FW_BUILD_ID),$(FW_BUILD_ID),00000000)

# Drop unconnected hardware paths when a mode module is linked into a host
# test.  GNU ld uses --gc-sections; Apple ld uses dead_strip.
ifeq ($(shell uname -s),Darwin)
DRAIN_GC_FLAGS := -Wl,-dead_strip
else
DRAIN_GC_FLAGS := -Wl,--gc-sections
endif

.PHONY: firmware tools examples deploy deploy-tools tool-run fm-check fm-gate run stop log clean test-firmware test-wf-target test-wire-target bench-fir bench-waterfall bench-scale bench-cover bench-workers rds-offline rds-mpx

firmware:
	docker run --rm -v $(CURDIR):/src:ro -v shesha-build:/build $(DOCKER_IMAGE) \
		cmake -B /build/firmware -S /src/firmware \
		-DCMAKE_TOOLCHAIN_FILE=/src/firmware/cmake/toolchain-pluto.cmake \
		-DFIRMWARE_BUILD_ID=$(FW_BUILD_ID) \
		-DCMAKE_BUILD_TYPE=Release
	docker run --rm -v $(CURDIR):/src:ro -v shesha-build:/build $(DOCKER_IMAGE) \
		cmake --build /build/firmware --parallel
	@mkdir -p build
	docker run --rm -v $(CURDIR)/build:/out -v shesha-build:/build:ro \
		$(DOCKER_IMAGE) sh -c 'cp /build/firmware/shesha-daemon /out/shesha-daemon && chmod 755 /out/shesha-daemon'

# On-device diagnostic tools (firmware/tools/).  Single-purpose binaries
# that exercise the hardware in isolation — no daemon, no TCP, no threads.
TOOLS ?= fm_stream_test

tools:
	docker run --rm -v $(CURDIR):/src:ro -v shesha-build:/build $(DOCKER_IMAGE) \
		cmake -B /build/firmware -S /src/firmware \
		-DCMAKE_TOOLCHAIN_FILE=/src/firmware/cmake/toolchain-pluto.cmake \
		-DCMAKE_BUILD_TYPE=Release
	docker run --rm -v $(CURDIR):/src:ro -v shesha-build:/build $(DOCKER_IMAGE) \
		cmake --build /build/firmware --parallel $(addprefix --target ,$(TOOLS))
	@mkdir -p build
	docker run --rm -v $(CURDIR)/build:/out -v shesha-build:/build:ro \
		$(DOCKER_IMAGE) sh -c 'for t in $(TOOLS); do \
			cp /build/firmware/$$t /out/$$t && chmod 755 /out/$$t; done'

deploy-tools: tools
	sshpass -p $(PLUTO_PASS) scp -O -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		$(addprefix build/,$(TOOLS)) $(PLUTO_USER)@$(PLUTO_IP):/usr/bin/

# Run an on-device tool.  ARGS passes flags through:
#   make tool-run TOOL=fm_stream_test ARGS="-d 60 -g nco -w 300"
TOOL ?= fm_stream_test
tool-run:
	@sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		$(PLUTO_USER)@$(PLUTO_IP) '$(TOOL) $(ARGS)'

# FM TX gate.  Stops the daemon first so the measurement is not competing
# with the poll loop and PSD broadcast for the single core.
fm-check: deploy-tools
	@sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null $(PLUTO_USER)@$(PLUTO_IP) \
		'killall shesha-daemon 2>/dev/null; sleep 1; \
		 echo "== generator accuracy =="; \
		 fm_stream_test --verify -g nco || exit 1; \
		 echo "== CPU headroom =="; \
		 fm_stream_test --bench -d 5 -g nco --fixed-scale || exit 1; \
		 echo "== sustained stream, $(FM_CHECK_SEC)s, $(FM_CHECK_MS)ms bound =="; \
		 fm_stream_test -d $(FM_CHECK_SEC) -g nco -w $(FM_CHECK_MS) -a 20 || exit 1'

FM_CHECK_SEC ?= 60
FM_CHECK_MS  ?= 300

# FM TX gate at the daemon level.  fm-check proves the ARM can modulate with
# nothing else running; this proves it still holds inside the daemon, with
# the poll loop, command dispatch and the TCP audio path competing for the
# single core.  Asserts against CMD_TX_STATS, so no human has to listen to
# a radio to know whether it worked.
FM_GATE_SEC ?= 30

fm-gate: deploy run
	@sleep 2
	python3 scripts/fm_tx_gate.py --host $(PLUTO_IP) -d $(FM_GATE_SEC) $(GATE_ARGS)

# The old daemon maps its own text.  Overwriting it while the process still
# exists returns ETXTBSY, so wait for it to exit before copying.
deploy: firmware
	sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		$(PLUTO_USER)@$(PLUTO_IP) \
		'killall shesha-daemon 2>/dev/null || true; \
		 i=0; \
		 while pidof shesha-daemon >/dev/null 2>&1; do \
		     if [ $$i -ge 200 ]; then \
		         echo "shesha-daemon did not exit" >&2; exit 1; \
		     fi; \
		     if [ $$i -ge 100 ]; then \
		         killall -9 shesha-daemon 2>/dev/null || true; \
		         sleep 0.2; \
		     fi; \
		     sleep 0.1; i=$$((i+1)); \
		 done'
	sshpass -p $(PLUTO_PASS) scp -O -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		build/shesha-daemon $(PLUTO_USER)@$(PLUTO_IP):/usr/bin/

run:
	sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		$(PLUTO_USER)@$(PLUTO_IP) \
		'killall shesha-daemon 2>/dev/null; \
		 i=0; \
		 while pidof shesha-daemon >/dev/null 2>&1 && [ $$i -lt 100 ]; do \
		     sleep 0.1; i=$$((i+1)); \
		 done; \
		 if pidof shesha-daemon >/dev/null 2>&1; then \
		     killall -9 shesha-daemon 2>/dev/null; sleep 0.2; \
		 fi; \
		 nohup shesha-daemon $(or $(DAEMON_PORT),5555) > /tmp/shesha.log 2>&1 &'

stop:
	sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		$(PLUTO_USER)@$(PLUTO_IP) 'killall shesha-daemon 2>/dev/null || true'

log:
	sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		$(PLUTO_USER)@$(PLUTO_IP) 'cat /tmp/shesha.log'

# Host unit tests for firmware code that is pure computation — no hardware,
# no cross-compile, no Docker.  Currently the /proc line parsers, whose
# failure mode is a plausible wrong number rather than an error, and whose
# nastiest input (a comm containing spaces or parens) the device never
# produces.
test-firmware:
	@mkdir -p build
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_thread_stats_parse \
		firmware/tests/test_thread_stats_parse.c firmware/src/thread_stats.c
	@./build/test_thread_stats_parse
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_fm_rx \
		firmware/tests/test_fm_rx.c firmware/src/fm_rx.c \
		firmware/src/fir.c firmware/src/rds_rx.c -lm
	@./build/test_fm_rx
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_rds_rx \
		firmware/tests/test_rds_rx.c firmware/src/rds_rx.c firmware/src/fir.c -lm
	@./build/test_rds_rx
	@cc -std=c11 -Wall -Wextra -Werror \
		-fsanitize=undefined -fno-sanitize-recover=undefined \
		-o build/test_rds_rx_ubsan \
		firmware/tests/test_rds_rx.c firmware/src/rds_rx.c firmware/src/fir.c -lm
	@./build/test_rds_rx_ubsan
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_rds_tx \
		firmware/tests/test_rds_tx.c firmware/src/rds_tx.c \
		firmware/src/rds_rx.c firmware/src/fir.c -lm
	@./build/test_rds_tx
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_fm_mod \
		firmware/tests/test_fm_mod.c firmware/src/fm_mod.c \
		firmware/src/rds_tx.c -lm
	@./build/test_fm_mod
	@cc -std=c11 -Wall -Wextra -Werror \
		-fsanitize=undefined -fno-sanitize-recover=undefined \
		-o build/test_fm_mod_ubsan firmware/tests/test_fm_mod.c \
		firmware/src/fm_mod.c firmware/src/rds_tx.c -lm
	@./build/test_fm_mod_ubsan
	@cc -std=c11 -Wall -Wextra -Werror \
		-I firmware/src -I firmware/platforms/styx/firmware/src \
		-o build/test_fm_tx firmware/tests/test_fm_tx.c \
		firmware/src/fm_mod.c firmware/src/rds_tx.c -lm
	@./build/test_fm_tx
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-o build/test_fm_rx_fir \
		firmware/tests/test_fm_rx_fir.c
	@./build/test_fm_rx_fir
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_fir firmware/tests/test_fir.c firmware/src/fir.c
	@./build/test_fir
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/tests \
		-o build/test_channel_filter \
		firmware/tests/test_channel_filter.c firmware/tests/channel_filter.c -lm
	@./build/test_channel_filter
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_mode_radio \
		firmware/tests/test_mode_radio.c firmware/src/mode_radio.c
	@./build/test_mode_radio
	@cc -std=c11 -Wall -Wextra -Werror \
		-I firmware/src -I firmware/platforms/styx/firmware/src \
		-I firmware/platforms/deimos/firmware/src \
		-ffunction-sections -fdata-sections $(DRAIN_GC_FLAGS) \
		-o build/test_wifi_fpga_chan \
		firmware/tests/test_wifi_fpga_chan.c firmware/src/wifi_fpga.c
	@./build/test_wifi_fpga_chan
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_session \
		firmware/tests/test_session.c firmware/src/session.c
	@./build/test_session
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-o build/test_tx_session \
		firmware/tests/test_tx_session.c firmware/src/tx_session.c
	@./build/test_tx_session
	@cc -std=c11 -Wall -Wextra -Werror \
		-I firmware/src \
		-o build/test_producer \
		firmware/tests/test_producer.c firmware/src/producer.c firmware/src/mode_radio.c
	@./build/test_producer
	@cc -std=c11 -Wall -Wextra -Werror -D_DEFAULT_SOURCE \
		-I firmware/src -I firmware/platforms/styx/firmware/src \
		-ffunction-sections -fdata-sections $(DRAIN_GC_FLAGS) \
		-o build/test_drain_metadata \
		firmware/tests/test_drain_metadata.c \
		firmware/src/iq_mode.c firmware/src/waterfall_mode.c \
		firmware/src/spsc_ring.c firmware/src/protocol.c -lm
	@./build/test_drain_metadata
	@cc -std=c11 -Wall -Wextra -Werror \
		-I firmware/src \
		-o build/test_radio_txn \
		firmware/tests/test_radio_txn.c firmware/src/radio_txn.c firmware/src/mode_radio.c
	@./build/test_radio_txn
	@cc -std=c11 -Wall -Wextra -Werror \
		-I firmware/src -I firmware/platforms/styx/firmware/src \
		-o build/test_radio \
		firmware/tests/test_radio.c firmware/src/radio.c
	@./build/test_radio
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_spsc_ring \
		firmware/tests/test_spsc_ring.c firmware/src/spsc_ring.c
	@./build/test_spsc_ring
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_sendq \
		firmware/tests/test_sendq.c firmware/src/sendq.c
	@./build/test_sendq
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_wf_claim \
		firmware/tests/test_wf_claim.c firmware/src/wf_claim.c -lpthread
	@./build/test_wf_claim
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_wf_workers_policy \
		firmware/tests/test_wf_workers_policy.c firmware/src/wf_workers_policy.c
	@./build/test_wf_workers_policy
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_iq_pack \
		firmware/tests/test_iq_pack.c firmware/src/iq_pack.c
	@./build/test_iq_pack
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-o build/test_adc_clip \
		firmware/tests/test_adc_clip.c firmware/src/adc_clip.c
	@./build/test_adc_clip
	@cc -std=c11 -Wall -Wextra -Werror \
		-I firmware/src -I firmware/platforms/styx/firmware/src \
		-o build/test_wf_convert \
		firmware/tests/test_wf_convert.c -lm
	@./build/test_wf_convert
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-o build/test_protocol_iq \
		firmware/tests/test_protocol_iq.c firmware/src/protocol.c
	@./build/test_protocol_iq
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-o build/test_protocol_frame \
		firmware/tests/test_protocol_frame.c firmware/src/protocol.c
	@./build/test_protocol_frame
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-o build/test_protocol_fm \
		firmware/tests/test_protocol_fm.c firmware/src/protocol.c
	@./build/test_protocol_fm
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-o build/test_protocol_wifi \
		firmware/tests/test_protocol_wifi.c firmware/src/protocol.c
	@./build/test_protocol_wifi
	@cc -std=c11 -Wall -Wextra -Werror \
		-o build/test_iq_ring \
		firmware/tests/test_iq_ring.c firmware/src/iq_ring.c
	@./build/test_iq_ring
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-o build/test_wifi_classify \
		firmware/tests/test_wifi_classify.c firmware/src/wifi_scan.c
	@./build/test_wifi_classify
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-fsanitize=address,undefined -fno-sanitize-recover=undefined \
		-o build/test_wifi_classify_san \
		firmware/tests/test_wifi_classify.c firmware/src/wifi_scan.c
	@./build/test_wifi_classify_san
	@cc -std=c11 -Wall -Wextra -Werror -D_DEFAULT_SOURCE \
		-I firmware/src -I firmware/platforms/styx/firmware/src \
		-o build/test_waterfall \
		firmware/tests/test_waterfall.c firmware/src/waterfall.c \
		firmware/src/wf_claim.c firmware/src/window.c -lm
	@./build/test_waterfall
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-o build/test_wf_mag \
		firmware/tests/test_wf_mag.c -lm
	@./build/test_wf_mag
	@cc -std=c11 -Wall -Wextra -Werror -I firmware/src \
		-o build/test_wf_fastlog \
		firmware/tests/test_wf_fastlog.c -lm
	@./build/test_wf_fastlog
	@cc -std=c11 -Wall -Wextra -Werror -D_DEFAULT_SOURCE \
		-I firmware/src -I firmware/platforms/styx/firmware/src \
		-o build/test_tx_scheduler \
		firmware/tests/test_tx_scheduler.c firmware/src/tx_scheduler.c \
		firmware/tests/fake_dma_tx.c -lpthread
	@./build/test_tx_scheduler

# Host tool: decode an interleaved float32 IQ capture through the exact
# firmware FM demod + RDS chain.  Used to separate a hardware failure from a
# signal failure, and to score an on-air IQ capture offline.  See the tool's
# header for the capture recipe; scripts/rds_quality.py drives it.
rds-offline:
	@mkdir -p build
	@cc -std=c11 -O2 -I firmware/src -I firmware/tests \
		-o build/rds_offline \
		firmware/tests/rds_offline.c firmware/tests/channel_filter.c \
		firmware/src/fm_rx.c firmware/src/fir.c firmware/src/rds_rx.c -lm
	@echo "built build/rds_offline"

# Host tool: synthesize an FM multiplex carrying real RDS groups as packed
# 12-bit IQ for a TX -> attenuator -> RX loopback (see the tool's header).
rds-mpx:
	@mkdir -p build
	@cc -std=c11 -O2 -o build/rds_mpx_gen firmware/tests/rds_mpx_gen.c -lm
	@echo "built build/rds_mpx_gen"

# Microbenchmark for the decimator FIR inner loop, on the target.
#
# Built with exactly the daemon's flags, statically linked because the Docker
# toolchain's glibc is newer than the Pluto's. Stops the daemon first: the
# whole point is to measure the loop, not to measure it competing with the
# thing it is part of.
#
# The deployed four-accumulator noinline shape in fir.c was chosen from an
# older -O2 run of this benchmark. Under the production flags symmetric
# folding ranks best in isolation, so re-measure in place before changing
# fir.c.
bench-fir:
	@mkdir -p build
	docker run --rm -v $(CURDIR):/src:ro -v $(CURDIR)/build:/out $(DOCKER_IMAGE) \
		arm-linux-gnueabihf-gcc -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard \
		-O3 -funsafe-math-optimizations -static -o /out/bench_fir /src/firmware/tests/bench_fir.c
	sshpass -p $(PLUTO_PASS) scp -O -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		build/bench_fir $(PLUTO_USER)@$(PLUTO_IP):/tmp/
	@sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null $(PLUTO_USER)@$(PLUTO_IP) \
		'chmod +x /tmp/bench_fir; killall shesha-daemon 2>/dev/null; \
		 sleep 1; /tmp/bench_fir'

# Magnitude/fast-log accuracy tests, on the target.
#
# By default these run on the host (make test-firmware, plain cc); but the
# vectoriser changes the code they are meant to verify, so the exhaustive
# sweep has to go through the same ARM codegen the daemon ships
# (-mcpu=cortex-a9 -mfpu=neon -O3 -funsafe-math-optimizations).  Statically
# linked because the Docker toolchain's glibc is newer than the Pluto's.
test-wf-target:
	@mkdir -p build
	docker run --rm -v $(CURDIR):/src:ro -v $(CURDIR)/build:/out $(DOCKER_IMAGE) \
		arm-linux-gnueabihf-gcc -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard \
		-O3 -funsafe-math-optimizations -static -I /src/firmware/src \
		-o /out/test_wf_fastlog_arm /src/firmware/tests/test_wf_fastlog.c -lm
	docker run --rm -v $(CURDIR):/src:ro -v $(CURDIR)/build:/out $(DOCKER_IMAGE) \
		arm-linux-gnueabihf-gcc -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard \
		-O3 -funsafe-math-optimizations -static -I /src/firmware/src \
		-o /out/test_wf_mag_arm /src/firmware/tests/test_wf_mag.c -lm
	sshpass -p $(PLUTO_PASS) scp -O -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		build/test_wf_fastlog_arm build/test_wf_mag_arm $(PLUTO_USER)@$(PLUTO_IP):/tmp/
	@sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null $(PLUTO_USER)@$(PLUTO_IP) \
		'chmod +x /tmp/test_wf_fastlog_arm /tmp/test_wf_mag_arm; \
		 echo "== fast log, ARM codegen =="; /tmp/test_wf_fastlog_arm || exit 1; \
		 echo "== magnitude, ARM codegen =="; /tmp/test_wf_mag_arm || exit 1'

# Inbound frame-size bound, on the target.
#
# proto_frame_size() rejects oversized lengths before the header-plus-payload
# addition can wrap.  That wrap only happens when size_t is 32 bits, so the
# host test proves nothing about the code the daemon ships.  Built with the
# daemon's ARM flags and statically linked, like test-wf-target.
test-wire-target:
	@mkdir -p build
	docker run --rm -v $(CURDIR):/src:ro -v $(CURDIR)/build:/out $(DOCKER_IMAGE) \
		arm-linux-gnueabihf-gcc -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard \
		-O3 -funsafe-math-optimizations -Wall -Wextra -Werror -static \
		-I /src/firmware/src \
		-o /out/test_protocol_frame_arm \
		/src/firmware/tests/test_protocol_frame.c /src/firmware/src/protocol.c
	sshpass -p $(PLUTO_PASS) scp -O -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		build/test_protocol_frame_arm $(PLUTO_USER)@$(PLUTO_IP):/tmp/
	@sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null $(PLUTO_USER)@$(PLUTO_IP) \
		'chmod +x /tmp/test_protocol_frame_arm; \
		 echo "== frame size bound, ARM codegen =="; \
		 /tmp/test_protocol_frame_arm'

# Per-stage waterfall compute benchmark, on the target.  Built through the
# firmware CMake project because it links lib80211/fftw3f (bench-fir is a bare
# single-file link).  Stops the daemon first: it owns the AD9361 and the RX
# DMA, and the point is to measure the compute, not the contention.
#   make bench-waterfall ARGS="1000"
bench-waterfall:
	@$(MAKE) --no-print-directory tools TOOLS=wf_bench
	sshpass -p $(PLUTO_PASS) scp -O -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		build/wf_bench $(PLUTO_USER)@$(PLUTO_IP):/tmp/
	@sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null $(PLUTO_USER)@$(PLUTO_IP) \
		'chmod +x /tmp/wf_bench; killall shesha-daemon 2>/dev/null; \
		 sleep 1; /tmp/wf_bench $(ARGS)'

# Two-core scaling harness, on the target.  Runs the waterfall pipeline on one
# and two pinned cores over the live DMA stream and reports aggregate Msps, so
# the "does a second core pay?" question is answered by measurement:
#   make bench-scale ARGS="2"
bench-scale:
	@$(MAKE) --no-print-directory tools TOOLS=wf_scale
	sshpass -p $(PLUTO_PASS) scp -O -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		build/wf_scale $(PLUTO_USER)@$(PLUTO_IP):/tmp/
	@sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null $(PLUTO_USER)@$(PLUTO_IP) \
		'chmod +x /tmp/wf_scale; killall shesha-daemon 2>/dev/null; \
		 sleep 1; /tmp/wf_scale $(ARGS)'

# Coverage/duty envelope, on the target.  Sweeps (fs, N) free-running vs
# paced to the demand rate and reports what fraction of the sample stream
# reached an FFT:
#   make bench-cover ARGS="2"
bench-cover:
	@$(MAKE) --no-print-directory tools TOOLS=wf_cover
	sshpass -p $(PLUTO_PASS) scp -O -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		build/wf_cover $(PLUTO_USER)@$(PLUTO_IP):/tmp/
	@sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null $(PLUTO_USER)@$(PLUTO_IP) \
		'chmod +x /tmp/wf_cover; killall shesha-daemon 2>/dev/null; \
		 sleep 1; /tmp/wf_cover $(ARGS)'

# Does a second producer core pay, and at which rates?  Reports coverage gain
# against duty cost for 1 vs 2 workers per (fs, N), so the answer is a measured
# trade rather than a peak-throughput number:
#   make bench-workers ARGS="3"
bench-workers:
	@$(MAKE) --no-print-directory tools TOOLS=wf_workers
	sshpass -p $(PLUTO_PASS) scp -O -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		build/wf_workers $(PLUTO_USER)@$(PLUTO_IP):/tmp/
	@sshpass -p $(PLUTO_PASS) ssh -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null $(PLUTO_USER)@$(PLUTO_IP) \
		'chmod +x /tmp/wf_workers; killall shesha-daemon 2>/dev/null; \
		 sleep 1; /tmp/wf_workers $(ARGS)'

# Host examples. They are not part of the daemon or the Python package;
# this target is the drift guard that keeps the C one compiling.
CC ?= cc
EXAMPLE_CFLAGS ?= -std=c17 -Wall -Wextra
EXAMPLE_CLIENT_SRC := client/c/src/client.c client/c/src/iq.c

examples: build/examples/iq_rx

build/examples/iq_rx: examples/iq_rx.c $(EXAMPLE_CLIENT_SRC) client/c/include/shesha/client.h
	@mkdir -p build/examples
	$(CC) $(EXAMPLE_CFLAGS) -Iclient/c/include -Iclient/c/src \
		examples/iq_rx.c $(EXAMPLE_CLIENT_SRC) -lm -o $@

clean:
	rm -rf build/
