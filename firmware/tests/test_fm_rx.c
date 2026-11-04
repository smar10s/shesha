/*
 * Characterisation tests for the FM discriminator and decimation chain
 * (firmware/src/fm_rx.c).
 *
 * Three jobs:
 *
 *   1. Assert real behaviour — that a known tone at known deviation comes
 *      back at the right frequency and amplitude. This is what makes the
 *      test meaningful rather than merely change-detecting.
 *   2. Assert the *stopband*, which no earlier version of this file did. That
 *      omission is why a decimator with 18 dB more aliased than direct noise
 *      shipped and passed: every test tone was inside the passband, where the
 *      defect is invisible. A tone from our own transmitter cannot excite it
 *      either, so the tests below synthesise a full FM stereo multiplex —
 *      pilot, stereo subcarrier, RDS — and measure what reaches the audio.
 *   3. Pin the exact output of a fixed input, so a refactor that is supposed
 *      to be numerically identical can be shown to be identical rather than
 *      assumed to be.
 *
 * The filter design lives in scripts/fm_decim_design.py, which asserts its own
 * invariants (--selftest) and generates firmware/src/fm_decim_taps.h. The
 * numbers this file checks against are that script's predictions; the point of
 * having both is that the script proves the filters are *specified* right and
 * this file proves they are *wired* right.
 *
 * Build and run:  make test-firmware
 */
#include "../src/fm_rx.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures;

#define CHECK(cond) do {                                                  \
    if (!(cond)) {                                                        \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
        failures++;                                                       \
    }                                                                     \
} while (0)

#define CHECK_NEAR(got, want, tol) do {                                   \
    double _g = (got), _w = (want), _t = (tol);                           \
    if (!(fabs(_g - _w) <= _t)) {                                         \
        printf("  FAIL %s:%d: %s = %.9g, want %.9g +/- %.3g"              \
               " (off by %.3g)\n",                                        \
               __FILE__, __LINE__, #got, _g, _w, _t, fabs(_g - _w));      \
        failures++;                                                       \
    }                                                                     \
} while (0)

/* Assert `got` is at least `want` dB below `ref`, and say by how much. */
#define CHECK_BELOW_DB(got, ref, want_db) do {                            \
    double _g = (got), _r = (ref), _w = (want_db);                        \
    double _db = 20.0 * log10((_g + 1e-30) / (_r + 1e-30));               \
    if (!(_db <= -_w)) {                                                  \
        printf("  FAIL %s:%d: %s is %.1f dB below %s, want %.1f dB\n",    \
               __FILE__, __LINE__, #got, -_db, #ref, _w);                 \
        failures++;                                                       \
    }                                                                     \
} while (0)

#define RUN(fn) do { printf("%s\n", #fn); fn(); } while (0)

#define IQ_RATE     2100000u
#define DEVIATION     75000u
#define DEEMPHASIS       75u

/* What the chain delivers. Derived, not chosen: 2100000 / 48. */
#define OUT_RATE    (double)FM_AUDIO_RATE

/*
 * Synthesise a constant-envelope FM carrier modulated by a sum of sines.
 *
 * Phase is integrated sample by sample rather than evaluated in closed form,
 * so the input is exactly what a modulator would produce and the test does
 * not depend on a trig identity being right.
 */
typedef struct { double freq; double amp; } tone_t;

/* Standard RDS injection, as a fraction of peak deviation. */
#define RDS_INJECT 0.03

/*
 * rds_wave, when non-NULL, is the bipolar biphase sign at IQ_RATE; it is
 * DSB-SC modulated onto 57 kHz and added to the audio sum, which is what a
 * real multiplex looks like. Passing NULL reproduces the tone-only synth
 * the other tests use.
 */
static void synth_fm_full(float *iq_real, float *iq_imag, size_t n,
                          const tone_t *tones, size_t n_tones,
                          const float *rds_wave)
{
    double phase = 0.0;
    double k = 2.0 * M_PI * (double)DEVIATION / (double)IQ_RATE;

    for (size_t i = 0; i < n; i++) {
        double audio = 0.0;
        for (size_t t = 0; t < n_tones; t++)
            audio += tones[t].amp * sin(2.0 * M_PI * tones[t].freq
                                        * (double)i / (double)IQ_RATE);
        if (rds_wave)
            audio += RDS_INJECT * (double)rds_wave[i]
                     * cos(2.0 * M_PI * 57000.0 * (double)i / (double)IQ_RATE);
        iq_real[i] = (float)cos(phase);
        iq_imag[i] = (float)sin(phase);
        phase += k * audio;
        if (phase > M_PI)  phase -= 2.0 * M_PI;
        if (phase < -M_PI) phase += 2.0 * M_PI;
    }
}

static void synth_fm_multi(float *iq_real, float *iq_imag, size_t n,
                           const tone_t *tones, size_t n_tones)
{
    synth_fm_full(iq_real, iq_imag, n, tones, n_tones, NULL);
}

