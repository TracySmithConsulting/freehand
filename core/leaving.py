"""When someone leaves (docs/design/people.md, "When someone leaves").

Removing a person is a decision, so the owner says what happens to what
they made, and FreeHand does all of it:

- **Straight away**, whatever the owner chooses: they're signed out
  everywhere, FreeHand stops answering their chats, and their approvals
  that were waiting are cancelled.
- **Their notes and files** (their folder, ``vault/people/<id>/``): kept
  for the owner in a folder "From <name>", or deleted.
- **Their scheduled jobs and team members:** kept for the owner (jobs
  paused, team members unshared), or deleted. A kept job's results go to
  the owner's console only, never to the person's old chat. Their
  requests to anyone else's team members leave those logs.
- **Their memories:** kept for the owner, or deleted. Deleted by default:
  they're private. What they shared stays shared.
- **Accounts that were theirs:** disconnected (the default), or given to
  the owner.
- **Their chats** (the last few exchanges) are always deleted: they're
  only kept for a few hours anyway.

Nothing is left under their id, and the id is never given to anyone new
(``former_people`` in settings), so a newcomer can't inherit anything.
Deleting everything (untick every "keep") is the privacy request a person
can make under POPIA.

If memory (Hindsight) can't be reached, the rest is still done, and the
person is listed on the People page with "Finish" to try memories again.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

FORMER = "former_people"


# ── What a person has ────────────────────────────────────────────────

def _workspace(person_id: str):
    from core import scheduler
    return scheduler._workspace(person_id)


def _files(person_id: str) -> list:
    folder = _workspace(person_id)
    if not folder.is_dir():
        return []
    return [p for p in folder.rglob("*") if p.is_file() and p.name != ".sweep_state.json"]


def _count(table: str, person_id: str, where: str = "") -> int:
    from core import database
    try:
        conn = sqlite3.connect(str(database.DB_PATH), timeout=10)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE person = ?{where}", (person_id,)).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:  # no such table yet: nothing of theirs
        return 0


def _memory_documents(person_id: str) -> Optional[list]:
    """Their memories as Hindsight documents (one per thing remembered),
    or None if memory can't be reached. [] when memory is turned off."""
    from core import long_memory
    if not long_memory.enabled():
        return []
    if not long_memory._ensure_bank():
        return None
    params = {"tags": [f"person:{person_id}"], "tags_match": "any_strict", "limit": 100}
    code, data = long_memory._call(
        "GET", long_memory._bank_path("/documents?" + urllib.parse.urlencode(params, doseq=True)))
    if code != 200 or not isinstance(data, dict):
        return None
    return [d for d in data.get("items") or [] if isinstance(d, dict) and d.get("id")]


def _their_team(person_id: str) -> int:
    from core import team
    return sum(1 for m in team.all_members() if team.maker(m) == person_id)


def their_accounts(person_id: str) -> list:
    from core import accounts
    return [{"account": a, "name": accounts.describe(a)}
            for a, whose in accounts.owners().items() if whose == person_id]


def what_they_have(person_id: str) -> dict:
    """For the People page, before the owner decides."""
    docs = _memory_documents(person_id)
    return {
        "files": len(_files(person_id)),
        "jobs": _count("tasks", person_id),
        "team_members": _their_team(person_id),
        "approvals_waiting": _count("approvals", person_id, " AND status = 'pending'"),
        "memories": None if docs is None else len(docs),
        "accounts": their_accounts(person_id),
    }


# ── Each kind of thing ───────────────────────────────────────────────

def _folder_name(name: str, person_id: str) -> str:
    safe = re.sub(r"[^\w .'-]", "", name, flags=re.UNICODE).strip(" .") or person_id
    return f"From {safe}"


def _handle_files(person_id: str, name: str, keep: bool) -> Optional[str]:
    """Move their folder to the owner's "From <name>", or delete it.
    Returns the kept folder's name, or None."""
    from core import scheduler
    folder = _workspace(person_id)
    if not folder.is_dir():
        return None
    if not keep or not _files(person_id):
        shutil.rmtree(folder, ignore_errors=True)
        return None
    base = _folder_name(name, person_id)
    dest, n = scheduler.VAULT_DIR / base, 2
    while dest.exists():
        dest, n = scheduler.VAULT_DIR / f"{base} ({n})", n + 1
    shutil.move(str(folder), str(dest))
    (dest / ".sweep_state.json").unlink(missing_ok=True)  # the owner's pad has its own
    return dest.name


