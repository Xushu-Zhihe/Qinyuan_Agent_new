#!/bin/sh
# pwntools looks for `pwntools-terminal` before tmux/xterm when it needs a
# terminal for gdb. The sandbox has no display and mounts /tmp noexec, so this
# helper allocates a pty with script(1). pwntools may pass either a command
# string or a generated launcher script; the latter is executed through python3
# because /tmp cannot be exec'd directly.
set -eu
if [ "$#" -ne 1 ]; then
    echo "usage: pwntools-terminal <command-or-launcher>" >&2
    exit 2
fi
if [ -f "$1" ] && [ -r "$1" ]; then
    exec /usr/bin/script -qefc "exec python3 $1" /dev/null
fi
exec /usr/bin/script -qefc "$1" /dev/null
