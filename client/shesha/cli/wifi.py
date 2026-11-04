from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

from shesha.channels import CHANNEL_TO_FREQ, freq_to_channel
from shesha.client import MODE_NAMES
from shesha.frames import (
    BROADCAST, DEFAULT_BSSID, build_beacon, build_deauth, build_raw,
    parse_mac,
)
from shesha.protocol import DaemonMode, TX_PROTO_WIFI

from .common import (
    AGC_CODES, AGC_NAMES, TX_PROTO_NAMES, RadioState, _connect, check, fail,
    print_json, restore_radio, snapshot_radio,
)


def _scan_dict(scan, result, *, freq_hz, agc, gain_db, duration_s) -> dict:
    return {
        "freq_hz": freq_hz,
        "agc": agc,
        "gain_db": gain_db,
        "duration_s": duration_s,
        "frames": result.wifi_count,
        "fcs_ok": result.wifi_fcs_ok,
        "aps": [asdict(ap) for ap in scan.aps.values()],
        "stations": [asdict(sta) for sta in scan.stations.values()],
        "errors": result.errors,
    }


def _tx_status_dict(status, ts) -> dict:
    active = bool(status.tx_busy) or (
        ts is not None and (ts.tx_active or ts.fm_active or ts.iq_active))
    if ts is not None and ts.iq_active:
        proto = "iq"
    elif ts is not None and ts.fm_active:
        proto = "fm"
    else:
        proto = TX_PROTO_NAMES.get(status.tx_proto, "unknown")
    data = {
        "active": active,
        "active_proto": proto if active else None,
        "tx_busy": bool(status.tx_busy),
        "tx_proto": status.tx_proto,
        "tx_proto_name": TX_PROTO_NAMES.get(status.tx_proto, "unknown"),
        "tx_flags": status.tx_flags,
        "tx_repeat": status.tx_repeat,
        "tx_interval_us": status.tx_interval_us,
        "tx_atten_db": status.tx_atten_db,
    }
    if ts is not None:
        data.update({
            "tx_active": ts.tx_active,
            "end_of_stream": ts.end_of_stream,
            "dac_rate": ts.dac_rate,
            "fed": ts.fed,
            "emitted": ts.emitted,
            "elapsed_us": ts.elapsed_us,
            "depth": ts.depth,
            "underruns": ts.underruns,
            "fm_active": ts.fm_active,
            "fm_ring_fill": ts.ring_fill,
            "fm_ring_size": ts.ring_size,
            "audio_rate": ts.audio_rate,
            "starved_samples": ts.starved_samples,
            "iq_active": ts.iq_active,
            "iq_ring_fill": ts.iq_ring_fill,
            "iq_starved": ts.iq_starved,
        })
    return data


def format_tx_status(status, ts) -> str:
    active = bool(status.tx_busy) or (
        ts is not None and (ts.tx_active or ts.fm_active or ts.iq_active))
    if ts is not None and ts.iq_active:
        proto = "iq"
    elif ts is not None and ts.fm_active:
        proto = "fm"
    else:
        proto = TX_PROTO_NAMES.get(status.tx_proto, "?")
    state = "busy" if active else "idle"
    if active:
        state += f" ({proto})"
    lines = [
        f"tx:         {state}",
        f"attenuation: {status.tx_atten_db:.1f} dB",
    ]
    if status.tx_busy:
        lines += [
            f"flags:       {status.tx_flags}",
            f"repeat:      {status.tx_repeat}",
            f"interval:    {status.tx_interval_us} us",
        ]
    if ts is not None and (ts.tx_active or ts.fm_active or ts.iq_active):
        lines += [
            f"dac rate:    {ts.dac_rate / 1e6:.3f} MSPS",
            f"emitted:     {ts.emitted} samples "
            f"({ts.rate_frac * 100:.2f}% nominal)",
        ]
        if ts.fm_active or ts.ring_fill:
            lines.append(
                f"fm stream:   {'active' if ts.fm_active else 'idle'}, "
                f"ring {ts.ring_fill_ms:.1f} ms, {ts.underruns} underruns")
        if ts.iq_active or ts.iq_ring_fill:
            lines.append(
                f"iq stream:   {'active' if ts.iq_active else 'idle'}, "
                f"ring {ts.iq_ring_fill} bytes, {ts.iq_starved} starved")
    return "\n".join(lines)


