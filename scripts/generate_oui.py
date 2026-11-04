#!/usr/bin/env python3
"""Generate oui.py lookup table from Wireshark's manuf file.

Source: https://www.wireshark.org/download/automated/data/manuf
Format: MAC_prefix<tab>short_name<tab>full_name
We only use 24-bit (XX:XX:XX) prefixes and the short name column.
"""
import argparse
from pathlib import Path

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / 'client' / 'shesha' / 'oui.py'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('manuf', nargs='?', default='manuf',
                    help="Wireshark manuf file (default: ./manuf)")
    ap.add_argument('-o', '--output', type=Path, default=DEFAULT_OUTPUT,
                    help=f"generated module (default: {DEFAULT_OUTPUT})")
    args = ap.parse_args(argv)

    manuf_file = Path(args.manuf)
    entries = {}

    with open(manuf_file, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) < 2:
                continue
            mac_prefix = parts[0].strip()
            short_name = parts[1].strip()
            # Only 24-bit OUIs (XX:XX:XX format, 8 chars)
            if len(mac_prefix) != 8 or mac_prefix[2] != ':' or mac_prefix[5] != ':':
                continue
            oui = mac_prefix.lower()
            if short_name:
                entries[oui] = short_name

    # Write output
    out = args.output
    lines = ['"""OUI (Organizationally Unique Identifier) lookup from Wireshark manuf.\n',
             '\n',
             'Maps the first 3 bytes of a MAC address to a short manufacturer name.\n',
             'Source: https://www.wireshark.org/download/automated/data/manuf (Wireshark\n',
             'manuf, GPL-2.0-or-later). This generated data table is distributed under\n',
             'that license; the rest of shesha remains MIT. Regenerate with\n',
             'scripts/generate_oui.py; do not edit by hand.\n',
             '"""\n',
             '\n',
             '\n',
             'def _is_locally_administered(mac: str) -> bool:\n',
             '    """Check if MAC has the Locally Administered (LA) bit set."""\n',
             "    first_byte = int(mac.split(':')[0], 16)\n",
             '    return bool(first_byte & 0x02)\n',
             '\n',
             '\n',
             'def manufacturer(mac: str) -> str:\n',
             '    """Look up manufacturer from MAC address OUI prefix.\n',
             '\n',
             '    Returns manufacturer short name, "Private MAC" if LA bit is set, or "" if unknown.\n',
             '    """\n',
             '    if not mac or len(mac) < 8:\n',
             '        return ""\n',
             '    if _is_locally_administered(mac):\n',
             '        return "Private MAC"\n',
             '    oui = mac[:8].lower()\n',
             '    return _OUI_TABLE.get(oui, "")\n',
             '\n',
             '\n',
             '_OUI_TABLE: dict[str, str] = {\n']
    for oui, name in sorted(entries.items()):
        lines.append(f'    {oui!r}: {name!r},\n')
    lines.append('}\n')

    out.write_text(''.join(lines))
    print(f"Wrote {len(entries)} entries to {out}")


if __name__ == '__main__':
    main()
