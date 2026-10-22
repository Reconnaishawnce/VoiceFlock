#!/usr/bin/env bash
cd "$(dirname "$0")" && git pull && .venv/bin/pip install -q -e . && echo "Updated. Restart ./voiceflock.sh"