static void synth_fm(float *iq_real, float *iq_imag, size_t n,
                     double tone_hz, double amplitude)
{
    tone_t t = { tone_hz, amplitude };
    synth_fm_multi(iq_real, iq_imag, n, &t, 1);
}

/* Zero crossings -> frequency, ignoring the first tenth as settling. */
static double measure_freq(const float *x, size_t n, double rate)
{
    size_t skip = n / 10;
    int crossings = 0;
    for (size_t i = skip + 1; i < n; i++)
        if ((x[i - 1] < 0.0f && x[i] >= 0.0f) || (x[i - 1] >= 0.0f && x[i] < 0.0f))
            crossings++;
    double span = (double)(n - skip - 1) / rate;
    return crossings / (2.0 * span);
}

static double measure_peak(const float *x, size_t n)
{
    /*
     * Skip a third, not a tenth. The envelope estimator that normalises the
     * discriminator moves at alpha = 1/2048 from a2_est = 1e-6, so until it
     * converges the reciprocal is enormous and the audio is clamped at
     * +/-1.0. That is 7 * 2048 = 14336 IQ samples to settle to 0.1%, which is
     * 299 audio samples of a 1024-sample frame. A tenth (102) leaves the tail
     * of that transient in range, and a peak measurement finds it: it read
     * 0.511 for a half-deviation tone whose true peak is 0.440.
     */
    size_t skip = n / 3;
    double peak = 0.0;
    for (size_t i = skip; i < n; i++)
        if (fabs(x[i]) > peak)
            peak = fabs(x[i]);
    return peak;
}

/*
 * Amplitude at one frequency, by Hann-windowed Goertzel.
 *
 * The window is not optional. With a rectangular window the 1 kHz mono tone
 * leaks about -42 dB into a bin 1750 Hz away, which is louder than the
 * aliases being measured — the test would be measuring its own leakage and
 * would pass for any filter. Hann's sidelobes fall 18 dB/octave and put that
 * leakage below -100 dB.
 *
 * Normalised by the window sum, so a unit-amplitude sinusoid reads 1.0 for
 * any window and the ratios the tests assert on are window-independent.
 */
static double tone_amp(const float *x, size_t n, double freq, double fs)
{
    double k = 2.0 * cos(2.0 * M_PI * freq / fs);
    double s1 = 0.0, s2 = 0.0, wsum = 0.0;
    for (size_t i = 0; i < n; i++) {
        double w = 0.5 - 0.5 * cos(2.0 * M_PI * (double)i / (double)(n - 1));
        double s0 = w * (double)x[i] + k * s1 - s2;
        s2 = s1;
        s1 = s0;
        wsum += w;
    }
    double p = s1 * s1 + s2 * s2 - k * s1 * s2;
    return 2.0 * sqrt(p > 0.0 ? p : 0.0) / wsum;
}

/*
 * De-emphasis magnitude response, for predicting amplitudes.
 *
 * This is the response of the one-pole IIR fm_rx.c actually runs, computed as
 * a discrete filter, NOT the analog RC curve 1/sqrt(1 + (2*pi*f*tau)^2).
 *
 * That distinction matters and cost a debugging round. 75 us at 43750 Hz is
 * not a slow filter relative to the sample rate, so the two diverge by up to
 * 10% inside the audio band -- measured 0.7277 analog against 0.6818 discrete
 * at 2 kHz, 0.3907 against 0.3549 at 5 kHz. Using the analog form as the
 * reference here made a perfectly flat decimator look 10% down at 5 kHz.
 *
 * The tests below use this to divide de-emphasis out so they measure the
 * decimator. Whether the implemented one-pole *should* match the broadcast
 * standard's analog curve more closely is a separate question, left open
 * rather than silently asserted either way here.
 */
static double deemph_gain(double f)
{
    double dt = 1.0 / OUT_RATE;
    double tau = (double)DEEMPHASIS * 1e-6;
    double a = dt / (dt + tau);
    double w = 2.0 * M_PI * f / OUT_RATE;
    double re = 1.0 - (1.0 - a) * cos(w);
    double im = (1.0 - a) * sin(w);
    return a / sqrt(re * re + im * im);
}

/* One chunk the size fm_mode.c actually captures. */
#define N_IQ    49152u
#define N_AUDIO  2048u

static void demod_tone(double tone_hz, double amplitude,
                       float *audio, size_t *n_audio)
{
    float *iq_real = malloc(N_IQ * sizeof(float));
    float *iq_imag = malloc(N_IQ * sizeof(float));
    CHECK(iq_real && iq_imag);

    synth_fm(iq_real, iq_imag, N_IQ, tone_hz, amplitude);

    fm_demod_t d;
    CHECK(fm_demod_init(&d, IQ_RATE, DEVIATION, DEEMPHASIS) == 0);

    *n_audio = N_AUDIO;
    CHECK(fm_demod_process(&d, iq_real, iq_imag, N_IQ, audio, n_audio) == 0);

    free(iq_real);
    free(iq_imag);
}

