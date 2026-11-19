"""
Lets the tests import wb-mqtt-python-lib from the source tree.

The library is a git submodule in the repository root. setup.py puts it inside the package as
wb.mqtt_dmx.wb_mqtt_lib, so in the build directory of the package it is simply there. In the source
tree it is not, and this file loads it under that name from the submodule.
"""

import importlib.util
import sys
from pathlib import Path

LIB_NAME = "wb.mqtt_dmx.wb_mqtt_lib"
LIB_SOURCE = Path(__file__).resolve().parent.parent / "wb-mqtt-python-lib" / "wb_mqtt_lib"


def _load_library_from_submodule() -> None:
    if importlib.util.find_spec(LIB_NAME) is not None:
        return
    spec = importlib.util.spec_from_file_location(
        LIB_NAME, LIB_SOURCE / "__init__.py", submodule_search_locations=[str(LIB_SOURCE)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"{LIB_SOURCE} is missing: run git submodule update --init")
    module = importlib.util.module_from_spec(spec)
    sys.modules[LIB_NAME] = module
    spec.loader.exec_module(module)


_load_library_from_submodule()
