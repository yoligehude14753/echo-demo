-- 001_initial.sql
-- 把 P2.4 之前 sqlite.py 里的 inline DDL 全量迁出来，作为 baseline schema。
-- 所有语句 IF NOT EXISTS / IF NOT EXISTS 包裹，存量库重跑也无副作用。

CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT,
    state TEXT NOT NULL CHECK(state IN ('in_meeting','ended','finalized')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    finalized_at TEXT,
    auto_started INTEGER NOT NULL DEFAULT 0,
    minutes_json TEXT,
    raw_transcript_ref TEXT
);

CREATE TABLE IF NOT EXISTS meeting_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    text TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    speaker_id TEXT,
    speaker_label TEXT,
    captured_at TEXT NOT NULL,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_meeting_segments_meeting
    ON meeting_segments(meeting_id, start_ms);

CREATE TABLE IF NOT EXISTS meeting_speaker_labels (
    meeting_id TEXT NOT NULL,
    speaker_id TEXT NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (meeting_id, speaker_id),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ambient_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audio_ref TEXT NOT NULL,
    text TEXT NOT NULL,
    speaker_id TEXT,
    speaker_label TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ambient_segments_captured
    ON ambient_segments(captured_at);
CREATE INDEX IF NOT EXISTS idx_ambient_segments_speaker
    ON ambient_segments(speaker_id);

CREATE TABLE IF NOT EXISTS speakers (
    speaker_id TEXT PRIMARY KEY,
    label TEXT,
    n_samples INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    embedding_blob BLOB
);
