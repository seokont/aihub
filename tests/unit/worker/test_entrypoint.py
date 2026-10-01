"""The worker's published entry point (task 2.6).

The container will run `arq moni_worker.main.WorkerSettings`. Nothing else in the suite invokes that
dotted name, so without these tests a rename — or a class built only under some import order — is
invisible until the container fails to start. That is not hypothetical: `mcp/rag` shipped an image
whose `CMD` named a `__main__` module that did not exist, and 369 unit tests passed while the
container crash-looped, because every test drove `build_server` and none drove the published entry
point.

So the assertions here are deliberately about the *interface* arq relies on — the importable name and
the attributes it reads — rather than about the values, which `test_queue_discipline.py` already owns.
"""

from __future__ import annotations

import importlib
from typing import Any, Final

#: What arq's CLI reads off the class. Missing any of these is a startup failure, not a default.
REQUIRED_ATTRIBUTES: Final = (
    "redis_settings",
    "functions",
    "cron_jobs",
    "max_jobs",
    "job_timeout",
)


def test_the_entry_point_is_importable_under_the_name_the_container_uses() -> None:
    """`arq moni_worker.main.WorkerSettings` — importable, and a class, not an instance."""
    module = importlib.import_module("moni_worker.main")

    assert hasattr(module, "WorkerSettings"), "arq's CLI would have nothing to load"
    assert isinstance(module.WorkerSettings, type), "arq reads the settings as class attributes"


def test_the_entry_point_exposes_every_attribute_arq_reads() -> None:
    """A missing attribute is the kind of failure that appears only on the deployed container, so it
    is asserted here against the real object rather than described in a comment."""
    settings: Any = importlib.import_module("moni_worker.main").WorkerSettings

    missing = [name for name in REQUIRED_ATTRIBUTES if not hasattr(settings, name)]
    assert not missing, f"arq would fail to start the worker: missing {missing}"


def test_the_entry_point_reads_the_environment_at_import(monkeypatch: Any) -> None:
    """The numbers come from the environment, and a reload picks up a change to it.

    Reloaded because configuration is resolved at import — which is arq's model (one process, one
    environment) and is why this test cannot simply mutate a module-level value.
    """
    import moni_worker.main as entry

    monkeypatch.setenv("WORKER_MAX_JOBS", "3")
    monkeypatch.setenv("POLL_MINUTES", "9")
    reloaded = importlib.reload(entry)

    assert reloaded.config.max_jobs == 3
    assert reloaded.config.poll_minutes == 9
    assert reloaded.WorkerSettings.max_jobs == 3

    monkeypatch.undo()
    importlib.reload(entry)  # leave the module as the rest of the suite expects to find it


def test_the_entry_point_holds_one_config_object_for_the_process() -> None:
    """The job layer must not re-read the environment per run: a poll interval that changed mid-flight
    would make "every five minutes" a number nobody could state."""
    entry = importlib.import_module("moni_worker.main")

    assert entry.WorkerSettings.max_jobs == entry.config.max_jobs
    assert entry.WorkerSettings.job_timeout == entry.config.job_timeout_seconds