static void test_recovers_tone_frequency(void)
{
    float audio[N_AUDIO];
    size_t n = 0;
    demod_tone(1000.0, 1.0, audio, &n);

    CHECK(n > 900);
    CHECK_NEAR(measure_freq(audio, n, OUT_RATE), 1000.0, 30.0);
}

static void test_recovers_tone_amplitude_with_deemphasis(void)
{
    float audio[N_AUDIO];
    size_t n = 0;
    demod_tone(1000.0, 1.0, audio, &n);

    /*
     * Full deviation maps to +/-1.0 before de-emphasis. The 75 us one-pole
     * attenuates 1 kHz by 0.9047, so a full-deviation 1 kHz tone should come
     * back near that, not at 1.0.
     */
    CHECK_NEAR(measure_peak(audio, n), deemph_gain(1000.0), 0.05);
}

static void test_half_deviation_halves_amplitude(void)
{
    float audio[N_AUDIO];
    size_t n = 0;
    demod_tone(1000.0, 0.5, audio, &n);

    CHECK_NEAR(measure_peak(audio, n), 0.5 * deemph_gain(1000.0), 0.05);
}

static void test_deemphasis_attenuates_higher_tones_more(void)
{
    float a1[N_AUDIO], a2[N_AUDIO];
    size_t n1 = 0, n2 = 0;
    demod_tone(1000.0, 1.0, a1, &n1);
    demod_tone(8000.0, 1.0, a2, &n2);

    /* 75 us: -0.87 dB at 1 kHz, -11.7 dB at 8 kHz. */
    CHECK(measure_peak(a2, n2) < measure_peak(a1, n1) * 0.5);
}

static void test_unmodulated_carrier_gives_silence(void)
{
    float audio[N_AUDIO];
    size_t n = 0;
    demod_tone(1000.0, 0.0, audio, &n);

    CHECK(measure_peak(audio, n) < 1e-3);
}

static void test_silence_does_not_poison_the_demodulator(void)
{
    float *re = calloc(N_IQ, sizeof(float));
    float *im = calloc(N_IQ, sizeof(float));
    float audio[N_AUDIO];
    CHECK(re && im);

    fm_demod_t d;
    CHECK(fm_demod_init(&d, IQ_RATE, DEVIATION, DEEMPHASIS) == 0);

    for (int chunk = 0; chunk < 4; chunk++) {
        size_t n = N_AUDIO;
        CHECK(fm_demod_process(&d, re, im, N_IQ, audio, &n) == 0);
        size_t bad = 0;
        for (size_t i = 0; i < n; i++)
            if (!isfinite(audio[i]))
                bad++;
        if (bad)
            printf("  %zu/%zu non-finite samples after silence\n", bad, n);
        CHECK(bad == 0);
    }
    CHECK(isfinite(d.inv_a2));

    synth_fm(re, im, N_IQ, 1000.0, 0.5);
    size_t n = N_AUDIO;
    CHECK(fm_demod_process(&d, re, im, N_IQ, audio, &n) == 0);
    size_t bad = 0;
    for (size_t i = 0; i < n; i++)
        if (!isfinite(audio[i]))
            bad++;
    if (bad)
        printf("  %zu/%zu non-finite samples after tone return\n", bad, n);
    CHECK(bad == 0);
    CHECK(measure_peak(audio, n) > 0.3);

    free(re);
    free(im);
}

/*
 * The decimation chain is flat across the audio band, not merely
 * approximately so: stage 1 and stage 2 both droop, and stage 3's taps carry
 * the inverse of that droop. Dropping the compensation would cost ~0.7 dB at
 * the top of the band, which this catches.
 *
 * De-emphasis is divided out using the discrete response the code implements,
 * so what remains is the decimator alone. Measured ratios across 500 Hz to
 * 14 kHz sit within 0.6% of unity.
 *
 * Stops at 14 kHz. At 15 kHz the measurement reads ~5% low, and that is not
 * the decimator -- the chain is 0.007 dB down there by design. It is the
 * cross-product discriminator and the Goertzel resolution at 0.69 of Nyquist.
 * Asserting a number there would be pinning measurement artefacts.
 */
static void test_passband_is_flat_against_deemphasis(void)
{
    static const double freqs[] = { 500.0, 1000.0, 2000.0, 5000.0,
                                    8000.0, 10000.0, 12000.0, 14000.0 };

    for (size_t i = 0; i < sizeof(freqs) / sizeof(freqs[0]); i++) {
        float audio[N_AUDIO];
        size_t n = 0;
        demod_tone(freqs[i], 1.0, audio, &n);

        /* Past the envelope estimator's startup, as measure_peak explains. */
        size_t skip = n / 3;
        double got = tone_amp(audio + skip, n - skip, freqs[i], OUT_RATE);
        double want = deemph_gain(freqs[i]);

        /* 3% covers the discriminator's own small-signal error and the
         * Goertzel's finite resolution. The decimator contributes 0.009 dB
         * (0.1%), so real passband droop would blow straight through it. */
        CHECK_NEAR(got / want, 1.0, 0.03);
    }
}

