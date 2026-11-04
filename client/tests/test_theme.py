"""Theme palette invariants.

These tests exist because the palette previously accumulated keys that
nothing read. They fail loudly if that happens again.
"""
from shesha.tui.config import KEY_SPECS
from shesha.tui.theme import STYLES, DEFAULT_STYLE


DEAD_KEYS = ('border', 'glyph_active', 'glyph_inactive', 'name')


def test_no_dead_keys():
    for name, style in STYLES.items():
        for key in DEAD_KEYS:
            assert key not in style, f'{name} still defines unused {key!r}'


def test_default_style_exists():
    assert DEFAULT_STYLE in STYLES


REQUIRED_KEYS = (
    'bg', 'fg', 'dim', 'accent', 'warn', 'header_bg', 'header_fg',
    'input_bg', 'input_fg', 'input_active', 'plot_bg', 'plot_fg',
    'colormap', 'glyph_daemon', 'glyph_tx',
)


def test_all_themes_define_every_key():
    """A palette with a gap breaks the interface it is meant to paint."""
    for name, style in STYLES.items():
        for key in REQUIRED_KEYS:
            assert key in style, f'{name} missing {key!r}'


# Foreground roles paired with the surface they are drawn on. A pair that
# is too close makes text vanish; dim == plot_bg in cyberpunk did exactly
# that to axis labels. plot_fg is the secondary tone for surfaces: plots,
# table stripes (header_bg) and the base background.
CONTRAST_PAIRS = (
    ('fg', 'bg'), ('dim', 'bg'), ('accent', 'bg'), ('warn', 'bg'),
    ('header_fg', 'header_bg'), ('input_fg', 'input_bg'),
    ('input_active', 'input_bg'),
    ('plot_fg', 'plot_bg'), ('plot_fg', 'bg'), ('plot_fg', 'header_bg'),
)

_MIN_CHANNEL_SUM = 90


def test_every_palette_separates_text_from_its_surface():
    for name, style in STYLES.items():
        for fg, bg in CONTRAST_PAIRS:
            distance = sum(abs(a - b) for a, b in zip(style[fg], style[bg]))
            assert distance >= _MIN_CHANNEL_SUM, (
                f'{name}: {fg} too close to {bg} (distance {distance})')


def test_dim_is_not_the_plot_surface():
    """Muted text must not vanish into the surface it is drawn on."""
    for name, style in STYLES.items():
        assert style['dim'] != style['plot_bg'], (
            f'{name}: dim and plot_bg are the same colour')


def test_every_theme_passes_config_validation():
    """A theme the config rejects cannot be selected or restored."""
    spec = KEY_SPECS['theme']
    for name in STYLES:
        valid, err = spec.validate(name)
        assert valid, f'config schema rejects theme {name!r}: {err}'


def test_unknown_theme_fails_config_validation():
    valid, err = KEY_SPECS['theme'].validate('no_such_theme')
    assert not valid, 'config schema accepts a theme with no palette'
    assert 'no_such_theme' in err


def test_themes_are_visually_distinct():
    palettes = {name: style['bg'] for name, style in STYLES.items()}
    assert len(set(palettes.values())) == len(palettes)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        value = value / 255
        return (value / 12.92 if value <= 0.04045
                else ((value + 0.055) / 1.055) ** 2.4)
    r, g, b = (channel(value) for value in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_colormaps_are_full_rgb_ramps():
    for name, style in STYLES.items():
        cmap = style['colormap']
        assert len(cmap) == 256, name
        assert all(
            len(color) == 3 and all(0 <= value <= 255 for value in color)
            for color in cmap
        ), name


def _contrast_ratio(a: tuple[int, int, int],
                    b: tuple[int, int, int]) -> float:
    lighter, darker = sorted(
        (_relative_luminance(a), _relative_luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_colormaps_move_away_from_their_background():
    """A sequential map that reverses direction makes magnitude unreadable."""
    for name, style in STYLES.items():
        lightness = [_relative_luminance(color) for color in style['colormap']]
        steps = [after - before for before, after in zip(lightness,
                                                         lightness[1:])]
        if _relative_luminance(style['bg']) < 0.5:
            bad = [(index, step) for index, step in enumerate(steps)
                   if step < -0.001]
            problem = 'dims'
        else:
            bad = [(index, step) for index, step in enumerate(steps)
                   if step > 0.001]
            problem = 'brightens'
        assert not bad, f'{name} colormap {problem} at {bad[:3]}'


def test_nord_colormap_runs_from_night_to_aurora():
    cmap = STYLES['nord']['colormap']
    assert cmap[0] == (0x2E, 0x34, 0x40)    # nord0
    assert cmap[-1] == (0xFF, 0xE8, 0x93)   # bright aurora yellow


def test_nord_light_keeps_text_readable_outdoors():
    style = STYLES['nord_light']
    pairs = (
        ('fg', 'bg', 7.0),
        ('header_fg', 'header_bg', 7.0),
        ('dim', 'bg', 7.0),
        ('dim', 'header_bg', 4.5),
        ('plot_fg', 'plot_bg', 7.0),
        ('accent', 'bg', 4.5),
        ('accent', 'header_bg', 4.5),
        ('warn', 'bg', 4.5),
        ('warn', 'header_bg', 4.5),
        ('input_fg', 'input_bg', 7.0),
        ('input_active', 'input_bg', 4.5),
    )
    for fg, bg, target in pairs:
        ratio = _contrast_ratio(style[fg], style[bg])
        assert ratio >= target, f'{fg} on {bg} is {ratio:.2f}:1, need {target}'


def test_light_colormap_peak_stands_out_on_its_plot_surface():
    style = STYLES['nord_light']
    ratio = _contrast_ratio(style['colormap'][-1], style['plot_bg'])
    assert ratio >= 7.0, f'light colormap peak is only {ratio:.2f}:1'


def test_nord_light_colormap_runs_from_snow_to_dark_aurora():
    cmap = STYLES['nord_light']['colormap']
    assert cmap[0] == (0xE5, 0xE9, 0xF0)    # nord5, matches plot_bg
    assert cmap[-1] == (0x69, 0x35, 0x3A)   # dark aurora red


def test_saved_theme_is_restored(tmp_path):
    """A theme choice must survive a restart."""
    import json
    from shesha.tui.config import Config
    from shesha.tui.shell import Shell
    from shesha.tui.dummy import DummyDaemon
    from shesha.tui.status import StatusApp

    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'theme': 'tokyonight'}))
    config = Config(str(path))
    shell = Shell(apps=[StatusApp()], config=config,
                  daemon=DummyDaemon(config))
    assert shell.style is STYLES['tokyonight']
