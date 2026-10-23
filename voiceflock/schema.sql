PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- An uploaded file or downloaded URL.
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  original_filename TEXT,
  url TEXT,
  orig_path TEXT,
  wav_path TEXT,
  sha256 TEXT,
  duration_s REAL,
  mode TEXT NOT NULL DEFAULT 'bulk',      -- bulk | match | enroll
  incognito INTEGER NOT NULL DEFAULT 0,
  enroll_person_id INTEGER,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued | processing | done | failed | deleted
  error TEXT,
  n_speakers INTEGER,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  processed_at TEXT,
  meta TEXT,
  feed_id INTEGER,
  review INTEGER NOT NULL DEFAULT 0,        -- 1 = the user wants to label the voices in this recording
  notes TEXT,
  stills_info TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,                      -- process_source
  source_id INTEGER,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed | cancelled
  priority INTEGER NOT NULL DEFAULT 0,
  stage TEXT,
  progress REAL NOT NULL DEFAULT 0,
  log TEXT,
  error TEXT,
  worker TEXT,
  result TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  started_at TEXT,
  finished_at TEXT,
  heartbeat_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, priority DESC, id);

-- An identity. kind='named' once you name it; 'provisional' = "Unknown Voice N".
CREATE TABLE IF NOT EXISTS persons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'provisional',
  alert INTEGER NOT NULL DEFAULT 0,          -- 1 = alert me when this voice is heard (Live / phone)
  alert_sound TEXT,                          -- siren | klaxon | chime | none | NULL = default
  alert_message TEXT,                        -- spoken on alert; [NAME] is replaced; NULL = default template
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT
);

-- One diarized speaker within one source.
CREATE TABLE IF NOT EXISTS source_speakers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  speech_seconds REAL NOT NULL DEFAULT 0,
  n_segments INTEGER NOT NULL DEFAULT 0,
  first_start_s REAL,
  quality_score REAL,
  snr_db REAL,
  clip_fraction REAL,
  clip_path TEXT,
  stills TEXT,                             -- JSON [{t, file}] low-res video frames for this speaker
  review_status TEXT,                      -- pending | done | skipped | NULL
  notes TEXT,
  ignored INTEGER NOT NULL DEFAULT 0,      -- 1 = not a voice (noise, laughter, crosstalk): never matched, never a print
  embedding BLOB,
  person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
  assignment TEXT,                         -- auto | confirmed | enrolled | named | NULL
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ss_source ON source_speakers(source_id);
CREATE INDEX IF NOT EXISTS ss_person ON source_speakers(person_id);

CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_speaker_id INTEGER NOT NULL REFERENCES source_speakers(id) ON DELETE CASCADE,
  start_s REAL NOT NULL,
  end_s REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS seg_ss ON segments(source_speaker_id);

-- Gallery: multiple embeddings per person with provenance.
CREATE TABLE IF NOT EXISTS prints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  source_id INTEGER,
  source_speaker_id INTEGER REFERENCES source_speakers(id) ON DELETE CASCADE,
  embedding BLOB NOT NULL,
  is_mean INTEGER NOT NULL DEFAULT 0,
  speech_seconds REAL,
  quality REAL,
  origin TEXT NOT NULL,                    -- enroll | confirm | named | auto-cluster | cli
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS prints_person ON prints(person_id);
CREATE INDEX IF NOT EXISTS prints_ss ON prints(source_speaker_id);

CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_speaker_id INTEGER NOT NULL REFERENCES source_speakers(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL,
  raw_score REAL NOT NULL,
  norm_score REAL,
  probability REAL NOT NULL,
  margin REAL,
  score_mode TEXT,                         -- raw | snorm
  status TEXT NOT NULL DEFAULT 'open',     -- open | auto | confirmed | rejected
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS matches_ss ON matches(source_speaker_id);
CREATE INDEX IF NOT EXISTS matches_person ON matches(person_id);

-- Explicit cohort (impostor) embeddings for adaptive s-norm.
CREATE TABLE IF NOT EXISTS cohort (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT,
  embedding BLOB NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS calibrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL,                      -- raw | snorm
  a REAL NOT NULL,
  b REAL NOT NULL,
  n_target INTEGER,
  n_nontarget INTEGER,
  eer REAL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- RSS/Atom feeds polled by the worker; new episodes are queued automatically.
CREATE TABLE IF NOT EXISTS feeds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL UNIQUE,
  title TEXT,
  mode TEXT NOT NULL DEFAULT 'bulk',
  incognito INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  interval_min INTEGER NOT NULL DEFAULT 60,
  backfill INTEGER NOT NULL DEFAULT 3,      -- how many existing items to grab on first poll
  last_checked_at TEXT,
  last_error TEXT,
  n_items INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS feed_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
  guid TEXT NOT NULL,
  title TEXT,
  url TEXT,
  published TEXT,
  source_id INTEGER,
  skipped INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(feed_id, guid)
);

-- Packages: groups of voices with bulk alert settings (e.g. "Politicians").
CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  alert INTEGER NOT NULL DEFAULT 0,
  alert_sound TEXT,
  alert_message TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS person_groups (
  person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  PRIMARY KEY (person_id, group_id)
);
