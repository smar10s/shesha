#pragma once

/*
 * Offline channel filter for IQ-mode captures.
 *
 * The live FM path gets a +/-200 kHz low-pass for free from the AD9361 RX
 * FIR.  IQ mode leaves that FIR disabled, so a raw capture carries the
 * adjacent channel and the offline decoder cannot lock.  A capture that has
 * been through this filter is equivalent input to the live chain, which is
 * what lets rds_offline separate a hardware failure from a signal failure.
 *
 * Host-only: the firmware runs the chip's FIR, not this.  Lives in
 * firmware/tests/ so it is not pulled into the daemon build.
 */

/*
 * Design an odd-length, symmetric, linear-phase low-pass as a Blackman-windowed
 * sinc with cutoff `cutoff_hz` at `fs_hz`, normalised to unity DC gain.
 * Returns 0 on success, -1 on bad geometry (non-odd n_taps, non-positive fs,
 * cutoff at or above Nyquist).
 */
int channel_filter_design(float *taps, int n_taps, double cutoff_hz, double fs_hz);

/*
 * Magnitude response at `f_hz`, linear (1.0 at DC for a unity-gain design).
 * Evaluated directly from the taps; used by the test to pin passband and
 * stopband without a separate FFT.
 */
double channel_filter_response(const float *taps, int n_taps,
                               double f_hz, double fs_hz);
