# VoiceFlock

Open-set speaker identification with a web UI. Upload (or link to) audio or video, and VoiceFlock
diarizes it, makes a voiceprint of **every** speaker, and matches those prints against a gallery
you build over time.

Three ways to use it, plus feed watching so new episodes scan themselves:

| Mode | What it does |
|---|---|
| **Bulk Scan** | Drop many files. Every speaker is printed. Known people raise an alert ("we think *X* is in this file at 12:34, check against *that* file"). New voices are saved as *unnamed voices*, so if the same voice shows up in a later upload you're told where it was first heard. |
| **Build Database** | Listen to the unnamed voices, name them, merge duplicates, or enroll a known person directly from a clip. |
| **VoiceFlock Match** | Upload one recording and get, per speaker, the top 5 candidates with calibrated probability, margin over #2, and a clip-quality score. Confirm / Reject feed the gallery. Incognito mode matches without saving anything. |

**Watched feeds**: add a podcast RSS or YouTube channel feed and new episodes are downloaded and scanned
automatically. **Retroactive alerts**: naming a voice or enrolling someone re-scores every earlier recording
in seconds, so "Alice was also in that file from last month" shows up without reprocessing.

Pipeline: `ffmpeg` → **pyannote** speaker diarization → **SpeechBrain ECAPA-TDNN** embeddings per
speaker (several per person) → **FAISS** cosine search → **adaptive s-norm** against a cohort →
**logistic-regression calibration** to a probability → open-set *unknown* below an editable threshold.

## Quick start

### 1. HuggingFace token (needed for diarization)

The pyannote models are gated. One time only:

