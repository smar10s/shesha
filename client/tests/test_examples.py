import importlib.util
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _load(name: str):
    path = EXAMPLES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"example_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_iq_rx_example_imports():
    assert callable(_load("iq_rx").main)
