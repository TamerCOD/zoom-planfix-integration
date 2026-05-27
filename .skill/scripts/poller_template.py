"""
Polling template for PlanFix integrations.

Why polling? Because:
  - PlanFix automations (webhooks) need ADMIN UI configuration
  - You can't create automations via REST API
  - `date_modified` filter doesn't work, sort doesn't work
  - For autonomous integrations, polling is the most reliable mechanism

This template covers:
  - Initial scan (record state without triggering)
  - State-hash change detection
  - In-memory dedup cache (survives within session)
  - Persistent dedup via meta in description (survives restarts)
  - Smart "is task ready" check (participants + start_time)
  - Per-task asyncio.Lock against race conditions
  - Tail-based pagination (newest tasks are at the END)
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Optional

# Adjust these imports to your project structure
# from .planfix_client import PlanfixClient, parse_user_id
# from .meta_storage import extract_meta, inject_meta

logger = logging.getLogger(__name__)


class BasePoller:
    """
    Generic poller. Subclass and override:
      - `is_my_task(task)` — filter for tasks you care about
      - `is_ready(task)` — return True when task is ready to process
      - `process_task(task, meta)` — your actual logic
    """

    def __init__(
        self,
        pf,                         # PlanfixClient
        poll_interval_s: int = 45,
        initial_scan_silent: bool = True,
        scan_pages: int = 3,
    ):
        self.pf = pf
        self.poll_interval_s = poll_interval_s
        self.scan_pages = scan_pages

        self._state_cache: dict = {}     # task_id → hash
        self._processed: dict = {}       # task_id → some-marker (e.g., result_id)
        self._first_seen: dict = {}      # task_id → unix_ts
        self._locks: dict = {}           # task_id → asyncio.Lock
        self._initialized = False
        self._initial_silent = initial_scan_silent
        self._stop = False

    # ─────────────────────── overrides ───────────────────────

    def is_my_task(self, task: dict) -> bool:
        """Subclass: return True for tasks this poller should process"""
        return True

    def is_ready(self, task: dict) -> bool:
        """Subclass: return True when the task has all required fields filled"""
        return True

    async def process_task(self, task: dict, meta: Optional[dict]):
        """Subclass: actual processing"""
        raise NotImplementedError

    # ─────────────────────── core ───────────────────────

    async def run(self):
        logger.info(f"Poller starting (interval={self.poll_interval_s}s)")
        while not self._stop:
            try:
                await self.tick()
            except Exception as e:
                logger.exception(f"Tick error: {e}")
            await asyncio.sleep(self.poll_interval_s)

    def stop(self):
        self._stop = True

    async def tick(self):
        if not self._initialized:
            await self._initial_scan()
            self._initialized = True
            return

        tasks = self._fetch_tail()
        my_tasks = [t for t in tasks if self.is_my_task(t)]

        changed = []
        for t in my_tasks:
            h = self._compute_hash(t)
            if self._state_cache.get(t["id"]) != h:
                changed.append(t)
                self._state_cache[t["id"]] = h

        if changed:
            logger.info(f"Tick: {len(my_tasks)} matched, {len(changed)} changed")

        for t in changed:
            try:
                await self._process(t)
            except Exception as e:
                logger.exception(f"Task {t['id']} error: {e}")

    async def _initial_scan(self):
        """Cache state of existing tasks WITHOUT triggering processing"""
        logger.info("Initial scan…")
        tasks = self._fetch_tail(pages=20)  # all visible tasks
        for t in tasks:
            self._state_cache[t["id"]] = self._compute_hash(t)
        logger.info(f"Initial scan done: cached {len(tasks)} tasks")

    def _fetch_tail(self, pages: int = None) -> list:
        """Fetch latest N pages (newest tasks at the end of PlanFix pagination)"""
        return self.pf.scan_tail(pages=pages or self.scan_pages)

    @staticmethod
    def _compute_hash(task: dict) -> str:
        """Hash key fields to detect changes"""
        key = json.dumps({
            "name": task.get("name"),
            "description": task.get("description") or "",
            "status_id": (task.get("status") or {}).get("id"),
            "start": task.get("startDateTime"),
            "end": task.get("endDateTime"),
            "assignees": task.get("assignees"),
            "participants": task.get("participants"),
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(key.encode()).hexdigest()

    def _get_lock(self, task_id: int) -> asyncio.Lock:
        if task_id not in self._locks:
            self._locks[task_id] = asyncio.Lock()
        return self._locks[task_id]

    async def _process(self, task: dict):
        task_id = task["id"]

        lock = self._get_lock(task_id)
        if lock.locked():
            return

        async with lock:
            # Get FRESH full task (tail data might be stale)
            full = self.pf.get_task(task_id)
            task = {**task, **(full or {})}

            # Extract integration meta
            from meta_storage import extract_meta
            meta = extract_meta(task.get("description") or "")

            # If we've already processed (have meta with result-marker) — just update path
            if meta and meta.get("processed"):
                # In-memory cache too (belt + suspenders)
                self._processed[task_id] = meta.get("processed")
                await self.process_task(task, meta)
                return

            # Not yet processed — readiness check
            if not self.is_ready(task):
                if task_id not in self._first_seen:
                    self._first_seen[task_id] = int(time.time())
                    logger.info(f"Task {task_id}: first seen, not ready yet")
                return

            # Process for the first time
            logger.info(f"Task {task_id}: ready, processing")
            await self.process_task(task, meta)


# ──────────────────────── example subclass ────────────────────────

class ZoomConferencePoller(BasePoller):
    """Example: process tasks with object ZOOM"""

    def __init__(self, pf, zoom_object_id: int, **kwargs):
        super().__init__(pf, **kwargs)
        self.zoom_object_id = zoom_object_id

    def is_my_task(self, task: dict) -> bool:
        obj_id = (task.get("object") or {}).get("id")
        return obj_id == self.zoom_object_id

    def is_ready(self, task: dict) -> bool:
        # Need at least 1 participant + start time
        attendees = self.pf.get_meeting_attendees(task)
        has_start = bool(task.get("startDateTime"))
        return bool(attendees) and has_start

    async def process_task(self, task: dict, meta):
        # Your logic here
        # e.g., create Zoom meeting, add comment, create RSVP subtasks
        pass