/*
 * The test that would have caught the shipped defect.
 *
 * A real FM broadcast carries far more than audio: a 19 kHz stereo pilot, the
 * L-R difference signal as DSB-SC around 38 kHz, and RDS at 57 kHz. The old
 * single 47-tap boxcar decimator had a 22.34 kHz Nyquist, so it passed the
 * pilot at -2.8 dB and folded 38 kHz to 6.7 kHz and 57 kHz to 12.3 kHz —
 * measured on air, 29.4% of delivered energy above 15 kHz.
 *
 * With the delivered rate now 43750 Hz the aliases land at:
 *   35 kHz (L-R lower)  -> 43750 - 35000 =  8750 Hz
 *   41 kHz (L-R upper)  -> 43750 - 41000 =  2750 Hz
 *   57 kHz (RDS)        -> 57000 - 43750 = 13250 Hz
 *   19 kHz (pilot)      -> stays at 19000 Hz, below Nyquist
 * All four are distinct from the 1 kHz mono tone, so each can be measured
 * separately rather than as an aggregate.
 */
#define MPX_CHUNKS 4u
#define MPX_N_IQ   (N_IQ * MPX_CHUNKS)
#define MPX_N_OUT  (MPX_N_IQ / FM_DECIM_TOTAL)

static const tone_t mpx_tones[] = {
    {  1000.0, 0.45  },   /* mono L+R                          */
    { 19000.0, 0.09  },   /* stereo pilot, 9% injection        */
    { 35000.0, 0.225 },   /* L-R lower sideband (38k - 3k)     */
    { 41000.0, 0.225 },   /* L-R upper sideband (38k + 3k)     */
    { 57000.0, 0.03  },   /* RDS, 3% injection                 */
};

/* Demodulate a full multiplex. Caller frees. Returns delivered sample count. */
static size_t demod_multiplex(float **audio_out)
{
    float *iq_real = malloc(MPX_N_IQ * sizeof(float));
    float *iq_imag = malloc(MPX_N_IQ * sizeof(float));
    float *audio   = malloc((MPX_N_OUT + 64) * sizeof(float));
    CHECK(iq_real && iq_imag && audio);
    if (!iq_real || !iq_imag || !audio) {
        free(iq_real); free(iq_imag); free(audio);
        *audio_out = NULL;
        return 0;
    }

    synth_fm_multi(iq_real, iq_imag, MPX_N_IQ, mpx_tones,
                   sizeof(mpx_tones) / sizeof(mpx_tones[0]));

    fm_demod_t d;
    CHECK(fm_demod_init(&d, IQ_RATE, DEVIATION, DEEMPHASIS) == 0);

    size_t n = MPX_N_OUT + 64;
    CHECK(fm_demod_process(&d, iq_real, iq_imag, MPX_N_IQ, audio, &n) == 0);

    free(iq_real);
    free(iq_imag);
    *audio_out = audio;
    return n;
}

/*
 * Rejection floor asserted by the multiplex tests, in dB below the recovered
 * mono tone.
 *
 * Measured, via --dump-rejection, over 3072 delivered samples:
 *
 *   mono 1 kHz            3.971e-01      0.0 dB
 *   pilot 19 kHz          9.593e-06    -92.3 dB
 *   L-R lower -> 8750 Hz  2.943e-06   -102.6 dB
 *   L-R upper -> 2750 Hz  1.904e-06   -106.4 dB
 *   RDS -> 13250 Hz       7.064e-09   -155.0 dB
 *
 * Deeper than scripts/fm_decim_design.py --analyse predicts for the filters
 * alone (62, 85, 96, 139 dB) because de-emphasis attenuates each alias again
 * at the frequency it lands on.
 *
 * The threshold is 60 dB: 32 dB of margin below the worst measured value, and
 * still far above what the boxcar this replaces could reach. That boxcar
 * delivered the pilot 33 dB down and the L-R lower sideband 30 dB down, so it
 * fails this by 27 dB.
 */
#define MPX_REJECT_DB 60.0

static void test_stereo_pilot_is_rejected(void)
{
    float *audio = NULL;
    size_t n = demod_multiplex(&audio);
    if (!audio) return;

    size_t skip = n / 4;
    double mono  = tone_amp(audio + skip, n - skip, 1000.0, OUT_RATE);
    double pilot = tone_amp(audio + skip, n - skip, 19000.0, OUT_RATE);

    CHECK(mono > 0.3);          /* the wanted signal survived */
    CHECK_BELOW_DB(pilot, mono, MPX_REJECT_DB);
    free(audio);
}

