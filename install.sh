#!/usr/bin/env sh
# radiolog installer for Linux / macOS.  Usage: ./install.sh  (or ./install.sh --uninstall)
set -e
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else
  echo "Python 3 was not found. Install it with your package manager (e.g. sudo apt install python3)."
  exit 1
fi
exec "$PY" install.py "$@"
