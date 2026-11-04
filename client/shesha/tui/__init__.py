"""Shesha TUI — vim-modal terminal interface for PlutoSDR."""
import argparse
import os

from ..paths import config_dir, default_config_path
from .config import Config
from .daemon import SheshaDaemon
from .dummy import DummyDaemon
from .shell import Shell
from .registry import APP_CLASSES


def main(argv=None):
    parser = argparse.ArgumentParser(description='Shesha TUI')
    parser.add_argument('--offline', action='store_true',
                        help='Start with dummy daemon (no hardware)')
    parser.add_argument('--host', default='192.168.2.1',
                        help='Pluto IP address')
    parser.add_argument('--port', type=int, default=5555,
                        help='Daemon TCP port')
    args = parser.parse_args(argv)

    # Project root: client/shesha/tui/__init__.py -> four levels up
    project_dir = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

    os.makedirs(config_dir(), exist_ok=True)
    config = Config(default_config_path())

    if args.offline:
        daemon = DummyDaemon(config)
    else:
        daemon = SheshaDaemon(
            config, host=args.host, port=args.port,
            project_dir=project_dir)

    apps = [cls() for cls in APP_CLASSES]
    for app in apps:
        config.register_keys(app.config_keys())

    # Seed a freshly started daemon with the saved view's mode BEFORE
    # connect.  Reconnecting to a running daemon adopts its state instead —
    # config is never pushed into a live daemon.
    saved_view = config.get('app.view')
    app_map = {a.name: a for a in apps}
    if saved_view:
        parts = saved_view.split(':', 1)
        app = app_map.get(parts[0])
        if app is not None:
            name = (parts[1] if len(parts) > 1 and parts[1] in app.views
                    else app.default_view)
            view = app.views.get(name)
            if view is not None and view.mode is not None:
                daemon.set_mode(view.mode)

    if not args.offline:
        daemon.connect()

    shell = Shell(
        apps=apps,
        config=config,
        daemon=daemon,
        project_dir=project_dir,
    )

    try:
        shell.run()
    except KeyboardInterrupt:
        shell.shutdown()


if __name__ == '__main__':
    main()
