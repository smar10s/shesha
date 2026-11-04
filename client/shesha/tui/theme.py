"""Colour palettes for the shesha TUI.

Every palette defines the same keys. Views read them by role, never by
literal colour, so switching themes needs no view changes.
"""


def _build_colormap(stops: list[tuple[float, tuple[int, int, int]]]
                    ) -> list[tuple[int, int, int]]:
    """Build a 256-entry colormap from color stops via linear interpolation."""
    cmap = []
    for i in range(256):
        t = i / 255.0
        for j in range(len(stops) - 1):
            if stops[j][0] <= t <= stops[j + 1][0]:
                local_t = (t - stops[j][0]) / (stops[j + 1][0] - stops[j][0])
                a, b = stops[j][1], stops[j + 1][1]
                cmap.append((
                    int(a[0] + (b[0] - a[0]) * local_t),
                    int(a[1] + (b[1] - a[1]) * local_t),
                    int(a[2] + (b[2] - a[2]) * local_t),
                ))
                break
        else:
            cmap.append(stops[-1][1])
    return cmap


VIRIDIS_STOPS = [
    (0.000, ( 68,   1,  84)),
    (0.125, ( 72,  35, 116)),
    (0.250, ( 64,  67, 135)),
    (0.375, ( 52,  94, 141)),
    (0.500, ( 41, 120, 142)),
    (0.625, ( 33, 144, 140)),
    (0.750, ( 66, 174, 118)),
    (0.875, (147, 207,  92)),
    (1.000, (253, 231,  37)),
]

VIRIDIS_CMAP = _build_colormap(VIRIDIS_STOPS)

# Sampled from an OKLCH night-to-aurora ramp. The boosted chroma keeps the
# peaks distinct from Nord's pastel midtones.
NORD_STOPS = [
    (0.000, ( 46,  52,  64)),
    (0.067, ( 68,  84, 110)),
    (0.133, ( 89, 120, 159)),
    (0.200, (118, 131, 186)),
    (0.267, (154, 132, 194)),
    (0.333, (190, 132, 189)),
    (0.400, (214, 136, 182)),
    (0.467, (233, 141, 174)),
    (0.533, (251, 147, 162)),
    (0.600, (255, 157, 150)),
    (0.667, (255, 169, 142)),
    (0.733, (255, 181, 139)),
    (0.800, (255, 193, 137)),
    (0.867, (255, 205, 137)),
    (0.933, (255, 219, 140)),
    (1.000, (255, 232, 147)),
]

NORD_CMAP = _build_colormap(NORD_STOPS)

# Light-surface ramp: low values blend into nord5, peaks darken to a
# high-contrast red so they read on a bright background.
NORD_LIGHT_STOPS = [
    (0.00, (229, 233, 240)),   # nord5
    (0.15, (216, 222, 233)),   # nord4
    (0.30, (235, 203, 139)),   # nord13
    (0.50, (208, 135, 112)),   # nord12
    (0.72, (191,  97, 106)),   # nord11
    (0.88, (143,  73,  80)),   # darkened nord11
    (1.00, (105,  53,  58)),   # darker nord11
]

NORD_LIGHT_CMAP = _build_colormap(NORD_LIGHT_STOPS)


CYBERPUNK: dict = {
    'bg':           (16, 20, 36),
    'fg':           (8, 247, 254),       # neon cyan
    'accent':       (254, 83, 187),      # hot pink
    'dim':          (86, 95, 137),       # muted blue-grey, above plot_bg
    'warn':         (255, 85, 85),       # red alert
    'header_bg':    (26, 32, 56),
    'header_fg':    (8, 247, 254),
    'input_bg':     (16, 20, 36),
    'input_fg':     (42, 52, 89),
    'input_active': (8, 247, 254),
    'plot_bg':      (42, 52, 89),        # muted blue-grey
    'plot_fg':      (108, 126, 176),     # brightest muted tone; plot text
    'colormap':       VIRIDIS_CMAP,
    'glyph_daemon':   '\u2620',
    'glyph_tx':       '\u2301',
}

TOKYONIGHT: dict = {
    'bg':           (26, 27, 38),        # #1a1b26 editor background
    'fg':           (169, 177, 214),     # #a9b1d6 default text
    'accent':       (122, 162, 247),     # #7aa2f7 blue
    'dim':          (86, 95, 137),       # #565f89 comment grey
    'warn':         (247, 118, 142),     # #f7768e red
    'header_bg':    (36, 40, 59),        # #24283b lighter panel
    'header_fg':    (192, 202, 245),     # #c0caf5 brighter than fg
    'input_bg':     (26, 27, 38),        # #1a1b26 matches bg
    'input_fg':     (86, 95, 137),       # #565f89 idle prompt text
    'input_active': (158, 206, 106),     # #9ece6a green — diverges from fg
    'plot_bg':      (36, 40, 59),        # #24283b matches header_bg
    'plot_fg':      (86, 95, 137),       # #565f89 comment grey
    'colormap':       VIRIDIS_CMAP,
    'glyph_daemon':   '\u2620',
    'glyph_tx':       '\u2301',
}

NORD: dict = {
    'bg':           ( 46,  52,  64),     # nord0
    'fg':           (216, 222, 233),     # nord4
    'accent':       (136, 192, 208),     # nord8
    'dim':          ( 76,  86, 106),     # nord3
    'warn':         (191,  97, 106),     # nord11
    'header_bg':    ( 59,  66,  82),     # nord1
    'header_fg':    (236, 239, 244),     # nord6
    'input_bg':     ( 46,  52,  64),     # nord0
    'input_fg':     ( 76,  86, 106),     # nord3
    'input_active': (136, 192, 208),     # nord8
    'plot_bg':      ( 59,  66,  82),     # nord1
    'plot_fg':      (129, 161, 193),     # nord9
    'colormap':       NORD_CMAP,
    'glyph_daemon':   '\u2620',
    'glyph_tx':       '\u2301',
}

NORD_LIGHT: dict = {
    'bg':           (236, 239, 244),     # nord6
    'fg':           ( 46,  52,  64),     # nord0
    'accent':       ( 46,  92, 138),     # deepened nord10
    'dim':          ( 67,  76,  94),     # nord2
    'warn':         (138,  50,  58),     # deepened nord11
    'header_bg':    (216, 222, 233),     # nord4
    'header_fg':    ( 46,  52,  64),     # nord0
    'input_bg':     (229, 233, 240),     # nord5
    'input_fg':     ( 67,  76,  94),     # nord2
    'input_active': ( 46,  92, 138),     # deepened nord10
    'plot_bg':      (229, 233, 240),     # nord5
    'plot_fg':      ( 67,  76,  94),     # nord2
    'colormap':       NORD_LIGHT_CMAP,
    'glyph_daemon':   '\u2620',
    'glyph_tx':       '\u2301',
}

STYLES: dict[str, dict] = {
    'cyberpunk': CYBERPUNK,
    'nord': NORD,
    'nord_light': NORD_LIGHT,
    'tokyonight': TOKYONIGHT,
}

DEFAULT_STYLE = 'cyberpunk'

DEFAULT_PALETTE: dict = STYLES[DEFAULT_STYLE]
