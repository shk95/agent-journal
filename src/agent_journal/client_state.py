import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import Future
from pathlib import Path
from threading import Thread

from .common import JournalHTTPError, state_dir
from .text_layout import markdown_with_title


def background(function, *args):
    future = Future()

    def run():
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(function(*args))
            except Exception as error:
                future.set_exception(error)

    Thread(target=run, daemon=True).start()
    return future


def fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_state(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_state(path, value, *, durable=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            if durable:
                os.fsync(file.fileno())
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class ClientState:
    def __init__(self, api, project):
        self.api, self.project = api, project
        self.root = (
            state_dir() / "client" / fingerprint(f"{api.base}\0{api.token}\0{project}")
        )
        self.modern = None
        self.drafts = {}
        self.saves = {}
        self.recoveries = {}
        self.revision = 0
        for path in (self.root / "drafts").glob("*.json"):
            draft = read_state(path, None)
            if draft and path == self.draft_path(draft["id"]):
                draft["body"] = markdown_with_title(draft["body"], draft.pop("title", ""))
                self.drafts[draft["id"]] = draft

    def board_path(self, query):
        return (
            self.root
            / "boards"
            / (fingerprint(json.dumps(query, sort_keys=True)) + ".json")
        )

    def cached_board(self, query):
        return read_state(self.board_path(query), {})

    def has_cache(self):
        return self.board_path({}).is_file()

    def board(self, query, cached):
        params = {"project": self.project, **query}
        if cached.get("revision"):
            params["revision"] = cached["revision"]
        try:
            data = self.api.request("board", **params)
            self.modern = True
        except JournalHTTPError as error:
            if error.status != 404:
                raise
            self.modern = False
            data = self.legacy_board(query, cached)
        if data.get("unchanged"):
            return cached
        path = self.board_path(query)
        try:
            write_state(path, data)
            boards = sorted(
                path.parent.glob("*.json"), key=lambda file: file.stat().st_mtime
            )
            for old in boards[:-12]:
                old.unlink(missing_ok=True)
        except OSError:
            pass
        return data

    def legacy_board(self, query, cached):
        params = {"project": self.project, "limit": 61, "lanes": "true"}
        edge = query.get("edge")
        before = cached.get("legacy_before", query.get("before"))
        newer_pages = list(cached.get("legacy_newer_pages", []))
        notes = self.api.request("notes", project=self.project)
        batch = self.api.request("journey", **(params | ({"before": before} if before else {})))
        if edge and "legacy_before" not in cached:
            # Legacy servers expose only backwards cursors. Remember the newer pages
            # while locating the requested end so that ] can retrace them.
            targets = {(note["thread"], note["turn"]) for note in notes if note["turn"]}
            latest = batch
            note_edge = None
            while edge != "last-note" or targets:
                if edge.endswith("-note") and any(
                    (row["thread"], row["turn"]) in targets for row in batch[-60:]
                ):
                    note_edge = (before, newer_pages.copy(), batch)
                    if edge == "last-note":
                        break
                if len(batch) <= 60:
                    break
                row = batch[-60]
                cursor = json.dumps([row[key] for key in ("started", "turn", "thread")])
                if cursor == before:
                    raise RuntimeError("Legacy server did not return an older Journey page")
                newer_pages.append(before)
                before = cursor
                batch = self.api.request("journey", **(params | {"before": before}))
            if note_edge:
                before, newer_pages, batch = note_edge
            elif edge == "last-note":
                before, newer_pages, batch = None, [], latest
        turns = batch[-60:]
        keys = {(row["thread"], row["turn"]) for row in turns}
        return {
            "legacy": True,
            **({"legacy_before": before, "legacy_newer_pages": newer_pages} if edge else {}),
            "turns": turns,
            "turn_notes": [
                note for note in notes if (note["thread"], note["turn"]) in keys
            ],
            "project_notes": [note for note in notes if not note["turn"]],
            "earlier": len(batch) > 60,
            "later": before is not None,
        }

    def detail(self, note):
        if self.modern is False and "body" in note:
            return note
        try:
            return self.api.request("note", project=self.project, id=note["id"])
        except JournalHTTPError as error:
            if error.status != 404 or self.modern is not False:
                raise
            return next(
                (
                    row
                    for row in self.api.request("notes", project=self.project)
                    if row["id"] == note["id"]
                ),
                None,
            )

    def draft_path(self, identifier):
        return self.root / "drafts" / (fingerprint(identifier) + ".json")

    def checkpoint(self, draft):
        write_state(self.draft_path(draft["id"]), draft, durable=True)

    def edit(self, note, *, immediate=False):
        previous = self.drafts.get(note["id"], {})
        if not (note["body"].strip() or note.get("version") or previous):
            return
        draft = {
            key: note[key]
            for key in ("id", "body", "thread", "turn", "version")
        }
        now = time.time()
        draft.update(
            due=now if immediate else now + 1,
            checkpoint=now if immediate else now + 0.25,
            conflict=previous.get("conflict", False),
        )
        self.drafts[draft["id"]] = draft
        if immediate:
            self.checkpoint(draft)
            draft["checkpoint"] = None

    def send(self, draft):
        payload = {key: draft[key] for key in ("id", "body", "thread", "turn")}
        payload.update(project=self.project, format="markdown")
        version = draft["version"]
        if version is None:
            result = self.api.request("notes", payload)
            version = result.get("version", 1)
            if result.get("matches", True):
                return version
            if version != 1:
                raise JournalHTTPError(
                    409, "The newly created note has already changed."
                )
        return self.api.request("edit-note", payload | {"version": version})["version"]

    def poll(self, now):
        for identifier, (job, sent) in tuple(self.saves.items()):
            if not job.done():
                continue
            del self.saves[identifier]
            draft = self.drafts[identifier]
            try:
                version = job.result()
            except (OSError, RuntimeError) as error:
                draft["conflict"] = isinstance(
                    error, JournalHTTPError
                ) and error.status in (404, 409)
                draft["error"] = str(error)
                draft["due"] = now + 4
                if isinstance(error, JournalHTTPError) and error.status == 400:
                    draft["due"] = None
            else:
                self.revision += 1
                self.recoveries[identifier] = sent | {"version": version}
                if len(self.recoveries) > 12:
                    del self.recoveries[next(iter(self.recoveries))]
                if draft["body"] == sent["body"]:
                    self.draft_path(identifier).unlink(missing_ok=True)
                    del self.drafts[identifier]
                    continue
                draft["version"] = version
                draft.pop("error", None)
            self.checkpoint(draft)
            draft["checkpoint"] = None
        for identifier, draft in tuple(self.drafts.items()):
            if draft.get("checkpoint") is not None and draft["checkpoint"] <= now:
                self.checkpoint(draft)
                draft["checkpoint"] = None
            if (
                identifier not in self.saves
                and len(self.saves) < 2
                and not draft.get("conflict")
                and draft.get("due") is not None
                and draft["due"] <= now
            ):
                sent = draft.copy()
                self.saves[identifier] = background(self.send, sent), sent

    def status(self, identifier):
        if identifier in self.saves:
            return "Saving…"
        draft = self.drafts.get(identifier)
        if not draft:
            return "Saved"
        if draft.get("conflict"):
            return "Conflict · ^R review"
        if draft.get("error"):
            return "Saved locally · " + draft["error"]
        return "Saved locally" if draft.get("checkpoint") is None else "Editing"

    def discard(self, identifier):
        self.draft_path(identifier).unlink(missing_ok=True)
        self.drafts.pop(identifier, None)