def _handle_jobs(person_id: str, name: str, keep: bool) -> int:
    from core import people, scheduler
    conn = scheduler._tasks_conn()
    try:
        scheduler._ensure_deliver_column(conn)
        people.ensure_person_column(conn, "tasks")
        if keep:
            cur = conn.execute(
                "UPDATE tasks SET person = ?, deliver_to = NULL, title = title || ?, "
                "status = CASE WHEN status IN ('pending', 'running') THEN 'paused' ELSE status END "
                "WHERE person = ?",
                (people.OWNER_ID, f" (from {name})", person_id))
        else:
            cur = conn.execute("DELETE FROM tasks WHERE person = ?", (person_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _delete_rows(table: str, person_id: str) -> None:
    from core import database
    try:
        conn = sqlite3.connect(str(database.DB_PATH), timeout=10)
        try:
            conn.execute(f"DELETE FROM {table} WHERE person = ?", (person_id,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:  # no such table: nothing to delete
        pass


def _handle_memories(person_id: str, keep: bool) -> Optional[int]:
    """Give their memories to the owner, or delete them. Returns how many,
    or None if memory couldn't be reached (try again later).

    Each thing FreeHand remembered is one Hindsight document, and its
    tags reach every fact and summary made from it, so changing or
    deleting the document is enough (checked against a real Hindsight,
    November 2026). The shared tag is kept: what they shared stays
    shared."""
    from core import long_memory, people
    me, done = f"person:{person_id}", 0
    for _ in range(1000):  # 100 at a time; never loop for ever
        docs = _memory_documents(person_id)
        if docs is None:
            return None
        if not docs:
            return done
        for doc in docs:
            tags = [t for t in doc.get("tags") or [] if t != me]
            if keep:
                tags.append(f"person:{people.OWNER_ID}")
            path = long_memory._bank_path(f"/documents/{urllib.parse.quote(doc['id'], safe='')}")
            code, _ = (long_memory._call("PATCH", path, {"tags": tags}, timeout=60.0) if tags
                       else long_memory._call("DELETE", path, timeout=60.0))
            if code is None or not 200 <= code < 300:
                return None
            done += 1
    return None


async def _disconnect(account: str) -> bool:
    try:
        _kind, service, label = account.split(":", 2)
        import importlib  # core.oauth's "router" is the APIRouter, not this module
        await importlib.import_module("core.oauth.router").disconnect(service, label)
        return True
    except Exception as e:  # a 404 or a backend that's down: say so
        log.info("couldn't disconnect %s: %s", account, e)
        return False


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _former(s: dict) -> list:
    return [f for f in s.get(FORMER) or [] if isinstance(f, dict) and f.get("id")]


def _set_former(person_id: str, name: str, memories_pending: Optional[str]) -> None:
    from core import agent_config
    s = agent_config._load_settings()
    former = [f for f in _former(s) if f["id"] != person_id]
    entry = {"id": person_id, "name": name,
             "left": datetime.now(timezone.utc).date().isoformat()}
    if memories_pending:
        entry["memories"] = memories_pending  # "keep" or "delete", still to do
    former.append(entry)
    s[FORMER] = former
    agent_config._save_settings(s)


def former_ids(s: dict) -> set:
    return {str(f["id"]).lower() for f in _former(s)}


def unfinished(s: Optional[dict] = None) -> list:
    """People who left whose memories still need keeping or deleting."""
    from core import people
    s = s if s is not None else people._settings()
    return [{"id": f["id"], "name": f.get("name") or f["id"], "memories": f["memories"]}
            for f in _former(s) if f.get("memories") in ("keep", "delete")]


def _sync_notes() -> None:
    try:
        from core import memory
        memory.sync_vault_to_sqlite()
    except Exception as e:  # the search index catches up on its next sync
        log.debug("notes index sync failed: %s", e)


# ── Removing someone ─────────────────────────────────────────────────

async def remove(person_id: str, keep_files: bool = True, keep_jobs: bool = True,
                 keep_memories: bool = False, disconnect_accounts: bool = True) -> dict:
    """Take someone off this FreeHand and do what the owner chose with
    their things. Returns what was done, as sentences for the owner."""
    import asyncio
    from core import accounts, agent_config, people
    person_id = str(person_id or "").strip().lower()
    if person_id == people.OWNER_ID:
        raise people.PeopleError("The owner can't be removed.")
    person = people.get(person_id)
    if person is None:
        raise people.PeopleError("There's nobody with that id on this FreeHand.")
    name = person["name"]

    # Access stops first, whatever happens next.
    people.remove_person(person_id)
    _set_former(person_id, name, "keep" if keep_memories else "delete")
    done = [f"{name} is signed out, and FreeHand no longer answers their chats."]

    _delete_rows("approvals", person_id)
    _delete_rows("conversation_turns", person_id)
    _delete_rows("notices", person_id)

    folder = _handle_files(person_id, name, keep_files)
    if folder:
        done.append(f"Their notes are in your folder \"{folder}\".")
    elif keep_files:
        done.append("They had no notes to keep.")
    else:
        done.append("Their notes were deleted.")
    _sync_notes()

    jobs = _handle_jobs(person_id, name, keep_jobs)
    if jobs and keep_jobs:
        it = "it" if jobs == 1 else "them"
        done.append(f"Their {_n(jobs, 'scheduled job is', 'scheduled jobs are')} yours now, paused. "
                    f"Resume {it} under Scheduled jobs if you want {it}.")
    elif jobs:
        done.append(f"Their {_n(jobs, 'scheduled job was', 'scheduled jobs were')} deleted.")

    from core import team
    helpers = team.leave(person_id, keep_jobs)
    if helpers and keep_jobs:
        done.append(f"Their {_n(helpers, 'team member is', 'team members are')} yours now, not shared.")
    elif helpers:
        done.append(f"Their {_n(helpers, 'team member was', 'team members were')} deleted.")

    s = agent_config._load_settings()
    owners = accounts.owners(s)
    for account in [a for a, whose in owners.items() if whose == person_id]:
        label = accounts.describe(account)
        if disconnect_accounts:
            if await _disconnect(account):
                done.append(f"{label} was disconnected.")
            else:
                done.append(f"FreeHand couldn't disconnect {label}. It's yours now: disconnect it under Connections.")
        else:
            done.append(f"{label} is yours now.")
        owners.pop(account, None)  # theirs no more: the owner's, the default
    s = agent_config._load_settings()
    s[accounts.SETTING] = owners
    agent_config._save_settings(s)

    memories = await asyncio.to_thread(_handle_memories, person_id, keep_memories)
    if memories is None:
        done.append("FreeHand's memory couldn't be reached, so their memories are still to do. "
                    "Press Finish next to their name on the People page later.")
    else:
        _set_former(person_id, name, None)
        if memories:
            done.append(f"Their {_n(memories, 'memory is', 'memories are')} yours now." if keep_memories
                        else f"Their {_n(memories, 'memory was', 'memories were')} deleted.")
    done.append(f"Tell {name} what you kept: it's their information.")
    return {"done": done, "all_done": memories is not None}


async def finish(person_id: str) -> dict:
    """Try the memories again for someone who left."""
    import asyncio
    from core import people
    person_id = str(person_id or "").strip().lower()
    entry = next((f for f in unfinished() if f["id"] == person_id), None)
    if entry is None:
        raise people.PeopleError("There's nothing left to do for them.")
    keep = entry["memories"] == "keep"
    memories = await asyncio.to_thread(_handle_memories, person_id, keep)
    if memories is None:
        return {"done": ["FreeHand's memory still can't be reached. Try again later."], "all_done": False}
    _set_former(person_id, entry["name"], None)
    said = (_n(memories, "memory is", "memories are") + " yours now" if keep
            else _n(memories, "memory was", "memories were") + " deleted")
    return {"done": [f"{entry['name']}'s {said}."], "all_done": True}