def do_scan(args: argparse.Namespace) -> None:
    from ..scan import ScanState

    state = None
    c = _connect(args)
    try:
        st, prev_mode = c.status() or (None, None)
        if st is None:
            fail("daemon not responding")
        state = RadioState.from_status(st, prev_mode)

        if args.gain is not None and args.agc not in (None, "manual"):
            fail("--gain requires --agc manual")
        _channel, freq_mhz = _resolve_wifi_channel(args)

        scan_mode = DaemonMode.WIFI
        mode_label = "wifi"

        print(f"status: {st.freq_hz/1e6:.0f} MHz, {st.gain_db:.0f} dB, "
              f"AGC={AGC_NAMES.get(st.agc_mode, '?')}, "
              f"mode={MODE_NAMES.get(prev_mode, '?')}",
              file=sys.stderr)

        check(c, c.set_mode(scan_mode),
              f"could not switch to {mode_label} mode")

        if args.agc is not None:
            agc_name = args.agc
        elif args.gain is not None:
            # A gain without manual AGC is a no-op in the daemon.
            agc_name = "manual"
        else:
            # Default to fast_attack for scan (multi-transmitter environment)
            agc_name = "fast"
        check(c, c.set_agc(AGC_CODES[agc_name]), "could not set AGC")
        print(f"  AGC \u2192 {agc_name}", file=sys.stderr)

        if args.gain is not None:
            check(c, c.set_gain(float(args.gain)), "could not set gain")
            print(f"  gain \u2192 {args.gain} dB", file=sys.stderr)
        hz = int(round(freq_mhz * 1e6))
        check(c, c.set_freq(hz), "could not set frequency")
        print(f"  freq \u2192 {freq_mhz} MHz", file=sys.stderr)

        st2, _ = c.status() or (None, -1)
        if st2:
            print(f"confirmed: {st2.freq_hz/1e6:.0f} MHz, "
                  f"{st2.gain_db:.0f} dB, "
                  f"AGC={AGC_NAMES.get(st2.agc_mode, '?')}",
                  file=sys.stderr)

        print(f"scanning {mode_label} for {args.duration}s...",
              file=sys.stderr)
        scan = ScanState()
        result = c.stream(duration=args.duration, wifi_sink=scan.update)
        groups = scan.group_aps()

        if args.json:
            print_json(_scan_dict(
                scan, result,
                freq_hz=st2.freq_hz if st2 else None,
                agc=AGC_NAMES.get(st2.agc_mode, "?") if st2 else agc_name,
                gain_db=st2.gain_db if st2 else args.gain,
                duration_s=args.duration))
        else:
            # Summary
            print(f"\nframes decoded: {result.wifi_count}")
            print(f"   FCS OK: {result.wifi_fcs_ok}")
            if result.errors:
                for e in result.errors:
                    print(f"  error: {e}")

            if groups:
                print(f"\nAP Table ({len(scan.aps)} BSSIDs, "
                      f"{len(groups)} physical devices):")
                hdr = (f"  {'SSID':<20} {'Ch':>3}  {'Security':<12} "
                       f"{'Cipher':<10} {'PMF':<5} {'CC':<3} {'PHY':<10} "
                       f"{'BSSID':<18} {'Bcns':>5} {'Last':>6}")
                print(hdr)
                print("  " + "\u2500" * (len(hdr) - 2))

                for group in groups:
                    for idx, bssid in enumerate(group):
                        ap = scan.aps[bssid]
                        if idx == 0:
                            ssid_col = (ap.ssid or "<hidden>")[:20]
                        else:
                            ssid_col = "  \u2514\u2500 (same AP)"
                        ch = str(ap.channel) if ap.channel else "?"
                        sec = ap.security if ap.security != "unknown" else "-"
                        cip = ap.cipher if ap.cipher != "unknown" else "-"
                        pmf = ap.pmf if ap.pmf != "none" else "-"
                        cc = ap.country or "-"
                        phy = ap.phy or "-"
                        last = f"{ap.last_seen:.1f}s"
                        print(f"  {ssid_col:<20} {ch:>3}  {sec:<12} "
                              f"{cip:<10} {pmf:<5} {cc:<3} {phy:<10} "
                              f"{bssid:<18} {ap.beacon_count:>5} {last:>6}")
            else:
                print("\nno APs discovered.")
                print("Suggestions:")
                print("  - Verify frequency: ch36=5180, ch1=2412, ch6=2437")
                print("  - Default AGC is fast; try --agc manual --gain 73")
                print("  - Try --duration 30 for longer dwell time")

            if scan.stations:
                print(f"\nStations ({len(scan.stations)}):")
                print(f"  {'MAC':<18} {'AP BSSID':<18} {'Frames':>7} "
                      f"{'Last':>6}")
                print("  " + "\u2500" * 55)
                for mac, sta in sorted(
                        scan.stations.items(),
                        key=lambda x: x[1].last_seen, reverse=True):
                    last = f"{sta.last_seen:.1f}s"
                    print(f"  {mac:<18} {sta.bssid:<18} "
                          f"{sta.frame_count:>7} {last:>6}")

    finally:
        restore_radio(c, state)
        c.disconnect()


