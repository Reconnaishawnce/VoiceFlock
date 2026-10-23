# VoiceFlock

Open-set speaker identification with a web UI. Upload (or link to) audio or video, and VoiceFlock
diarizes it, makes a voiceprint of **every** speaker, and matches those prints against a gallery
you build over time.

The workflow is **background library first, names later**: bulk-import everything, let VoiceFlock file every
speaker as an unnamed voice, then identify people whenever you like by uploading reference audio or naming
the voices you recognise. Every identification re-checks the whole library in seconds.

Three ways to use it, plus feed watching so new episodes scan themselves:

| Mode | What it does |
|---|---|
The web UI has five tabs: **Home** (alerts, stats, voice map), **Library** (add recordings, YouTube channel
import, feeds, every recording with a synced video/audio player and speaker timeline), **People** (known,
unnamed, possible duplicates; each person has a profile with every recording they were heard in), **Live**
and **Settings**. Below, "Bulk Scan", "Build Database" and "Match" describe the three ways of using it.

| **Bulk Scan** | Drop many files. Every speaker is printed. Known people raise an alert ("we think *X* is in this file at 12:34, check against *that* file"). New voices are saved as *unnamed voices*, so if the same voice shows up in a later upload you're told where it was first heard. |
| **Build Database** | Listen to the unnamed voices, name them, merge duplicates, or enroll a known person directly from a clip. |
| **Live** | The laptop microphone, scored against the whole library every second: "who is speaking right now", with a beep or browser notification when a known voice is heard. Nothing is recorded. |
| **Overview** | Stat tiles, a voice map (every voice placed by similarity, sized by speaking time), most-heard voices, recordings over time, who is heard together. |
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

**GPU.** If the laptop has an NVIDIA GPU, setup installs the CUDA build of PyTorch and the header shows the
GPU name next to "device". Diarization is then 10-30× faster than CPU (a 1 h meeting in about a minute).
If the header says `cpu` but you have an NVIDIA card, update the NVIDIA driver (any 2024+ GeForce/Studio
driver), close VoiceFlock and run `gpu.bat`. The UI shows a banner when it detects this situation.

**Parallel jobs.** `VOICEFLOCK_WORKERS=2` (or `voiceflock serve --workers 2`) processes two recordings at
once. Leave it at 1 with a GPU or a laptop CPU; 2-3 helps on a big CPU-only desktop.

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

**Keep.** An unnamed voice you don't know the name of but want tracked can be *kept*: it becomes a known
voice called "Voice N" and raises alerts like a named person. Rename it whenever you learn who it is.

**Merge suggestions.** Incremental clustering can split one person across several unnamed voices when the
mic or room changes. Build Database lists pairs that are probably the same person, with both clips, and a
one-click merge.

