#!/usr/bin/env python3

from setuptools import find_namespace_packages, find_packages, setup

from wb.mqtt_dmx.version import get_version_from_changelog

# wb-mqtt-python-lib is a git submodule in the repository root. It is installed inside the package,
# so every service carries the version of the library it was tested with.
LIB_PACKAGE = "wb.mqtt_dmx.wb_mqtt_lib"
LIB_DIR = "wb-mqtt-python-lib/wb_mqtt_lib"

setup(
    name="wb-mqtt-dmx",
    version=get_version_from_changelog(),
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="DMX512 output from a Wiren Board controller serial port",
    url="https://github.com/wirenboard/wb-mqtt-dmx",
    license="MIT",
    # Matches every subpackage of wb. "wb" itself stays out, it comes from the base package.
    packages=find_namespace_packages(include=["wb.*"])
    + [LIB_PACKAGE]
    + [f"{LIB_PACKAGE}.{name}" for name in find_packages(LIB_DIR)],
    package_dir={LIB_PACKAGE: LIB_DIR},
    # Other files (scripts, configs and etc):
    # - Installed by debian/install file
    # Requirements:
    # - Installed from debian/control file
)
