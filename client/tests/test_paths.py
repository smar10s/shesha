"""Config location resolution."""
import os

from shesha.paths import config_dir, default_config_path


def test_config_dir_falls_back_to_home_dot_config(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert config_dir() == os.path.join(str(tmp_path), ".config", "shesha")


def test_config_dir_honours_absolute_xdg_override(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert config_dir() == os.path.join(str(tmp_path / "xdg"), "shesha")


def test_config_dir_ignores_relative_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert config_dir() == os.path.join(str(tmp_path), ".config", "shesha")


def test_config_dir_ignores_empty_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert config_dir() == os.path.join(str(tmp_path), ".config", "shesha")


def test_config_dir_shesha_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SHESHA_CONFIG_DIR", str(tmp_path / "custom"))

    assert config_dir() == str(tmp_path / "custom")


def test_default_config_path_is_config_json_in_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SHESHA_CONFIG_DIR", str(tmp_path))

    assert default_config_path() == os.path.join(str(tmp_path), "config.json")
