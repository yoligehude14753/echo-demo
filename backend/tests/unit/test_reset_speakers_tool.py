"""清库工具 reset_speakers 的单测（不动真 DB）。"""

from __future__ import annotations

import sqlite3

import pytest
from app.tools.reset_speakers import main


def _make_db(tmp_path) -> tuple:  # type: ignore[type-arg]
    db = tmp_path / "test.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE speakers (
          speaker_id TEXT PRIMARY KEY,
          label TEXT,
          n_samples INTEGER NOT NULL DEFAULT 0,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          embedding_blob BLOB
        );
        CREATE TABLE ambient_segments (id INTEGER PRIMARY KEY, text TEXT);
        CREATE TABLE meeting_speaker_labels (id INTEGER PRIMARY KEY, label TEXT);
        INSERT INTO speakers VALUES('speaker_1', NULL, 0, '2026-05-01', '2026-05-01', NULL);
        INSERT INTO speakers VALUES('speaker_2', NULL, 0, '2026-05-02', '2026-05-02', NULL);
        INSERT INTO ambient_segments(text) VALUES('hello'), ('world');
        INSERT INTO meeting_speaker_labels(label) VALUES('A'), ('B');
        """
    )
    con.commit()
    con.close()
    return db


@pytest.mark.unit
def test_dry_run_does_not_modify(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _make_db(tmp_path)
    rc = main(["--db", str(db)])
    assert rc == 0
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM speakers").fetchone()[0] == 2
    con.close()


@pytest.mark.unit
def test_yes_clears_speakers_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _make_db(tmp_path)
    rc = main(["--db", str(db), "--yes"])
    assert rc == 0
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM speakers").fetchone()[0] == 0
    # 默认 不动 ambient_segments / meeting_speaker_labels
    assert con.execute("SELECT COUNT(*) FROM ambient_segments").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM meeting_speaker_labels").fetchone()[0] == 2
    con.close()


@pytest.mark.unit
def test_include_segments_clears_extra_tables(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _make_db(tmp_path)
    rc = main(["--db", str(db), "--yes", "--include-segments"])
    assert rc == 0
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM speakers").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM ambient_segments").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM meeting_speaker_labels").fetchone()[0] == 0
    con.close()


@pytest.mark.unit
def test_missing_db_returns_nonzero(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rc = main(["--db", str(tmp_path / "nope.db")])
    assert rc == 1


@pytest.mark.unit
def test_missing_extra_tables_handled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """老 DB 只有 speakers 没 ambient_segments → 不要崩。"""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE speakers (speaker_id TEXT PRIMARY KEY, label TEXT, "
        "n_samples INTEGER NOT NULL DEFAULT 0, first_seen_at TEXT NOT NULL, "
        "last_seen_at TEXT NOT NULL, embedding_blob BLOB)"
    )
    con.execute(
        "INSERT INTO speakers VALUES('speaker_1', NULL, 0, '2026-05-01', '2026-05-01', NULL)"
    )
    con.commit()
    con.close()

    rc = main(["--db", str(db), "--yes", "--include-segments"])
    assert rc == 0
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM speakers").fetchone()[0] == 0
    con.close()
