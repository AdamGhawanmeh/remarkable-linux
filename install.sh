#!/bin/sh
# Builds and installs the unofficial reMarkable Linux client: finds Python 3 and runs
# install.py with every argument given here. See README.md, or ./install.sh --help.
#
#   ./install.sh               asks before each download and install
#   ./install.sh --yes         no questions (automated runs)
#
# PYTHON=/path/to/python3 ./install.sh  picks the interpreter.
set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

usable() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

python=${PYTHON:-}
if [ -n "$python" ]; then
    if ! usable "$python"; then
        echo "install.sh: PYTHON=$python is not Python 3.10 or newer" >&2
        exit 1
    fi
else
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
        if command -v "$candidate" >/dev/null 2>&1 && usable "$candidate"; then
            python=$(command -v "$candidate")
            break
        fi
    done
fi

if [ -z "$python" ]; then
    cat >&2 <<'EOF'
install.sh: Python 3.10 or newer is needed. Install it, then run ./install.sh again:
    Fedora:          sudo dnf install python3
    Debian, Ubuntu:  sudo apt install python3 python3-venv
    Arch:            sudo pacman -S python
EOF
    exit 1
fi

exec "$python" "$here/install.py" "$@"