static void test_stereo_subcarrier_does_not_fold_into_audio(void)
{
    float *audio = NULL;
    size_t n = demod_multiplex(&audio);
    if (!audio) return;

    size_t skip = n / 4;
    double mono  = tone_amp(audio + skip, n - skip, 1000.0, OUT_RATE);
    /* 35 kHz and 41 kHz fold here. Both are 22.5% injection, the loudest
     * things in the multiplex after the mono sum. */
    double lower = tone_amp(audio + skip, n - skip, 8750.0, OUT_RATE);
    double upper = tone_amp(audio + skip, n - skip, 2750.0, OUT_RATE);

    CHECK_BELOW_DB(lower, mono, MPX_REJECT_DB);
    CHECK_BELOW_DB(upper, mono, MPX_REJECT_DB);
    free(audio);
}

static void test_rds_does_not_fold_into_audio(void)
{
    float *audio = NULL;
    size_t n = demod_multiplex(&audio);
    if (!audio) return;

    size_t skip = n / 4;
    double mono = tone_amp(audio + skip, n - skip, 1000.0, OUT_RATE);
    double rds  = tone_amp(audio + skip, n - skip, 13250.0, OUT_RATE);

    CHECK_BELOW_DB(rds, mono, MPX_REJECT_DB);
    free(audio);
}

/*
 * With a full multiplex present, almost all delivered energy must be the
 * mono programme. This is the aggregate form of the three tests above and
 * the direct analogue of the on-air measurement in docs/fm-rx.md, which found
 * 40.3% of energy in 8-15 kHz and 29.4% above 15 kHz.
 */
static void test_multiplex_energy_is_almost_all_audio(void)
{
    float *audio = NULL;
    size_t n = demod_multiplex(&audio);
    if (!audio) return;

    size_t skip = n / 4;
    const float *x = audio + skip;
    size_t m = n - skip;

    double mean = 0.0;
    for (size_t i = 0; i < m; i++)
        mean += x[i];
    mean /= (double)m;

    /* Total AC power, and the power in the 1 kHz mono tone alone. */
    double total = 0.0;
    for (size_t i = 0; i < m; i++) {
        double v = (double)x[i] - mean;
        total += v * v;
    }
    total /= (double)m;

    double a = tone_amp(x, m, 1000.0, OUT_RATE);
    double mono_power = 0.5 * a * a;

    CHECK(total > 0.0);
    /* The mono tone is the only wanted component, so it should account for
     * essentially all the AC power. The old chain left ~30% of it in folded
     * subcarriers. */
    CHECK(mono_power / total > 0.98);
    free(audio);
}

/*
 * 49152 IQ samples decimate by 48 to exactly 1024, which is
 * FM_AUDIO_FRAME_SIZE. This is not a coincidence to be preserved by luck:
 * at the old decimation of 47 the same chunk produced 1045 samples and the
 * ring push silently dropped 21 per frame, a 2% shortfall plus a
 * discontinuity at every frame boundary.
 */
static void test_output_count_matches_decimation(void)
{
    float audio[N_AUDIO];
    size_t n = 0;
    demod_tone(1000.0, 1.0, audio, &n);

    CHECK(n == N_IQ / FM_DECIM_TOTAL);
    CHECK(n == 1024u);
    CHECK(N_IQ % FM_DECIM_TOTAL == 0u);
}

static void test_reported_rate_is_the_delivered_rate(void)
{
    fm_demod_t d;
    CHECK(fm_demod_init(&d, IQ_RATE, DEVIATION, DEEMPHASIS) == 0);
    CHECK(fm_demod_audio_rate(&d) == 43750u);
    CHECK(fm_demod_audio_rate(&d) == IQ_RATE / FM_DECIM_TOTAL);
    /* Exact integer, so no accumulating timestamp drift on the client. */
    CHECK(IQ_RATE % FM_DECIM_TOTAL == 0u);
}

/*
 * The tap tables anti-alias correctly for one rate pair only, so init must
 * refuse anything else rather than filter at the wrong frequencies and
 * deliver plausible-sounding aliased audio.
 */
static void test_rejects_unsupported_iq_rate(void)
{
    fm_demod_t d;
    /* Says "somewhere" rather than "below" on purpose: this goes to stderr
     * and the test output to stdout, so when either is piped the ordering is
     * whatever the buffering decides. */
    printf("  (one 'unsupported' message on stderr somewhere is expected)\n");
    CHECK(fm_demod_init(&d, 1000000u, DEVIATION, DEEMPHASIS) != 0);
    CHECK(fm_demod_init(&d, IQ_RATE, 0u, DEEMPHASIS) != 0);
}

/*
 * A zero-capacity output buffer must produce nothing. The loop used to
 * write the first sample and only then compare the index with the capacity,
 * so a caller passing max_out == 0 lost one float past the end of its buffer.
 */
