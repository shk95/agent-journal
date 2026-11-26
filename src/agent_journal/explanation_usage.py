"""Local token counts for Agent Journal's separate Codex invocations."""

import json
import sqlite3
import sys
import time
from contextlib import closing
from functools import partial

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")


def token_counts(output):
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    totals = None
    for line in (output or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") not in ("turn.completed", "turn.failed"):
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        counts = tuple(usage.get(field) for field in TOKEN_FIELDS)
        if all(type(count) is int and count >= 0 for count in counts) and counts[1] <= counts[0]:
            totals = counts if totals is None else tuple(a + b for a, b in zip(totals, counts))
    return totals


def record_usage(root, purpose, quality, model, output):
    counts = token_counts(output)
    try:
        with closing(sqlite3.connect(root / "explanation-usage.sqlite3", timeout=2)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS usage(
                    recorded REAL, purpose TEXT, quality TEXT, model TEXT,
                    input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER
                )
            """)
            db.execute(
                "INSERT INTO usage VALUES(?,?,?,?,?,?,?)",
                (time.time(), purpose, quality, model or "Codex default", *(counts or (None,) * 3)),
            )
    except (OSError, sqlite3.Error) as error:
        print(f"Could not record explanation usage: {error}", file=sys.stderr)


def usage_recorder(root, purpose, quality=None, model=None):
    if root is None:
        return None
    return partial(record_usage, root, purpose, quality, model)


def explanation_usage(root, days=30):
    if not 1 <= days <= 3650:
        raise ValueError("Choose an explanation usage window of 1 to 3650 days.")
    path = root / "explanation-usage.sqlite3"
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        with db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='usage'").fetchone():
                return []
            rows = db.execute("""
                SELECT purpose, quality, model, COUNT(*) AS calls,
                       COUNT(input_tokens) AS measured_calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens
                FROM usage
                WHERE recorded >= ?
                GROUP BY purpose, quality, model
                ORDER BY purpose, quality, model
            """, (time.time() - days * 86_400,))
            return [dict(row) for row in rows]