def do_fpga(args: argparse.Namespace) -> None:
    state = None
    ok = False
    c = _connect(args)
    try:
        state = snapshot_radio(c)
        enable = args.state == "on"
        if enable:
            check(c, c.set_mode(DaemonMode.WIFI),
                  "could not switch to wifi mode")
        check(c, c.set_fpga(enable), "could not set FPGA state")
        ok = True
        if args.json:
            print_json({"fpga": args.state})
        else:
            print(f"FPGA pipeline: {args.state}")
    finally:
        if not ok:
            restore_radio(c, state)
        c.disconnect()


def _resolve_wifi_channel(args: argparse.Namespace, *,
                          needs_channel: bool = False
                          ) -> tuple[int | None, float]:
    """Resolve --channel/--freq, defaulting to channel 6."""
    channel = getattr(args, "channel", None)
    freq_mhz = getattr(args, "freq", None)
    if freq_mhz is not None:
        channel = freq_to_channel(freq_mhz)
        if needs_channel and channel == 0:
            fail(f"{freq_mhz} MHz is not a WiFi channel")
        channel = channel or None
    elif channel is None:
        channel = 6
    if channel is not None:
        if channel not in CHANNEL_TO_FREQ:
            fail(f"unknown WiFi channel: {channel}")
        if freq_mhz is None:
            freq_mhz = float(CHANNEL_TO_FREQ[channel])
    return channel, float(freq_mhz)


def _parse_hex(text: str, label: str) -> bytes:
    cleaned = "".join(text.replace(":", "").replace("-", "").split())
    try:
        data = bytes.fromhex(cleaned)
    except ValueError:
        fail(f"invalid {label}: {text}")
    if not data:
        fail(f"invalid {label}: {text}")
    return data


def _parse_mac_arg(text: str, label: str) -> bytes:
    try:
        return parse_mac(text)
    except ValueError:
        fail(f"invalid {label}: {text}")


def _parse_ie(spec: str) -> bytes:
    tag_text, sep, data_text = spec.partition(":")
    if not sep:
        fail(f"invalid --ie (expected TAG:HEX): {spec}")
    try:
        tag = int(tag_text, 0)
    except ValueError:
        fail(f"invalid --ie tag: {tag_text}")
    if not 0 <= tag <= 0xFF:
        fail(f"--ie tag out of range: {tag}")
    data = _parse_hex(data_text, "--ie data")
    if len(data) > 0xFF:
        fail("--ie data is longer than 255 bytes")
    return bytes([tag, len(data)]) + data


