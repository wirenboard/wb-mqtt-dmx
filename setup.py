#!/usr/bin/env python3

from setuptools import find_namespace_packages, setup

from wb.mqtt_dmx.version import get_version_from_changelog

setup(
    name="wb-mqtt-dmx",
    version=get_version_from_changelog(),
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="DMX512 output from a Wiren Board controller serial port",
    url="https://github.com/wirenboard/wb-mqtt-dmx",
    license="MIT",
    # Matches every subpackage of wb. "wb" itself stays out, it comes from the base package.
    packages=find_namespace_packages(include=["wb.*"]),
    # Other files (scripts, configs and etc):
    # - Installed by debian/install file
    # Requirements:
    # - Installed from debian/control file
)