**Microphone.** VoiceFlock Match has a Record button (browser rule: works on the laptop itself or over HTTPS,
e.g. Tailscale; plain http://ip from a phone can't open the mic).

**Seeing who is talking.** A YouTube recording embeds the video; an uploaded video file plays the original;
audio plays the converted wav. Each speaker also gets a few low-res **stills** (≈15 KB each) cut at their
longest turns: from the file for uploaded videos, from YouTube's seek-bar storyboard sprites for YouTube
recordings (no video download). They appear on the speaker card, on unnamed-voice cards and on profiles, and
clicking one jumps the player there. Settings → *Video stills per speaker* (0 disables); *Make stills for
existing recordings* backfills older ones. Clicking a block on the timeline, a timestamp chip, or *▶ watch longest turn*
jumps the player there, so naming 25 senators from a hearing is a matter of reading the on-screen caption.

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

### YouTube channels and playlists

Bulk Scan → **Import a YouTube channel or playlist**: paste `https://www.youtube.com/@SomeCity`, a playlist, or a
`/videos` URL. The videos are listed without downloading (title, length, date when YouTube exposes it); filter
by title regex, length and date, untick what you don't want, **Queue selected**. Videos already in the library
are marked. Same from the CLI: `voiceflock youtube URL --filter "council|hearing" --min-minutes 20 --queue`.

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
  live.py       live-mode matcher            youtube.py   channel/playlist listing + import
  onnx_export.py speaker encoder -> ONNX     android/     the phone app (Kotlin, ONNX Runtime)
  config.py     settings (DB + env)          schema.sql   static/ (UI)
tests/          pytest: unit tests need no models; `-m slow` runs the end-to-end test with the real ECAPA model
```

## Phone app (Android, offline)

`android/` is a small Kotlin app that listens passively and alerts (sound, vibration, notification) when a
**flagged** voice is heard. Everything runs on the phone: the same ECAPA speaker encoder, exported to ONNX
by the laptop with feature extraction inside the graph (`voiceflock export-model`; verified to reproduce
the laptop's embeddings to cosine 1.000), plus a compact copy of the library. After one sync it needs no
network, and audio is never stored or sent anywhere.

1. **Flag voices** on the laptop: the 🔔 next to any person (people list, person page, unnamed-voice cards).
   Flagged voices also light up in the laptop's Live tab.
2. **Get the APK**: every push to `main` that touches `android/` publishes a GitHub Release with the APKs.
   With **Obtainium** add the app from `https://github.com/Reconnaishawnce/VoiceFlock` (it picks the newest
   release; choose the `arm64-v8a` asset for a modern phone, `universal` otherwise) and it will keep the app
   updated. Or download the APK from the Releases page, or open `android/` in Android Studio.
   For updates to install over an existing copy the builds must share one signing key: add the
   `ANDROID_KEYSTORE_B64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`
   repository secrets (a keystore made with `keytool -genkeypair -storetype PKCS12`); without them each
   build is signed with a throwaway key and needs an uninstall first.
3. **Get the library onto the phone.** Simplest, no network setup: laptop Settings → *Download phone
   library (.json)*, send that small file to the phone (WhatsApp, email, Drive) and open it with VoiceFlock
   (or Settings → *Import library file*). Then press *Download model* once (85 MB from GitHub). Re-send the
   library whenever you flag or name voices. Alternatively, with the phone on the same Wi-Fi or Tailscale,
   enter the laptop address in the app's Settings and press **Sync voices + model**.