def _build_frame(args: argparse.Namespace, channel: int | None) -> bytes:
    try:
        if args.type == "beacon":
            bssid = (_parse_mac_arg(args.bssid, "--bssid")
                     if args.bssid else DEFAULT_BSSID)
            da = (_parse_mac_arg(args.da, "--da")
                  if args.da else BROADCAST)
            sa = _parse_mac_arg(args.sa, "--sa") if args.sa else None
            return build_beacon(
                ssid=args.ssid, bssid=bssid, da=da, sa=sa,
                channel=channel, seq=args.seq, capability=args.capability,
                beacon_interval=args.beacon_interval,
                ies=[_parse_ie(spec) for spec in args.ie])
        if args.type == "deauth":
            if not args.bssid:
                fail("deauth requires --bssid")
            if not args.da:
                fail("deauth requires --da")
            return build_deauth(
                da=_parse_mac_arg(args.da, "--da"),
                bssid=_parse_mac_arg(args.bssid, "--bssid"),
                sa=_parse_mac_arg(args.sa, "--sa") if args.sa else None,
                reason=args.reason, seq=args.seq)
        if not args.psdu_file and not args.psdu_hex:
            fail("raw frame requires --psdu-file or --psdu-hex")
        if args.psdu_file:
            try:
                data = Path(args.psdu_file).read_bytes()
            except OSError as exc:
                fail(f"cannot read {args.psdu_file}: {exc}")
        else:
            data = _parse_hex(args.psdu_hex, "--psdu-hex")
        return build_raw(data)
    except ValueError as exc:
        fail(str(exc))


def do_frame(args: argparse.Namespace) -> None:
    channel, freq_mhz = _resolve_wifi_channel(
        args, needs_channel=args.type == "beacon")
    psdu = _build_frame(args, channel)

    c = _connect(args)
    try:
        freq_hz = int(round(freq_mhz * 1e6))
        check(c, c.set_freq(freq_hz), "could not set frequency")
        check(c, c.wifi_tx(args.rate, psdu,
                           repeat=args.count, interval_us=args.interval,
                           cyclic=args.cyclic),
              f"could not transmit {args.type} frame")
        if args.json:
            data = {
                "type": args.type,
                "freq_mhz": freq_mhz,
                "channel": channel,
                "rate_mbps": args.rate,
                "repeat": args.count,
                "interval_us": args.interval,
                "cyclic": args.cyclic,
                "psdu_len": len(psdu),
            }
            if args.type == "beacon":
                data["ssid"] = args.ssid
            elif args.type == "deauth":
                data.update({
                    "da": args.da,
                    "sa": args.sa or args.bssid,
                    "bssid": args.bssid,
                    "reason": args.reason,
                })
            print_json(data)
        else:
            where = (f"channel {channel} ({freq_mhz:.0f} MHz)"
                     if channel is not None else f"{freq_mhz:.0f} MHz")
            print(f"{args.type}: {where}, {args.rate} Mbps, "
                  f"count {args.count}, {len(psdu)} byte PSDU")
    finally:
        c.disconnect()


def do_beacon(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        channel, freq_mhz = _resolve_wifi_channel(args, needs_channel=True)

        freq_hz = int(freq_mhz * 1e6)
        check(c, c.set_freq(freq_hz), "could not set frequency")

        psdu = build_beacon(ssid=args.ssid, channel=channel)
        check(c, c.wifi_tx(args.rate, psdu,
                           repeat=args.count, interval_us=args.interval,
                           cyclic=args.cyclic),
              "could not transmit beacon")
        if args.json:
            print_json({"ssid": args.ssid, "channel": channel,
                        "freq_mhz": freq_mhz,
                        "rate_mbps": args.rate, "repeat": args.count,
                        "interval_us": args.interval,
                        "cyclic": args.cyclic,
                        "psdu_len": len(psdu)})
        else:
            print(f'beacon: ssid "{args.ssid}", channel {channel} '
                  f'({freq_mhz:.0f} MHz), {args.rate} Mbps, '
                  f'count {args.count}')
    finally:
        c.disconnect()


def do_beacon_stop(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        check(c, c.tx_stop(TX_PROTO_WIFI), "could not stop WiFi transmission")
        if args.json:
            print_json({"stopped": True, "protocol": "wifi"})
        else:
            print("wifi transmission stopped")
    finally:
        c.disconnect()


def do_tx_status(args: argparse.Namespace) -> None:
    c = _connect(args)
    try:
        result = c.status()
        if result is None:
            fail("daemon not responding")
        status, _mode = result
        ts = c.tx_stats()
        if args.json:
            print_json(_tx_status_dict(status, ts))
        else:
            print(format_tx_status(status, ts))
    finally:
        c.disconnect()
