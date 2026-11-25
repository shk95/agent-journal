"""Create a consistent SQLite snapshot while the journal is serving."""

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def backup(database, output):
    source = Path(database).resolve()
    target = Path(output).resolve()
    if target.exists() or not source.is_file():
        raise ValueError("Choose an existing database and a new backup filename")
    with tempfile.TemporaryDirectory(
        prefix=".journal-backup-", dir=target.parent
    ) as folder:
        staged = Path(folder) / "journal.sqlite3"
        staged.touch(mode=0o600)
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live,
            closing(sqlite3.connect(staged)) as copy,
        ):
            live.backup(copy)
            if copy.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Backup failed the SQLite consistency check")
            copy.execute("PRAGMA journal_mode=DELETE")
        os.link(staged, target)
