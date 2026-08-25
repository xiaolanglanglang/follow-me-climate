"""Test bootstrap: load the integration's HA-free modules standalone.

Only const.py and controller.py are loaded (via a synthetic package so their
relative imports resolve), which keeps the control-loop unit tests free of
any homeassistant dependency.
"""

import importlib.util
import sys
import types
from pathlib import Path

BASE = Path(__file__).parent / "custom_components" / "follow_me_climate"

_package = types.ModuleType("follow_me_climate_under_test")
_package.__path__ = [str(BASE)]
sys.modules.setdefault("follow_me_climate_under_test", _package)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"follow_me_climate_under_test.{name}", BASE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


const = _load("const")
controller = _load("controller")

FollowMeController = controller.FollowMeController
ControllerConfig = controller.ControllerConfig
