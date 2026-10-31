#!/bin/sh
# binwalk 2.3.x imports capstone 4/5 APIs (CS_ARCH_ARM64). The sandbox
# PYTHONPATH exposes pwntools' capstone 6, where those names were renamed, so
# clear PYTHONPATH and use Debian's python3-capstone for this entry point only.
exec env -u PYTHONPATH /usr/bin/python3 /usr/bin/binwalk "$@"
