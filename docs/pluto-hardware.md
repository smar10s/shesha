# PlutoSDR Hardware Notes

Board-level facts that hold across modes. Everything here is measured on a
Pluto (Zynq 7010, two Cortex-A9 at 667 MHz) with the stock ADI kernel and a
styx-based bitstream.

Shesha requires CPU1 online. The stock boot environment does not enable it, so
the first section is also an installation prerequisite.

## The second Cortex-A9 core exists

`nproc` reports 1 because of a **boot argument, not silicon**. There is no
cpufreq driver, so the clock is fixed at boot and throttling cannot explain a
throughput change.

Confirmed from hardware registers, not part numbers:

- IDCODE at `0xF8000530` reads `0x13722093`, identifying a 7z010.
- The SCU configuration at `0xF8F00004` reads `0x00000511`; bits 1:0 are `01`,
  meaning two CPUs are present.

There is no single-core 7z010. The single-core Zynq-7000S parts are
7z007S/7z012S/7z014S.

The limit is deliberate ADI policy. In `u-boot-xlnx`, branch `pluto`,
`include/configs/zynq-common.h` contains the literal `"maxcpus=1\0"`, consumed
as `maxcpus=${maxcpus}` in all three boot paths. Commit `413748e99b`
(Michael Hennerich, ADI, 2017-03-15) added it, and `26e73f4436` hoisted it to a
variable so it could be overridden per board. The sibling config
`zynq_zc70x_sidekiqz2.h` in the same repository sets `"maxcpus=2\0"`, which is
the proof it is policy rather than a hardware limit. Nothing in `plutosdr-fw`
mentions `maxcpus`.

ADI's rationale is undocumented. The probable motive is the USB 2.0 2.5 W
power budget; treat that as a hypothesis, not a fact.

## Enabling CPU1

No rebuild and no reflash are needed: `CONFIG_HOTPLUG_CPU=y`,
`CONFIG_NR_CPUS=4`, and the kernel is SMP PREEMPT.

```sh
echo 1 > /sys/devices/system/cpu/cpu1/online   # runtime, -> nproc=2
fw_setenv maxcpus 2                            # persistent (QSPI)
```

Both are safe. The runtime write is reversible with `echo 0 > online`, and
`fw_setenv maxcpus 2` is common prior art across PlutoSDR projects. The only
established cost is power. Watch the USB budget, not stability.

Measured throughput on an FM-discriminator-shaped float workload, including
the `disc / a2_est` divide from `fm_rx.c`: **1.98x** (9.08 -> 17.99 Msamp/s).

The runtime write alone is lost on reboot. `maxcpus=1` remains in the boot
environment until `fw_setenv maxcpus 2` persists it.

## SMP safety

The ring buffers use `_Atomic` seq_cst, which emits a real `dmb` on ARM, and
`dma_tx.c` uses `__sync_synchronize()`. Nothing relies on single-core implicit
serialization.

## Validation on two cores

`echo 1 > /sys/devices/system/cpu/cpu1/online` brings CPU1 up cleanly
(`CPU1: thread -1, cpu 1, socket 0, mpidr 80000001`, no warnings), `nproc`
becomes 2, and `/proc/stat` starts emitting the `cpu1` line. That line is what
makes the `shesha debug cpu` system figure trustworthy.

| | nproc=1 | nproc=2 |
|---|---|---|
| `fm-check` 20 s output rate | 99.999% | 99.999% |
| `fm-check` depth min-max | 292.4-298.9 ms | **297.7**-298.9 ms |
| `fm-check` headroom (nco, fixed) | 1.73x | 1.76x |
| `fm-gate` 30 s output rate | 99.999% | 99.999% |
| `fm-gate` underruns / starved | 0 / 0 | 0 / 0 |
| `fm-gate` status polls | 295 ok, 0 failed | 295 ok, 0 failed |

The gates are already at their ceiling on one core, so the interesting number
is not the rate: **the feeder's worst-case margin improves**, with minimum
depth rising from 292.4 ms to 297.7 ms. The transient TX deficit on one core
is gone.

The mechanism is visible directly: involuntary context switches on the
waterfall producer drop from **49.3/s to 2.5/s** at 20 MSPS. That is the
crackle risk from `fm-tx.md`, in the counters.

