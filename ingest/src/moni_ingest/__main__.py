"""``python -m moni_ingest`` — ingestion CLI entry point.

The task described this as ``python -m ingest.cli``. The import package is ``moni_ingest``
(the ``ingest/`` directory is a workspace *project*, not a Python package — it holds
``src/moni_ingest``), so the runnable form is ``python -m moni_ingest``. Both this and
``python -m moni_ingest.cli`` work.

    uv run --group dev python -m moni_ingest add ./docs --roles manager,director
"""

from __future__ import annotations

import sys

from moni_ingest.cli import main

if __name__ == "__main__":
    sys.exit(main())