4. **Listen**: tap the big microphone. The ring shows the level, the card under it shows who is speaking
   right now, and every known voice heard above the threshold lands in the **Heard** log (kept across
   restarts, shareable as CSV). Flagged voices also alarm, speak their line and post a notification, with the
   screen off. The Voices tab lets you flag/unflag on the phone (overrides the laptop's flags), filter to
   flagged voices or packages, and open any voice for its sound, spoken line and recent sightings.
5. **Identify a voice** (Listen tab): hold the phone toward whoever is speaking; after about six seconds of
   speech it shows the five closest voices. "This is them" adds the clip as a new print for that person,
   "Enroll" adds someone new (alerts on by default). Both work offline and take effect immediately; they
   reach the laptop with Settings → *Share my edits*, which now carries these enrollments too.
6. **Keep it running**: Settings → *Keep it running* asks Android for unrestricted battery use (some phones
   stop background microphones otherwise) and adds a quick-settings tile that starts and stops listening
   from the notification shade.

**Packages.** People → Packages groups voices under one alert setting ("Politicians": alert on, vault sound,
one spoken line for all 500), downloadable as a single file and importable on another VoiceFlock. A person's
own settings override their package's. The phone shows packages at the top of Voices with the same bulk
switch and editor.

**Getting the library to the phone without touching the firewall.** Settings → *Auto-export phone library
to* a folder that syncs to the cloud (Dropbox, Google Drive, OneDrive). The file is rewritten whenever voices,
packages or alerts change. Share that file's link once and paste it into the app's Settings → *Library link*;
the phone re-downloads it whenever it is opened. Edits made on the phone (flags, sounds, lines) go back with
Settings → *Share my edits* (sends text) → paste into the laptop's Settings.

**Custom alerts (the bit).** Each flagged person or package can have its own alarm sound (synthesised: siren,
two-tone horn, blast-door vault, phone-style radar, klaxon, chime) and a spoken line, set on the laptop's person page (with a Test button) or on the phone by
tapping the name in Voices. `[NAME]` is replaced with the person's name; the default template lives in
Settings → Alerts. On the phone, **Demo mode** plays the alarm and the text-to-speech line through the
alarm channel at full volume even when the phone is silent; the laptop's Live tab does the same with the
browser's speech engine.

How it works: the last 4 s of speech are embedded every second (≈100 ms of CPU on a mid-range phone, nothing during
silence), scored against every print with cosine similarity, calibrated with the laptop's calibration,
smoothed over consecutive speech windows, and alerted when a flagged person is above the match threshold
(cooldown 30 s per person). Expect roughly 5-10% battery per hour of active listening.

Check the recording-consent law where you are before listening to people continuously.

## Transcripts

Each processed recording can be transcribed on the laptop with Whisper (`faster-whisper`; large-v3-turbo on
a GPU, base on CPU, set under Settings → Transcripts). Words carry timestamps and are attributed to the
diarized voice whose turn they fall in, so the recording page shows a transcript that follows playback,
click any line to jump there, search within it, and open a voice's side panel to read what they said.
Names spoken around a turn ("the chair recognizes Senator Warren", "my name is …", "thank you, Senator X")
become **name hints**: one-click suggestions next to the name box. The Library search also searches every
transcript ("Said in recordings"). *Transcribe new recordings* defaults to `auto` = only when a GPU is
present; press **Transcribe** on any recording otherwise, or Settings → *Transcribe existing recordings*.
Raw words are cached in `data/transcripts/` so a reprocess only redoes the attribution.
Command line: `voiceflock transcribe <id|all> [--force]`.

## Calibration and noise

**Calibrate from your decisions.** Every Confirm, Name, Enroll and Reject is a labelled trial. Settings →
*Probability calibration* → **Calibrate from my decisions** scores each confirmed speaker against the library
(prints from its own recording excluded), fits the probability model to the same-voice and different-voice
score distributions, re-scores the library, and suggests a match threshold at about 1% false alarms. Needs
roughly 8 confirmed or named speakers across 2 people. Repeat whenever you have made many more decisions.
Command line: `voiceflock calibrate --from-decisions`. *Back to defaults* undoes it.

**Not a voice.** A diarized "speaker" that is laughter, applause, crosstalk or room noise: press **Not a voice**
on its card, in the side panel, in the review queue, or in the low-confidence warning. Its prints and
candidates are removed and it is never matched, never used as a print, never queued for review and never used
for calibration. The segments stay on the timeline, greyed out, with an Undo.

## Updating

Easiest: **Settings → Version & updates → Check for updates → Update and restart**. The launcher
(`VoiceFlock.bat` / `voiceflock.sh`) restarts the server for you. If the page ever shows a yellow banner
saying the server is older than the page, the code was updated without a restart: press *Restart now*.

By hand: close VoiceFlock, double-click `update.bat` (Windows) or run `./update.sh` (Mac/Linux), start it
again. Docker: `git pull && docker compose up -d --build`. Your `data` folder is never touched by updates.

**Starting over**: Settings → *Danger zone* → type `WIPE`. Deletes every recording, voice, print, package and
derived file; keeps settings (token), calibration and the downloaded models.

## Notes & limits

* CPU speed: roughly 5-10× real time for diarization on a laptop CPU (a 1 h podcast ≈ 10-15 min).
  A GPU brings that to about a minute. Jobs run one at a time per worker; start more with `voiceflock worker`.
* Long files are processed whole (3 h ≈ 0.7 GB RAM for the waveform). Split anything longer than ~4 h.
* Video files (mp4, mkv, mov, webm, ...) are fine: only the audio track is used, the video is dropped during
  conversion. A file with no audio track fails with "no audio stream found".
* A running job can be stopped from Sources & Queue; it halts at its next progress tick (seconds).
* URLs use `yt-dlp` (YouTube, most podcast pages, direct media links).
* Performance numbers depend on your audio. Run `calibrate`/`evaluate` on clips that look like your real
  use (same mics, rooms, phone lines) before trusting the probabilities.
* This identifies voices in recordings you choose to upload. Use it lawfully and with the consent that
  applies where you are.
