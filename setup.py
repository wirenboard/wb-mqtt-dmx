#!/usr/bin/env python3

from setuptools import find_namespace_packages, setup

setup(
    name="wb-mqtt-dmx",
    version="0.1.0",
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="DMX512 output from a Wiren Board controller serial port",
    license="MIT",
    packages=find_namespace_packages(include=["wb.*"]),
)