1. Create a free account and a read token at <https://huggingface.co/settings/tokens>.
2. With the same account, click *Agree* on
   <https://huggingface.co/pyannote/speaker-diarization-community-1> and
   <https://huggingface.co/pyannote/segmentation-3.0>
   (and <https://huggingface.co/pyannote/speaker-diarization-3.1> if you switch to that pipeline in Settings).
3. Paste the token in **Settings → HuggingFace token** (or set `HF_TOKEN` in `.env`).

Everything else (SpeechBrain ECAPA) downloads automatically on first run.

### 2a. Run with Docker (recommended, works on the always-on laptop)

```bash
git clone https://github.com/Reconnaishawnce/VoiceFlock && cd VoiceFlock
cp .env.example .env            # put your HF_TOKEN in here
docker compose up -d --build    # first build downloads torch, ~5 min
# open http://localhost:8787  (or http://<laptop-ip>:8787 from your phone)
```

Data (SQLite DB, converted audio, clips, model cache) lives in `./data`. Back that folder up and you
have everything. Then open **Settings → Run self-test**: it loads both models on synthetic audio and tells you
exactly what is wrong if the token or model terms aren't right.

If the laptop is reachable from other devices (LAN, Tailscale), set `VOICEFLOCK_PASSWORD=...` in `.env`
and the UI/API ask for it (HTTP basic auth, any username).

NVIDIA GPU: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build`.

### 2b. Run natively on Windows (no Docker)

1. Install Git (https://git-scm.com) if you don't have it, then in PowerShell:
   `git clone https://github.com/Reconnaishawnce/VoiceFlock` and `cd VoiceFlock`
2. Right-click `setup.ps1` → **Run with PowerShell** (or `powershell -ExecutionPolicy Bypass -File setup.ps1`).
   It installs Python 3.11 and ffmpeg through winget if they're missing, creates `.venv`, installs PyTorch
   (CUDA build if an NVIDIA GPU is present) and VoiceFlock, asks for your HF token and a UI password, and can
   register itself to start at login.
3. Double-click `VoiceFlock.bat`. It starts the server and opens http://localhost:8787. Closing the window
   stops the server.

No Developer Mode / symlink permission is needed: on Windows the models are copied instead of linked.

### 2c. Run natively on macOS / Linux

```bash
bash setup.sh        # venv + torch + ffmpeg check + .env
./voiceflock.sh      # http://localhost:8787
```

## How matching works

1. **Diarization** splits the recording into speaker turns (pyannote). Turns per speaker are merged,
   speakers with less than *min speech* are dropped.
2. **Embeddings**: each turn is cut into ≤ 8 s chunks and embedded with ECAPA-TDNN. A speaker's print
   is the duration-weighted mean plus the best chunk embeddings (so a person has *many* prints).
3. **Quality score** (0-100) from speech duration (saturates at 30 s), SNR (speaker's speech energy vs the
   recording's noise floor, 5-25 dB window) and clipping.
4. **Search**: FAISS inner-product over all prints, best print per person.
5. **Adaptive s-norm**: raw cosine is normalised with the top-k cohort statistics of both the probe (z-norm)
   and the print (t-norm). The cohort is the explicit set added by `calibrate`/`cohort` plus (by default)
   one mean embedding per person in the gallery. Below *min cohort size* raw cosine is used instead.
6. **Calibration**: `p = sigmoid(a·(score + b))`. Defaults are sensible for ECAPA; run `calibrate` to fit
   them to your audio.
7. **Decision** (per speaker, thresholds in Settings):
   * top candidate is a *known* person and `p ≥ match threshold` → **match alert** (prints are added only when you Confirm);
   * top candidate is an *unnamed voice* and `p ≥ clustering threshold` → attach to it (its prints grow);
   * otherwise, if not incognito and enough speech → new **Unknown Voice N**;
   * incognito → nothing is written to the gallery.

Confirm adds the clip's embeddings to the person with full provenance; Reject or Unassign removes them
again. The UI reminds you to confirm only above the *high-confidence threshold* (default 90%).
"Forget voices from this file" undoes everything a recording added; deleting a recording removes its
audio, clips and prints.

**Re-matching.** Every recording-speaker keeps its embedding, so after you name an unnamed voice, enroll
someone, confirm a match or import a gallery, a *rematch* job re-scores all earlier recordings against the
new gallery (no ML, seconds). Confirmed, rejected and named decisions are never overridden. You can also
trigger it with **Sources → Re-match all** or `voiceflock rematch`.

**Duplicates.** Re-uploading a file you already processed (same bytes, any filename) or the same URL is
detected and points you at the existing recording instead of processing it twice.

**Timeline.** Each recording shows a colour-coded speaker timeline; click any block or timestamp chip to
jump the player there, so "check at 12:34" is one click.

## Command line

```bash
voiceflock serve [--port 8787] [--no-worker]     # web UI + API + background worker
voiceflock worker                                 # extra worker process (same ./data)
voiceflock ingest FILES|DIRS|URLS [--mode bulk|match|enroll --person NAME] [--incognito] [--run]
voiceflock enroll "Jane Doe" clip1.wav clip2.m4a  # single-speaker clips, no diarization
voiceflock calibrate ./labeled                    # fit calibration + cohort from labelled clips
voiceflock evaluate ./labeled [--out eval_out] [--enroll-per-speaker 1] [--against-gallery]
voiceflock cohort ./impostor_clips [--replace]    # add impostor embeddings for s-norm
voiceflock rematch [SOURCE_ID ...]                # re-score recordings against the current gallery (no ML)
voiceflock selftest                               # load both models on synthetic audio, print timings / errors
voiceflock feeds add URL [--mode bulk|match] [--interval 60] [--backfill 3] [--poll]
voiceflock feeds [list|poll|remove ID]
voiceflock export gallery|matches OUT             # gallery JSON (people + prints) / rank-1 matches CSV
voiceflock import gallery.json                    # merge an export into this database (by person name)
voiceflock persons                                # list people / unnamed voices
voiceflock settings [--set match_threshold=0.8 hf_token=hf_...]
```

### Feeds

Podcast RSS (`<enclosure>`), Media RSS and Atom are supported. For a YouTube channel use
`https://www.youtube.com/feeds/videos.xml?channel_id=UC...` (entries are fetched with yt-dlp).
`--backfill N` grabs the N newest existing episodes on the first poll and ignores the rest; after that only
new episodes are queued (behind manual uploads in the queue).

### Labelled folder format (calibrate / evaluate)

```
labeled/
  Jane Doe/   clip1.wav  clip2.mp3 ...
  John Roe/   a.m4a      b.wav ...
  pairs.csv   (optional: file1,file2,label   label 1 = same speaker)
```

Clips should be single-speaker. Without `pairs.csv` all cross pairs are used.

`calibrate` fits raw and s-norm calibrations with logistic regression, stores them, adds one cohort
embedding per labelled speaker, and prints EER, miss rate at 1% false alarm and the calibrated
probability at that operating point (a good value for the match threshold).

`evaluate` reports **rank-1 accuracy** (enrol the first clip per speaker, probe the rest; or
`--against-gallery` to probe your live gallery), **EER**, **miss rate at 1 % false alarm**, minDCF,
and writes `det.csv` + `det.png` (DET curve) and `identification.csv`.

## API

The UI is a thin client over a JSON API (`/docs` for Swagger):
`POST /api/upload`, `POST /api/upload_url`, `GET /api/sources`, `GET /api/sources/{id}`,
`POST /api/sources/{id}/forget`, `DELETE /api/sources/{id}`, `GET /api/jobs`, `GET /api/alerts`,
`GET /api/voices/unnamed`, `GET|POST|PATCH|DELETE /api/persons`, `POST /api/persons/{id}/merge`,
`POST /api/speakers/{id}/name`, `POST /api/speakers/{id}/unassign`,
`POST /api/matches/{id}/confirm|reject`, `GET|PUT /api/settings`, `POST /api/rematch`, `POST|GET /api/selftest`,
`GET|POST|PATCH|DELETE /api/feeds`, `POST /api/feeds/{id}/poll`, `GET /api/export/gallery.json`,
`GET /api/export/matches.csv`, `POST /api/import/gallery`.

## Layout

```
voiceflock/
  api.py        FastAPI routes               pipeline.py  diarize → embed → quality → match → decide
  worker.py     SQLite job queue             gallery.py   FAISS search, s-norm, calibration, prints
  diarize.py    pyannote wrapper             embed.py     SpeechBrain ECAPA wrapper
  audio.py      ffmpeg / yt-dlp / SNR        calibrate.py evaluate.py metrics.py labeled.py
  rematch.py    retroactive re-scoring       feeds.py     RSS/Atom watcher (polled by the worker)
  selftest.py   model smoke test             transfer.py  gallery export/import, matches CSV
  config.py     settings (DB + env)          schema.sql   static/ (UI)
tests/          pytest: unit tests need no models; `-m slow` runs the end-to-end test with the real ECAPA model
```

## Notes & limits

* CPU speed: roughly 5-10× real time for diarization on a laptop CPU (a 1 h podcast ≈ 10-15 min).
  A GPU brings that to about a minute. Jobs run one at a time per worker; start more with `voiceflock worker`.
* Long files are processed whole (3 h ≈ 0.7 GB RAM for the waveform). Split anything longer than ~4 h.
* URLs use `yt-dlp` (YouTube, most podcast pages, direct media links).
* Performance numbers depend on your audio. Run `calibrate`/`evaluate` on clips that look like your real
  use (same mics, rooms, phone lines) before trusting the probabilities.
* This identifies voices in recordings you choose to upload. Use it lawfully and with the consent that
  applies where you are.