static void test_zero_output_capacity_writes_nothing(void)
{
    float *iq_real = calloc(4096, sizeof(float));
    float *iq_imag = calloc(4096, sizeof(float));
    float audio[1] = { 123.0f };
    size_t n = 0;
    fm_demod_t d;

    CHECK(iq_real && iq_imag);
    CHECK(fm_demod_init(&d, IQ_RATE, DEVIATION, DEEMPHASIS) == 0);
    CHECK(fm_demod_process(&d, iq_real, iq_imag, 4096, audio, &n) == 0);
    CHECK(n == 0);
    CHECK(audio[0] == 123.0f);

    free(iq_real);
    free(iq_imag);
}

/*
 * Chunk boundaries must be invisible. Nothing tested this before: every test
 * made exactly one fm_demod_process call on a fresh state, so the three FIR
 * history buffers and phase counters carried nothing across a call and a bug
 * there could not show up.
 *
 * 16384 is deliberately not a multiple of 48, so the split lands mid-phase in
 * all three stages.
 */
static void test_streaming_across_chunks_is_continuous(void)
{
    float *iq_real = malloc(N_IQ * sizeof(float));
    float *iq_imag = malloc(N_IQ * sizeof(float));
    CHECK(iq_real && iq_imag);
    if (!iq_real || !iq_imag) { free(iq_real); free(iq_imag); return; }
    synth_fm(iq_real, iq_imag, N_IQ, 1000.0, 1.0);

    float whole[N_AUDIO], piecewise[N_AUDIO];
    size_t n_whole = N_AUDIO, n_piece = 0;

    fm_demod_t a;
    CHECK(fm_demod_init(&a, IQ_RATE, DEVIATION, DEEMPHASIS) == 0);
    CHECK(fm_demod_process(&a, iq_real, iq_imag, N_IQ, whole, &n_whole) == 0);

    fm_demod_t b;
    CHECK(fm_demod_init(&b, IQ_RATE, DEVIATION, DEEMPHASIS) == 0);
    const size_t step = 16384;
    CHECK(N_IQ % step == 0u);
    CHECK(step % FM_DECIM_TOTAL != 0u);
    for (size_t off = 0; off < N_IQ; off += step) {
        size_t got = N_AUDIO - n_piece;
        CHECK(fm_demod_process(&b, iq_real + off, iq_imag + off, step,
                               piecewise + n_piece, &got) == 0);
        n_piece += got;
    }

    CHECK(n_piece == n_whole);
    /* Bit-identical: the same arithmetic in the same order, only the call
     * boundaries moved. */
    for (size_t i = 0; i < n_whole && i < n_piece; i++)
        CHECK(whole[i] == piecewise[i]);

    free(iq_real);
    free(iq_imag);
}

/*
 * Golden vector, for detecting unintended numerical change. Printed on
 * mismatch (--dump-golden) so a deliberate change can be re-baselined with
 * the delta visible.
 *
 * Re-baselined twice:
 *
 *   1. When the per-sample divide became a per-block reciprocal. Max
 *      deviation 4.7e-3 (0.47% of peak) confined to the envelope estimator's
 *      startup transient, 7.2e-4 (0.07%) settled, error-to-signal -63.8 dB.
 *   2. When the single 47-tap boxcar became the three-stage 6x4x2 FIR chain.
 *      This one is not a refactor and the output is not comparable sample for
 *      sample: the decimation factor changed from 47 to 48, so the delivered
 *      rate moved from 44680.85 Hz to 43750 Hz and sample i is a different
 *      instant. The passband behaviour is pinned by the tone, amplitude,
 *      flatness and de-emphasis tests instead, and the stopband by the
 *      multiplex tests. This vector's job from here is only to detect
 *      unintended drift.
 *   3. When the FIR MAC loop went from one accumulator to four, to break the
 *      vmla latency chain. Pure float reassociation: max deviation 1.79e-7 on
 *      a 0.93 sample, -134 dB, and every behavioural test above held
 *      unchanged. The four-way order is written out in fir_push rather than
 *      left to the compiler precisely so this vector stays reproducible
 *      across build flags.
 */
static void test_golden_output_is_unchanged(void)
{
    static const struct { size_t idx; double val; } golden[] = {
        {   0,     0.000000482568 },
        {   1,    -0.000036092770 },
        { 100,    -0.929161429405 },
        { 500,    -0.574668169022 },
        {1000,     0.803948581219 },
        {1023,    -0.738208591938 },
    };

    float audio[N_AUDIO];
    size_t n = 0;
    demod_tone(1000.0, 1.0, audio, &n);

    for (size_t k = 0; k < sizeof(golden) / sizeof(golden[0]); k++) {
        CHECK(golden[k].idx < n);
        CHECK_NEAR(audio[golden[k].idx], golden[k].val, 1e-7);
    }
}

