#!/usr/bin/env bash
cd "$(dirname "$0")" && git pull && .venv/bin/pip install -q -e . && .venv/bin/pip install -q -U yt-dlp && echo "Updated. Restart ./voiceflock.sh"
