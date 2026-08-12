"""Scheduled task modules (ADR-0011).

One module per scheduled job. Each declares its task against the `src.broker` singleton, gates
itself on a flag that is OFF by default, and imports its heavy dependencies INSIDE the task body
rather than at module scope — so a disabled task costs an import of structlog, the broker and its
settings profile, and nothing else.

Every module here must be listed in `src/worker_main.py`'s `_TASK_MODULES`. Taskiq only imports
the broker module itself; a task module that is never imported is never registered, and its
messages are enqueued and silently never consumed.

No re-exports: task modules are imported for their decoration side effect, not for names.
"""