## Waterfall CPU scales with sample rate

Always quote the rate with the number. At the default FFT size (N=4096) and
one worker, the waterfall producer costs **58.4%** of a core at 2.1 MSPS and
**90.9%** at 20 MSPS, which is the daemon's default waterfall rate. With CPU1
online it is **58.9%** at 2.1 MSPS; the producer still uses one worker,
because the policy clamps to one at or below `fs_max`. These are daemon
`shesha debug cpu` figures. `psd-waterfall.md` reports isolated `make bench-*`
figures at specific N and worker counts; they are different operating points,
not a contradiction.

## DDR, caching, and the convert path

### Do not drop `O_SYNC`

`hal.c` opens `/dev/mem` with `O_SYNC`. Dropping it to speed up DDR access is
incorrect for TX: ARM writes land in cache, the FPGA reads DRAM, and there is
no userspace flush primitive for a `/dev/mem` mapping.

For RX it would be correct only by luck. The ring is 128 MB against 512 KB of
L2, and `fm_mode`'s read pointer advances monotonically, but
`dma_rx_capture` re-latches per call and can re-read a still-cached region.
Intermittently stale IQ would not show up in a gate. Whether it is broken at
all turns on AXI HP versus ACP, and that is not established anywhere in this
repository.

Staging through `memcpy` gets the wide accesses with no memory-model change.

Auto-vectorization is not an alternative. At `-O3` with `volatile` removed,
GCC 11 emits `ldrd` (two words per access) and leaves the arithmetic scalar,
because the 12-bit sign-extending unpack de-interleaves into two arrays.

DDR *is* mapped uncached and `volatile` does forbid the compiler widening the
loads. Both are real; together they are worth 1.6 points of the RX demod, not
19. See `fm-rx.md` for the measured split against the demodulator.

## Measuring CPU

Use `shesha debug cpu`, not hand arithmetic. `CMD_THREAD_STATS` reports
per-thread cumulative jiffies plus voluntary and involuntary context switches
from `/proc/self/task`; the client differences two samples. Polling it during
a 30 s `fm-gate` run did not perturb the result (99.9995%, 0 underruns), so it
is safe under load.

Two traps produce plausible wrong numbers rather than errors:

- `/proc/stat`'s bare `cpu` aggregate sums `for_each_possible_cpu()`, which
  includes CPU1 while it is offline. Its idle accounting there is not
  predictable: measured advancing at 200 jiffies/s shortly after boot against
  a true 100 Hz, which made "system busy" read 47% while the daemon's own
  threads summed to 92%. Sum only per-core `cpuN` lines. The kernel emits
  those solely for online CPUs, so the figure stays correct once CPU1 is up.
- `CONFIG_SCHEDSTATS` is compiled out, so there is no `/proc/*/schedstat` and
  no runqueue-wait figure. Jiffies at 100 Hz give 0.2% resolution over a 5 s
  window.

The instrument also separates yielding from preemption, which raw CPU% cannot:
the waterfall producer shows ~3900 voluntary switches/s (the `usleep(50)`
inside `dma_rx_capture`) against only ~7 involuntary. FM RX shows ~1030
voluntary against ~23 involuntary.

## Where the cores go

At 2.1 MSPS, measured with `shesha debug cpu`, one core:

| thread | standalone | during exclusive FM TX |
|---|---|---|
| FM TX (`tx-stream`) | 61.0% | 61.0% |
| waterfall producer | **58.4%** | -- (suspended) |
| FM RX producer | **41.9%** (27.3% with a boxcar decimator) | -- (suspended) |
| poll loop / TCP | 0.4-1.5% | 0.4-0.8% |

The FM TX figure is the daemon's `tx-stream` thread. `fm-tx.md` reports 86.1%
for the standalone `--bench` harness, which measures different work.

The waterfall figure is its **demand**, not what it gets while starved. A
lower number sampled during concurrent TX is the share it was allowed, not its
cost.

Concurrent TX + waterfall demands **~119% of one core**, and TX + FM RX
**~103%**. Against two cores the budget is 200%, so both fit; against one,
neither does.
