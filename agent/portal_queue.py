"""Path B: Persistent queue for portal writes when the portal is unreachable.

When the agent's direct portal calls fail (429 rate limit, firewall, network),
write operations are saved to a JSONL queue file instead of being lost.
The queue is committed to the repo so it survives across runs.

Two flushers drain the queue:
1. The agent itself, at the start of each run (flush_queue).
2. Muse, via a scheduled cron that reads the repo and pushes through
   its own portal connection (independent network path).

Format: one JSON object per line in portal_queue/pending.jsonl:
    {"ts": 1234567890, "action": "outreach_log", "payload": {...}}

Only WRITE actions are queued. Reads (leads_to_email, etc.) still raise
PortalError — they can't be deferred.
"""
from __future__ import annotations

import json
import os
import time

# Actions that change portal state — safe to queue and retry.
# Reads are NOT queued (they need an immediate answer).
WRITE_ACTIONS = {
    "upsert_lead",
    "save_audit",
    "outreach_log",
    "save_ads",
    "save_messages",
    "mark_bad_data",
    "log",
    "state_set",
    "add_note",
    "add_seo",
    "seo_update",
    "leads_stage",
}

QUEUE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "portal_queue")
QUEUE_FILE = os.path.join(QUEUE_DIR, "pending.jsonl")
MAX_QUEUE_AGE_DAYS = 7  # drop items older than this (stale data)


def _ensure_dir():
    os.makedirs(QUEUE_DIR, exist_ok=True)
    # Keep the directory in git even when empty.
    gitkeep = os.path.join(QUEUE_DIR, ".gitkeep")
    if not os.path.exists(gitkeep):
        open(gitkeep, "w").close()


def queue_write(action: str, payload: dict) -> bool:
    """Save a failed portal write to the persistent queue. Returns True if saved."""
    if action not in WRITE_ACTIONS:
        return False
    _ensure_dir()
    item = {"ts": int(time.time()), "action": action, "payload": payload}
    try:
        with open(QUEUE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_queue() -> list[dict]:
    """Read all pending queue items (oldest first)."""
    if not os.path.exists(QUEUE_FILE):
        return []
    items = []
    cutoff = int(time.time()) - MAX_QUEUE_AGE_DAYS * 86400
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("ts", 0) >= cutoff:
                    items.append(item)
    except OSError:
        pass
    return items


def remove_queued(items_done: list[dict]) -> None:
    """Remove successfully delivered items from the queue file."""
    if not os.path.exists(QUEUE_FILE):
        return
    done_ids = {id(it) for it in items_done}
    remaining = [it for it in read_queue() if id(it) not in done_ids]
    # Rewrite without the done items (match by ts+action+payload).
    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            for it in remaining:
                # Skip items that were in the done list (compare content).
                if any(d.get("ts") == it.get("ts") and d.get("action") == it.get("action")
                       for d in items_done):
                    continue
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    except OSError:
        pass


def queue_size() -> int:
    return len(read_queue())
