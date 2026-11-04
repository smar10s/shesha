"""Command-line parser for the headless Shesha client."""
from __future__ import annotations

import argparse

from shesha.client import MODE_BY_NAME
from shesha.protocol import ACCUM_CODES, MAX_FM_AUDIO_RATE_HZ, WINDOW_CODES

from .common import (
    add_freq, add_json, bounded_int, finite_float, non_negative_float,
    positive_float,
)
from .debug import do_cpu, do_diag, do_stream
from .fm import do_fm_rx, do_fm_tx, do_rds
from .iq import do_iq_bits, do_iq_rx, do_iq_tx
from .radio import do_agc, do_bw, do_freq, do_gain, do_sample_rate
from .status import do_caps, do_status
from .viz import (
    do_accum, do_capture, do_fft, do_fps, do_window, do_workers,
)
from .wifi import (
    do_beacon, do_beacon_stop, do_fpga, do_frame, do_scan, do_tx_status,
)

FFT_SIZES = (256, 512, 1024, 2048, 4096, 8192)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shesha",
        description="Headless client for the Shesha SDR daemon.",
    )
    parser.add_argument(
        "--host", default="192.168.2.1",
        help="daemon address (default: 192.168.2.1)")
    parser.add_argument(
        "--port", type=bounded_int(1, 65535, "port"), default=5555,
        help="daemon TCP port (default: 5555)")

    sub = parser.add_subparsers(dest="group", required=True)

    p_status = sub.add_parser("status", help="show device and signal status")
    p_status.set_defaults(func=do_status)
    add_json(p_status)
    status_sub = p_status.add_subparsers(dest="status_command")
    p_caps = status_sub.add_parser("caps", help="show daemon capabilities")
    p_caps.set_defaults(func=do_caps)
    add_json(p_caps, default=argparse.SUPPRESS)

    p_radio = sub.add_parser("radio", help="set shared radio parameters")
    radio_sub = p_radio.add_subparsers(dest="radio_command", required=True)
    p_freq = radio_sub.add_parser("freq", help="set center frequency")
    p_freq.add_argument("mhz", type=positive_float, help="frequency in MHz")
    p_freq.set_defaults(func=do_freq)
    add_json(p_freq)
    p_bw = radio_sub.add_parser("bw", help="set analog bandwidth")
    p_bw.add_argument("mhz", type=positive_float, help="bandwidth in MHz")
    p_bw.set_defaults(func=do_bw)
    add_json(p_bw)
    p_gain = radio_sub.add_parser(
        "gain", help="set RX gain (switches AGC to manual)")
    p_gain.add_argument("db", type=finite_float, help="gain in dB")
    p_gain.set_defaults(func=do_gain)
    add_json(p_gain)
    p_agc = radio_sub.add_parser("agc", help="set the RX AGC mode")
    p_agc.add_argument("mode", choices=["manual", "fast", "slow"])
    p_agc.set_defaults(func=do_agc)
    add_json(p_agc)
    p_sr = radio_sub.add_parser("sample-rate", help="set the sample rate")
    p_sr.add_argument("msps", type=positive_float,
                      help="sample rate in MSPS")
    p_sr.set_defaults(func=do_sample_rate)
    add_json(p_sr)

    p_viz = sub.add_parser("viz", help="configure the spectrum view")
    viz_sub = p_viz.add_subparsers(dest="viz_command", required=True)
    p_fft = viz_sub.add_parser("fft", help="set the waterfall FFT size")
    p_fft.add_argument("size", type=int, choices=FFT_SIZES, help="FFT size")
    p_fft.set_defaults(func=do_fft)
    add_json(p_fft)
    p_fps = viz_sub.add_parser("fps", help="set the waterfall frame rate")
    p_fps.add_argument("fps", type=bounded_int(1, 255, "fps"),
                       help="frames per second")
    p_fps.set_defaults(func=do_fps)
    add_json(p_fps)
    p_window = viz_sub.add_parser("window", help="set the FFT window")
    p_window.add_argument("window", choices=list(WINDOW_CODES))
    p_window.set_defaults(func=do_window)
    add_json(p_window)
    p_accum = viz_sub.add_parser(
        "accum", help="set how buffered PSD frames are reduced")
    p_accum.add_argument("accum", choices=list(ACCUM_CODES))
    p_accum.set_defaults(func=do_accum)
    add_json(p_accum)
    p_workers = viz_sub.add_parser(
        "workers", help="set the PSD producer worker count")
    p_workers.add_argument("workers", type=int, choices=[1, 2])
    p_workers.set_defaults(func=do_workers)
    add_json(p_workers)

    p_capture = viz_sub.add_parser(
        "capture", help="capture PSD frames and summarize them")
    p_capture.add_argument("--duration", type=positive_float, default=5.0,
                           help="capture seconds (default: 5)")
    p_capture.add_argument("--out",
                           help="write one JSON object per PSD frame")
    p_capture.set_defaults(func=do_capture)
    add_json(p_capture)

    p_wifi = sub.add_parser("wifi", help="scan and transmit 802.11 frames")
    wifi_sub = p_wifi.add_subparsers(dest="wifi_command", required=True)
    p_scan = wifi_sub.add_parser("scan", help="scan for access points")
    add_freq(p_scan)
    p_scan.add_argument("--gain", type=finite_float, help="RX gain in dB")
    p_scan.add_argument("--agc", choices=["manual", "fast", "slow"],
                        help="AGC mode (default: fast)")
    p_scan.add_argument("--duration", type=positive_float, default=15.0,
                        help="scan duration in seconds (default: 15)")
    p_scan.set_defaults(func=do_scan)
    add_json(p_scan)

    p_fpga = wifi_sub.add_parser(
        "fpga", help="enable or disable the FPGA pipeline")
    p_fpga.add_argument("state", choices=["on", "off"])
    p_fpga.set_defaults(func=do_fpga)
    add_json(p_fpga)

    p_tx = wifi_sub.add_parser("tx", help="transmit frames")
    tx_sub = p_tx.add_subparsers(dest="tx_command", required=True)
    p_beacon = tx_sub.add_parser("beacon", help="transmit beacon frames")
    p_beacon.add_argument("--ssid", default="SHESHA_TEST",
                          help="beacon SSID (default: SHESHA_TEST)")
    beacon_freq = p_beacon.add_mutually_exclusive_group()
    beacon_freq.add_argument("--channel", type=int,
                             help="WiFi channel (default: 6)")
    beacon_freq.add_argument("--freq", type=positive_float,
                             help="frequency in MHz (maps to a WiFi channel)")
    p_beacon.add_argument("--rate",
                          type=bounded_int(6, 54, "TX rate in Mbps"),
                          default=6, help="TX rate in Mbps (default: 6)")
    p_beacon.add_argument("--count", type=bounded_int(0, 65535, "count"),
                          default=1,
                          help="beacon count; 0/1=one-shot, "
                               "65535=forever (CLI exit stops TX)")
    p_beacon.add_argument("--interval",
                          type=bounded_int(0, 0xFFFFFFFF, "interval"),
                          default=0,
                          help="microseconds between beacons")
    p_beacon.add_argument("--cyclic", action="store_true",
                          help="hardware-cycled TX (CLI exit stops it)")
    p_beacon.set_defaults(func=do_beacon)
    add_json(p_beacon)

    p_frame = tx_sub.add_parser(
        "frame", help="transmit a built or raw 802.11 PSDU")
    p_frame.add_argument("--type", required=True,
                         choices=["beacon", "deauth", "raw"],
                         help="frame template or raw PSDU")
    add_freq(p_frame)
    p_frame.add_argument("--rate",
                         type=bounded_int(6, 54, "TX rate in Mbps"),
                         default=6, help="TX rate in Mbps (default: 6)")
    p_frame.add_argument("--count", type=bounded_int(0, 65535, "count"),
                         default=1,
                         help="frame count; 0/1=one-shot, "
                              "65535=forever (CLI exit stops TX)")
    p_frame.add_argument("--interval",
                         type=bounded_int(0, 0xFFFFFFFF, "interval"),
                         default=0, help="microseconds between frames")
    p_frame.add_argument("--cyclic", action="store_true",
                         help="hardware-cycled TX (CLI exit stops it)")
    p_frame.add_argument("--ssid", default="SHESHA_TEST",
                         help="beacon SSID (default: SHESHA_TEST)")
    p_frame.add_argument("--bssid", help="BSSID as aa:bb:cc:dd:ee:ff")
    p_frame.add_argument("--da", help="destination MAC address")
    p_frame.add_argument("--sa", help="source MAC address")
    p_frame.add_argument("--seq",
                         type=bounded_int(0, 4095, "sequence number"),
                         default=0, help="sequence number (0-4095)")
    p_frame.add_argument("--capability", type=lambda value: int(value, 0),
                         default=0x0401,
                         help="beacon capability field (default: 0x0401)")
    p_frame.add_argument("--beacon-interval",
                         type=bounded_int(0, 65535, "beacon interval"),
                         default=100,
                         help="beacon interval in TU (default: 100)")
    p_frame.add_argument("--ie", action="append", default=[],
                         metavar="TAG:HEX",
                         help="append an information element (repeatable)")
    p_frame.add_argument("--reason",
                         type=bounded_int(0, 65535, "reason code"),
                         default=7,
                         help="deauth reason code (default: 7)")
    raw_source = p_frame.add_mutually_exclusive_group()
    raw_source.add_argument("--psdu-file",
                            help="file containing a raw PSDU (without FCS)")
    raw_source.add_argument("--psdu-hex",
                            help="raw PSDU as hex (without FCS)")
    p_frame.set_defaults(func=do_frame)
    add_json(p_frame)

    p_stop = tx_sub.add_parser(
        "stop", help="stop the current WiFi transmission")
    p_stop.set_defaults(func=do_beacon_stop)
    add_json(p_stop)
    p_tx_status = tx_sub.add_parser("status", help="show transmission state")
    p_tx_status.set_defaults(func=do_tx_status)
    add_json(p_tx_status)

    p_fm = sub.add_parser("fm", help="receive and transmit broadcast FM")
    fm_sub = p_fm.add_subparsers(dest="fm_command", required=True)
    p_fm_rx = fm_sub.add_parser("rx", help="receive FM audio")
    add_freq(p_fm_rx, required=True, help="FM frequency in MHz")
    p_fm_rx.add_argument("--gain", type=finite_float,
                         help="manual RX gain in dB (omit for AGC)")
    p_fm_rx.add_argument("--duration", type=non_negative_float, default=0.0,
                         help="seconds to listen; 0 = until Ctrl-C")
    p_fm_rx.add_argument("--wav", help="record decoded audio to a WAV file")
    p_fm_rx.add_argument("--volume", type=finite_float, default=1.0,
                         help="output volume scale (default: 1.0)")
    p_fm_rx.set_defaults(func=do_fm_rx)
    add_json(p_fm_rx)

    p_fm_tx = fm_sub.add_parser("tx", help="transmit an audio file over FM")
    add_freq(p_fm_tx, required=True, help="FM frequency in MHz")
    p_fm_tx.add_argument("--file", required=True,
                         help="audio file, or - for raw f32le stdin")
    p_fm_tx.add_argument(
        "--rate",
        type=bounded_int(1, MAX_FM_AUDIO_RATE_HZ, "audio rate in Hz"),
        default=44100,
        help="audio sample rate in Hz (default: 44100)")
    p_fm_tx.add_argument("--tx-atten", type=finite_float,
                         help="TX attenuation in dB")
    p_fm_tx.set_defaults(func=do_fm_tx)
    add_json(p_fm_tx)

    p_rds = fm_sub.add_parser("rds", help="monitor decoded RDS data")
    add_freq(p_rds, required=True, help="FM frequency in MHz")
    p_rds.add_argument("--gain", type=finite_float,
                       help="manual RX gain in dB (omit for AGC)")
    p_rds.add_argument("--duration", type=non_negative_float, default=0.0,
                       help="seconds to monitor; 0 = until Ctrl-C")
    p_rds.set_defaults(func=do_rds)
    add_json(p_rds)

    p_iq = sub.add_parser("iq", help="capture and replay raw IQ samples")
    iq_sub = p_iq.add_subparsers(dest="iq_command", required=True)
    p_iq_rx = iq_sub.add_parser("rx", help="capture raw IQ to a file")
    p_iq_rx.add_argument("--out", required=True,
                         help="output file (raw packed samples)")
    p_iq_rx.add_argument("--duration", type=non_negative_float, default=10.0,
                         help="capture seconds; 0 = until Ctrl-C")
    p_iq_rx.add_argument("--bits", type=int, choices=[8, 12], default=12)
    add_freq(p_iq_rx, help="center frequency in MHz")
    p_iq_rx.add_argument("--bw", type=positive_float,
                         help="analog bandwidth in MHz")
    p_iq_rx.add_argument("--sr", type=positive_float,
                         help="sample rate in MSPS")
    p_iq_rx.add_argument("--gain", type=finite_float,
                         help="manual RX gain in dB")
    p_iq_rx.add_argument("--meta", action="store_true",
                         help="write capture settings to OUT.meta.json")
    p_iq_rx.set_defaults(func=do_iq_rx)
    add_json(p_iq_rx)

    p_iq_tx = iq_sub.add_parser("tx", help="stream a raw packed IQ file")
    p_iq_tx.add_argument("--file", required=True,
                         help="input file (raw packed samples)")
    p_iq_tx.add_argument("--bits", type=int, choices=[8, 12], default=None,
                         help="wire sample width (default: sidecar or 12)")
    add_freq(p_iq_tx, help="center frequency in MHz")
    p_iq_tx.add_argument("--sr", type=positive_float,
                         help="sample rate in MSPS")
    p_iq_tx.add_argument("--tx-atten", type=finite_float,
                         help="TX attenuation in dB")
    p_iq_tx.set_defaults(func=do_iq_tx)
    add_json(p_iq_tx)

    p_iq_bits = iq_sub.add_parser(
        "bits", help="set the IQ wire sample width")
    p_iq_bits.add_argument("bits", type=int, choices=[8, 12])
    p_iq_bits.set_defaults(func=do_iq_bits)
    add_json(p_iq_bits)

    p_debug = sub.add_parser("debug", help="developer diagnostics")
    debug_sub = p_debug.add_subparsers(dest="debug_command", required=True)
    p_cpu = debug_sub.add_parser("cpu", help="show per-thread CPU share")
    p_cpu.add_argument("--interval", type=positive_float, default=2.0,
                       help="measurement window in seconds (default: 2.0)")
    p_cpu.add_argument("--count", type=bounded_int(1, 1_000_000, "count"),
                       default=1,
                       help="number of windows to report (default: 1)")
    p_cpu.set_defaults(func=do_cpu)
    add_json(p_cpu)

    p_diag = debug_sub.add_parser("diag", help="show WiFi decoder stats")
    add_freq(p_diag)
    p_diag.add_argument("--gain", type=finite_float, help="RX gain in dB")
    p_diag.add_argument("--agc", choices=["manual", "fast", "slow"])
    p_diag.add_argument("--duration", type=non_negative_float, default=5.0,
                        help="poll duration in seconds (default: 5)")
    p_diag.add_argument("--interval", type=positive_float, default=1.0,
                        help="poll interval in seconds (default: 1)")
    p_diag.add_argument("--continuous", "-c", action="store_true",
                        help="show a live table instead of one summary")
    p_diag.add_argument("--quiet", "-q", action="store_true",
                        help="CSV output")
    p_diag.set_defaults(func=do_diag)
    add_json(p_diag)

    p_stream = debug_sub.add_parser(
        "stream", help="capture and summarize a data stream")
    p_stream.add_argument("--duration", type=positive_float, default=5.0,
                          help="capture duration in seconds (default: 5)")
    p_stream.add_argument("--mode", choices=list(MODE_BY_NAME), default=None,
                          help="switch to this mode before capturing")
    p_stream.add_argument("--verbose", action="store_true",
                          help="include per-frame detail")
    p_stream.set_defaults(func=do_stream)
    add_json(p_stream)

    return parser
