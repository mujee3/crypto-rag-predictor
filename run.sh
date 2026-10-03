#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
crypto_python=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info >= (3,11)' 2>/dev/null; then
    crypto_python="$candidate"
    break
  fi
done
if [[ -z "$crypto_python" ]]; then
  echo 'Python 3.11+ is required.' >&2
  exit 1
fi
if [[ ! -x .numeric_venv/bin/python ]]; then "$crypto_python" -m venv .numeric_venv; fi
if ! .numeric_venv/bin/python -c 'import numpy,pandas,certifi,openai,pydantic; assert tuple(map(int,openai.__version__.split(".")[:2])) >= (2,54); assert int(pydantic.__version__.split(".")[0]) == 2' >/dev/null 2>&1; then
  .numeric_venv/bin/python -m pip install -r requirements.txt
fi
exec .numeric_venv/bin/python run.py "$@"
