#!/usr/bin/env bash
# VoiceFlock native setup for macOS / Linux:  bash setup.sh   then   ./voiceflock.sh
set -euo pipefail
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || { echo "ffmpeg missing: brew install ffmpeg  /  sudo apt install ffmpeg"; exit 1; }
PY=$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)
[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
pip install --upgrade pip wheel >/dev/null
if command -v nvidia-smi >/dev/null 2>&1; then pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
else pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu; fi
pip install -e .
[ -f .env ] || { cp .env.example .env; echo "Edit .env and set HF_TOKEN (and VOICEFLOCK_PASSWORD)."; }
cat > voiceflock.sh <<'EOS'
#!/usr/bin/env bash
cd "$(dirname "$0")"; set -a; [ -f .env ] && . ./.env; set +a
exec .venv/bin/voiceflock serve --host 0.0.0.0 --port "${VOICEFLOCK_PORT:-8787}"
EOS
chmod +x voiceflock.sh
echo "Done. Start with ./voiceflock.sh and open http://localhost:8787"
