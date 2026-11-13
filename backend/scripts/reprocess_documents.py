"""Re-extract and re-embed documents whose derived data is out of date: embeddings from a
different model, failed embeddings, or images stored before image reading was configured.

Operator tool (runs across all tenants on the configured database, outside the API).

Usage:
  python scripts/reprocess_documents.py            # dry run: list what would change
  python scripts/reprocess_documents.py --apply    # reprocess them
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.adapters.embeddings import embedder_from_settings  # noqa: E402
from app.adapters.storage import store_from_settings  # noqa: E402
from app.adapters.vision import describer_from_settings  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.domain.documents import needs_reprocessing, reprocess  # noqa: E402
from app.persistence.db import _session_factory  # noqa: E402
from app.persistence.documents import DocumentVersion  # noqa: E402


def main(apply: bool) -> None:
    settings = get_settings()
    embedder = embedder_from_settings(settings)
    describer = describer_from_settings(settings)
    store = store_from_settings(settings)
    print(f"embedding model: {embedder.model} | image reading: {describer.model if describer else 'off'}")

    with _session_factory()() as session:
        versions = [v for v in session.scalars(select(DocumentVersion).order_by(DocumentVersion.uploaded_at))
                    if needs_reprocessing(v, embedder, describer)]
        if not versions:
            print("Nothing to reprocess.")
            return
        for v in versions:
            before = f"{v.status}/{v.embedding_model or '-'}"
            if not apply:
                print(f"would reprocess {v.filename} v{v.version_no} ({before})")
                continue
            try:
                reprocess(session, v, store, embedder, describer)
                session.commit()
                print(f"reprocessed {v.filename} v{v.version_no}: {before} -> {v.status}/{v.embedding_model or '-'}")
            except Exception as exc:  # keep going; report each failure
                session.rollback()
                print(f"FAILED {v.filename} v{v.version_no}: {type(exc).__name__}: {exc}")
        if not apply:
            print(f"{len(versions)} version(s) need reprocessing. Run again with --apply.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv[1:])
