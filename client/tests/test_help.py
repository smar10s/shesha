"""Help overlay: everything derives from live shell state."""
import os
import tempfile

from shesha.tui.app import App, View
from shesha.tui.command_model import Command
from shesha.tui.config import Config, KeySpec
from shesha.tui.dummy import DummyDaemon
from shesha.tui.help import HelpView
from shesha.tui.registry import APP_CLASSES
from shesha.tui.shell import Shell


def _shell(apps=None):
    f = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    f.write('{}')
    f.close()
    config = Config(f.name)
    apps = apps or [cls() for cls in APP_CLASSES]
    for app in apps:
        config.register_keys(app.config_keys())
    daemon = DummyDaemon(config)
    return Shell(apps=apps, config=config, daemon=daemon), f.name


def _text(view):
    return '\n'.join(view._lines)


class _NamedView(View):
    def __init__(self, name):
        super().__init__()
        self.name = name

    def command_hints(self):
        return [('start', 'Start')]


class _NamedApp(App):
    def __init__(self, name, views=()):
        super().__init__()
        self.name = name
        self.views = {view: _NamedView(view) for view in views}


def test_help_commands_come_from_the_command_tree():
    """Every described node appears, including nested ones; no hardcoded list."""
    root = Command('')
    root.add(
        Command('help', description='Show this help'),
        Command('quit', aliases=('q',), description='Exit'),
        Command('theme', usage='<name>', description='Switch theme'),
        Command('daemon', description='Daemon control').add(
            Command('sync', description='Sync config to daemon')),
        Command('freq', usage='<MHz>', description='Set center frequency'),
    )
    app = Command('wifi', description='wifi scanning and traffic')
    app.add(Command('scan', description='Open the scan view'),
            Command('record', usage='[file]', description='Record frames')
                   .add(Command('stop', description='Stop recording')))
    root.add(app)

    view = HelpView({}, {'wifi': _NamedApp('wifi', views=('scan',))})
    view.build_content('wifi', 'scan', global_keys=(),
                       global_commands=root, app_commands={'wifi': app})
    text = _text(view)

    for expected in (':help', ':quit / :q', ':theme <name>',
                     ':daemon', ':daemon sync', ':freq <MHz>',
                     ':wifi record [file]', ':wifi record stop'):
        assert expected in text, expected
    assert 'Builtins' not in text
    assert ':stop_tx' not in text


def test_help_lists_local_before_global():
    root = Command('')
    root.add(Command('help', description='Show this help'))
    app = Command('wifi', description='wifi')
    app.add(Command('scan', description='Open the scan view'),
            Command('record', description='Record frames'))
    root.add(app)

    schema = {'freq': KeySpec('int', default=1),
              'wifi.scan.dwell': KeySpec('float', default=2.0)}
    view = HelpView(schema, {'wifi': _NamedApp('wifi', views=('scan',))})
    view.build_content('wifi', 'scan', global_keys=(),
                       global_commands=root, app_commands={'wifi': app})
    lines = view._lines

    assert lines.index('    wifi - scan') < lines.index('    Global')
    assert lines.index('    wifi') < lines.index('    Global')
    local = next(i for i, line in enumerate(lines) if 'wifi.scan.dwell' in line)
    global_row = next(i for i, line in enumerate(lines) if line.strip().startswith('freq '))
    assert local < global_row


def test_help_combines_keys_that_share_an_action():
    class KeysView(View):
        name = 'test'

        def _key_defs(self):
            return [
                ('j', 'Select next', lambda c: None),
                ('down', 'Select next', lambda c: None),
                ('k', 'Select previous', lambda c: None),
            ]

    class KeysApp(App):
        name = 'fake'
        default_view = 'test'

        def __init__(self):
            super().__init__()
            self.views = {'test': KeysView()}

    view = HelpView({}, {'fake': KeysApp()})
    view.build_content('fake', 'test', global_keys=(), global_commands=Command(''),
                       app_commands={'fake': Command('fake')})
    text = _text(view)

    assert 'j / \u2193' in text
    assert 'Select next' in text
    assert 'k' in text
    assert text.count('Select next') == 1


def test_help_config_columns_line_up_per_section():
    schema = {
        'freq': KeySpec('int', default=5180000000),
        'viz.waterfall.fps': KeySpec('int', default=20),
        'viz.waterfall.window': KeySpec('window_type', default='hann'),
        'viz.waterfall.avg_value': KeySpec('int', default=8),
    }
    view = HelpView(schema, {'viz': _NamedApp('viz', views=('waterfall',))})
    view.build_content('viz', 'waterfall', global_keys=(),
                       global_commands=Command(''), app_commands={})

    columns = {line.index('default=') for line in view._lines
               if 'default=' in line}
    assert len(columns) == 1


def test_help_fits_eighty_columns_and_combines_global_arrows():
    shell, path = _shell()
    try:
        shell._help_view.build_content(
            app_name='wifi', view_name='scan',
            global_keys=shell._global_keys,
            global_commands=shell._commands.root,
            app_commands=shell._app_commands)
        lines = shell._help_view._lines
        assert max(len(line) for line in lines) <= 80
        text = '\n'.join(lines)
        assert 'h / \u2190' in text
        assert 'l / \u2192' in text
        assert ':wifi scan start' in text
        assert ':wifi record stop' in text
    finally:
        os.unlink(path)


def test_help_lists_wifi_tx_frame_forms():
    shell, path = _shell()
    try:
        shell._help_view.build_content(
            app_name='wifi', view_name='scan',
            global_keys=shell._global_keys,
            global_commands=shell._commands.root,
            app_commands=shell._app_commands)
        lines = shell._help_view._lines
        text = '\n'.join(lines)
        for row in (':wifi tx beacon <ssid> [opts]',
                    ':wifi tx frame <beacon|deauth|raw> ...',
                    ':wifi tx frame beacon [<ssid>] [opts]',
                    ':wifi tx frame deauth <da> <bssid> [opts]',
                    ':wifi tx frame raw <hex> [opts]'):
            assert row in text, row
        assert max(len(line) for line in lines) <= 80
    finally:
        os.unlink(path)


def test_every_command_node_has_a_description():
    shell, path = _shell()
    try:
        def walk(node):
            for child in node.children.values():
                assert child.description, child.name
                walk(child)

        walk(shell._commands.root)
    finally:
        os.unlink(path)


def test_fft_size_shortcut_targets_the_waterfall():
    shell, path = _shell()
    try:
        shell._exec_command_inner('fft_size 1024')
        assert shell.config.get('viz.waterfall.fft_size') == '1024'
    finally:
        os.unlink(path)