/* Prints the values the golden test wants, for re-baselining. */
static void dump_golden(void)
{
    float audio[N_AUDIO];
    size_t n = 0;
    demod_tone(1000.0, 1.0, audio, &n);
    const size_t idx[] = {0, 1, 100, 500, 1000, 1023};
    printf("n_audio = %zu\n", n);
    for (size_t k = 0; k < sizeof(idx) / sizeof(idx[0]); k++)
        printf("        { %4zu, %18.12f },\n", idx[k], (double)audio[idx[k]]);
}

/* Prints the multiplex rejection actually achieved, for comparison against
 * scripts/fm_decim_design.py --analyse. */
static void dump_rejection(void)
{
    float *audio = NULL;
    size_t n = demod_multiplex(&audio);
    if (!audio) return;
    size_t skip = n / 4;
    const float *x = audio + skip;
    size_t m = n - skip;
    double mono = tone_amp(x, m, 1000.0, OUT_RATE);
    printf("delivered %zu samples at %.0f Hz, measuring %zu\n",
           n, OUT_RATE, m);
    printf("  %-22s %10s %10s\n", "component", "amp", "dB rel mono");
    struct { const char *name; double f; } probe[] = {
        { "mono 1 kHz",        1000.0 },
        { "pilot 19 kHz",     19000.0 },
        { "L-R lower -> 8750", 8750.0 },
        { "L-R upper -> 2750", 2750.0 },
        { "RDS -> 13250",     13250.0 },
    };
    for (size_t i = 0; i < sizeof(probe) / sizeof(probe[0]); i++) {
        double a = tone_amp(x, m, probe[i].f, OUT_RATE);
        printf("  %-22s %10.3e %10.1f\n", probe[i].name, a,
               20.0 * log10((a + 1e-30) / (mono + 1e-30)));
    }
    free(audio);
}

/*
 * Integration: an RDS-modulated subcarrier in a full stereo multiplex,
 * through the real discriminator and the stage-1 tap, must decode. This is
 * the test that proves the RDS chain is wired to the right intermediate
 * value, which the unit tests in test_rds_rx.c cannot see.
 */
static uint16_t rds_enc_crc(uint16_t info)
{
    static const uint16_t res[16] = {
        0x1B9, 0x372, 0x35D, 0x303, 0x3BF, 0x2C7, 0x037, 0x06E,
        0x0DC, 0x1B8, 0x370, 0x359, 0x30B, 0x3AF, 0x2E7, 0x077,
    };
    uint16_t rem = 0;
    for (int i = 0; i < 16; i++)
        if ((info >> i) & 1u)
            rem ^= res[i];
    return rem;
}

static void rds_integration_run(double fade_hz, double fade_min)
{
    enum { N_GROUPS = 12 };
    static const uint16_t offsets[4] = { 0x0FC, 0x198, 0x168, 0x1B4 };
    static const char ps[4][2] = { {'T','E'}, {'S','T'}, {'F','M'}, {' ',' '} };

    uint16_t groups[N_GROUPS][4];
    for (int g = 0; g < N_GROUPS; g++) {
        int a = g & 3;
        groups[g][0] = 0x8203;
        groups[g][1] = (uint16_t)((1 << 10) | (10 << 5) | (1 << 3) | a);
        groups[g][2] = 0x7E5C;      /* AF codes 126, 92 */
        groups[g][3] = (uint16_t)(((uint8_t)ps[a][0] << 8) | ps[a][1]);
    }

    size_t n_bits = N_GROUPS * 104;
    uint8_t *enc = malloc(n_bits);
    CHECK(enc);
    if (!enc) return;
    size_t k = 0;
    uint8_t e = 0;
    for (int g = 0; g < N_GROUPS; g++) {
        for (int b = 0; b < 4; b++) {
            uint32_t w = ((uint32_t)groups[g][b] << 10)
                       | (rds_enc_crc(groups[g][b]) ^ offsets[b]);
            for (int i = 25; i >= 0; i--) {
                e ^= (uint8_t)((w >> i) & 1u);
                enc[k++] = e;
            }
        }
    }

    size_t n_iq = (size_t)((double)n_bits / 1187.5 * (double)IQ_RATE)
                + N_IQ;
    float *wave = malloc(n_iq * sizeof(float));
    float *iq_real = malloc(n_iq * sizeof(float));
    float *iq_imag = malloc(n_iq * sizeof(float));
    CHECK(wave && iq_real && iq_imag);
    if (!wave || !iq_real || !iq_imag) {
        free(enc); free(wave); free(iq_real); free(iq_imag);
        return;
    }
    for (size_t i = 0; i < n_iq; i++) {
        double pos = (double)i / (double)IQ_RATE * 1187.5;
        float v = 0.0f;
        if (pos < (double)n_bits) {
            size_t bit = (size_t)pos;
            double frac = pos - (double)bit;
            double s = enc[bit] ? 1.0 : -1.0;
            v = (float)((frac < 0.5) ? s : -s);
        }
        wave[i] = v;
    }

    synth_fm_full(iq_real, iq_imag, n_iq, mpx_tones,
                  sizeof(mpx_tones) / sizeof(mpx_tones[0]), wave);

    if (fade_hz > 0.0) {
        for (size_t i = 0; i < n_iq; i++) {
            double t = (double)i / (double)IQ_RATE;
            double env = fade_min + (1.0 - fade_min)
                       * 0.5 * (1.0 + cos(2.0 * M_PI * fade_hz * t));
            iq_real[i] = (float)(iq_real[i] * env);
            iq_imag[i] = (float)(iq_imag[i] * env);
        }
    }

    fm_demod_t d;
    CHECK(fm_demod_init(&d, IQ_RATE, DEVIATION, DEEMPHASIS) == 0);

    size_t done = 0;
    while (done < n_iq) {
        size_t chunk = n_iq - done;
        if (chunk > N_IQ)
            chunk = N_IQ;
        float audio[N_AUDIO];
        size_t n_audio = N_AUDIO;
        CHECK(fm_demod_process(&d, iq_real + done, iq_imag + done, chunk,
                               audio, &n_audio) == 0);
        done += chunk;
    }

    const rds_state_t *s = rds_rx_state(&d.rds);
    CHECK(s->pi_valid && s->pi == 0x8203);
    CHECK(s->pty == 10);
    CHECK(rds_ps_valid(s));
    CHECK(memcmp(s->ps, "TESTFM  ", 8) == 0);
    CHECK(s->groups > 4);

    free(enc);
    free(wave);
    free(iq_real);
    free(iq_imag);
}

