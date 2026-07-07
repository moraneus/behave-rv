"""The mock todo app: tasks with due windows, plus a background sync channel.

Two entities: tasks (task_id, event type task.status) and the sync channel
(session_id, event type sync.status). The sync channel is homogeneous -- only
sync outcomes flow on it -- which is what makes the `historically` policy
read cleanly. Clock and sleep are injectable for deterministic verification.
"""

from __future__ import annotations

import time

from behave_rv.events.event import Event

TASK_TYPE = "task.status"
SYNC_TYPE = "sync.status"


class TodoService:
    def __init__(self, emit, clock=time.time, sleep=time.sleep, pace=0.5):
        self._emit = emit
        self._clock = clock
        self._sleep = sleep
        self._pace = pace

    def _task(self, tid: str, status: str) -> None:
        self._emit(Event(TASK_TYPE, self._clock(), {"task_id": tid},
                         {"status": status}, "todo-app"))
        self._sleep(self._pace)

    def _sync(self, sid: str, status: str) -> None:
        self._emit(Event(SYNC_TYPE, self._clock(), {"session_id": sid},
                         {"status": status}, "todo-app"))
        self._sleep(self._pace)

    # -- normal flows ---------------------------------------------------------

    def flow_quick_task(self, tid: str) -> None:
        for s in ("created", "started", "checkpoint", "completed"):
            self._task(tid, s)

    def flow_edit_and_archive(self, tid: str) -> None:
        for s in ("created", "edited", "started", "checkpoint", "completed", "archived"):
            self._task(tid, s)

    def flow_rework(self, tid: str) -> None:
        for s in ("created", "started", "checkpoint", "completed", "reopened", "completed"):
            self._task(tid, s)

    def flow_block_unblock(self, tid: str) -> None:
        for s in ("created", "started", "checkpoint", "blocked", "unblocked", "completed"):
            self._task(tid, s)

    def flow_sync_healthy(self, sid: str) -> None:
        for _ in range(4):
            self._sync(sid, "sync_ok")

    # -- buggy flows ------------------------------------------------------------

    def bug_complete_unstarted(self, tid: str) -> None:
        for s in ("created", "completed"):
            self._task(tid, s)

    def bug_edit_uncreated(self, tid: str) -> None:
        self._task(tid, "edited")

    def bug_reopen_uncompleted(self, tid: str) -> None:
        for s in ("created", "reopened"):
            self._task(tid, s)

    def bug_edit_archived(self, tid: str) -> None:
        for s in ("created", "started", "checkpoint", "completed", "archived", "edited"):
            self._task(tid, s)

    def bug_edit_deleted(self, tid: str) -> None:
        for s in ("created", "deleted", "edited"):
            self._task(tid, s)

    def bug_miss_due_window(self, tid: str) -> None:
        """Started, checkpointed, then abandoned: the due window is violated by
        the wall clock while the task sits idle."""
        for s in ("created", "started", "checkpoint"):
            self._task(tid, s)

    def bug_miss_checkpoint(self, tid: str) -> None:
        """Started and abandoned before the checkpoint: BOTH timers fire (the
        checkpoint at 3s, the due window at 5s) -- both are real misses."""
        for s in ("created", "started"):
            self._task(tid, s)

    def bug_blocked_completes(self, tid: str) -> None:
        for s in ("created", "started", "checkpoint", "blocked", "completed"):
            self._task(tid, s)

    def bug_sync_fails(self, sid: str) -> None:
        for s in ("sync_ok", "sync_ok", "sync_fail"):
            self._sync(sid, s)


FLOWS = {
    "quick": ("Play: quick task done in time", "normal", "flow_quick_task"),
    "edit_archive": ("Play: edit, complete, archive", "normal", "flow_edit_and_archive"),
    "rework": ("Play: complete, reopen, redo", "normal", "flow_rework"),
    "block_unblock": ("Play: block, unblock, complete", "normal", "flow_block_unblock"),
    "sync_ok": ("Play: healthy sync burst", "normal", "flow_sync_healthy"),
    "complete_unstarted": ("Trigger: complete an unstarted task", "bug", "bug_complete_unstarted"),
    "edit_uncreated": ("Trigger: edit an uncreated task", "bug", "bug_edit_uncreated"),
    "reopen_uncompleted": ("Trigger: reopen an uncompleted task", "bug", "bug_reopen_uncompleted"),
    "edit_archived": ("Trigger: edit an archived task", "bug", "bug_edit_archived"),
    "edit_deleted": ("Trigger: edit a deleted task", "bug", "bug_edit_deleted"),
    "miss_due": ("Trigger: miss the due window (timer)", "bug", "bug_miss_due_window"),
    "miss_checkpoint": ("Trigger: miss the checkpoint (2 timers)", "bug", "bug_miss_checkpoint"),
    "blocked_completes": ("Trigger: blocked task completes", "bug", "bug_blocked_completes"),
    "sync_fail": ("Trigger: sync failure", "bug", "bug_sync_fails"),
}
