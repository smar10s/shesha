"""One console script: bare invocation opens the TUI, arguments hit the CLI."""
import shesha


def test_no_arguments_starts_the_tui(monkeypatch):
    calls = []
    monkeypatch.setattr("shesha.tui.main", lambda argv=None: calls.append(argv))

    shesha.main([])

    assert calls == [[]]


def test_tui_prefix_starts_the_tui_with_its_flags(monkeypatch):
    calls = []
    monkeypatch.setattr("shesha.tui.main", lambda argv=None: calls.append(argv))

    shesha.main(["tui", "--offline"])

    assert calls == [["--offline"]]


def test_arguments_go_to_the_cli(monkeypatch):
    calls = []
    monkeypatch.setattr("shesha.cli.main", lambda argv=None: calls.append(argv))

    shesha.main(["status", "--json"])

    assert calls == [["status", "--json"]]


def test_global_flags_go_to_the_cli(monkeypatch):
    calls = []
    monkeypatch.setattr("shesha.cli.main", lambda argv=None: calls.append(argv))

    shesha.main(["--host", "192.168.2.2", "status"])

    assert calls == [["--host", "192.168.2.2", "status"]]