static void test_counts_adc_rails(void)
{
    float *re = calloc(N_IQ, sizeof(float));
    float *im = calloc(N_IQ, sizeof(float));
    CHECK(re && im);
    re[0] = 2047.0f / 2048.0f;
    im[0] = 0.0f;
    re[1] = 0.0f;
    im[1] = -1.0f;
    re[2] = -2047.0f / 2048.0f;   /* adjacent, not a rail */
    im[2] = 2046.0f / 2048.0f;    /* adjacent, not a rail */

    fm_demod_t d;
    CHECK(fm_demod_init(&d, IQ_RATE, DEVIATION, DEEMPHASIS) == 0);
    float audio[N_AUDIO];
    size_t n = N_AUDIO;
    CHECK(fm_demod_process(&d, re, im, N_IQ, audio, &n) == 0);

    CHECK(d.sample_count == (uint64_t)N_IQ * 2);
    CHECK(d.clip_count == 2);

    free(re);
    free(im);
}

static void test_rds_decodes_through_the_demod(void)
{
    rds_integration_run(0.0, 1.0);
}

/*
 * The same stream with a deep 300 Hz envelope fade, the shape that broke
 * the old smoothed-power discriminator normalisation: the smoothed power
 * lags the fade by ~1 ms, so while the instantaneous product collapses the
 * divider is still large, the output explodes past the clamp, and impulsive
 * noise buried the 57 kHz subcarrier while the audio still sounded
 * plausible. Verified to fail against the audio path's normalisation and
 * pass against the RDS path's fade-bounded one. The fade rate matters --
 * a 25 Hz fade is slow enough that the smoothed power tracks it.
 */
static void test_rds_survives_amplitude_fades(void)
{
    rds_integration_run(300.0, 0.02);
}

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "--dump-golden") == 0) {
        dump_golden();
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "--dump-rejection") == 0) {
        dump_rejection();
        return 0;
    }

    RUN(test_recovers_tone_frequency);
    RUN(test_recovers_tone_amplitude_with_deemphasis);
    RUN(test_half_deviation_halves_amplitude);
    RUN(test_deemphasis_attenuates_higher_tones_more);
    RUN(test_unmodulated_carrier_gives_silence);
    RUN(test_silence_does_not_poison_the_demodulator);
    RUN(test_passband_is_flat_against_deemphasis);
    RUN(test_stereo_pilot_is_rejected);
    RUN(test_stereo_subcarrier_does_not_fold_into_audio);
    RUN(test_rds_does_not_fold_into_audio);
    RUN(test_multiplex_energy_is_almost_all_audio);
    RUN(test_output_count_matches_decimation);
    RUN(test_reported_rate_is_the_delivered_rate);
    RUN(test_rejects_unsupported_iq_rate);
    RUN(test_zero_output_capacity_writes_nothing);
    RUN(test_streaming_across_chunks_is_continuous);
    RUN(test_golden_output_is_unchanged);
    RUN(test_counts_adc_rails);
    RUN(test_rds_decodes_through_the_demod);
    RUN(test_rds_survives_amplitude_fades);

    if (failures) {
        printf("\n%d check(s) FAILED\n", failures);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
