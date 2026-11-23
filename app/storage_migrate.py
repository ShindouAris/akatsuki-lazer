"""Copy the replays and beatmap files that are on the local disk into the object storage.

    python -m app.storage_migrate            # copy what is missing from the first remote backend
    python -m app.storage_migrate --dry-run  # only list it

Files are never deleted from the disk; run it again at any time, it skips what is already stored. The local disk is
never the target: if no S3 or SeaweedFS is configured there is nothing to migrate to.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from app.core.config import get_settings
from app.services.storage import LocalBackend
from app.services.storage import ObjectStorage
from app.services.storage import get_storage

logger = logging.getLogger("storage_migrate")


def local_files(settings) -> list[tuple[str, Path]]:  # noqa: ANN001
    """(key, path) of every replay and every cached .osz/.osu file on the disk."""
    found: list[tuple[str, Path]] = []
    replays = Path(settings.replays_path)
    if replays.is_dir():
        found += [(f"replays/{p.name}", p) for p in sorted(replays.glob("*.osr"))]
    beatmaps = Path(settings.beatmaps_path)
    for kind, pattern in (("osz", "*.osz"), ("osu", "*.osu")):
        folder = beatmaps / kind
        if folder.is_dir():
            found += [(f"beatmaps/{kind}/{p.name}", p) for p in sorted(folder.glob(pattern))]
    return found


async def migrate(storage: ObjectStorage, files: list[tuple[str, Path]], *, dry_run: bool = False) -> tuple[int, int, int]:
    """Returns (copied, already stored, failed)."""
    remote = ObjectStorage([b for b in storage.backends if not isinstance(b, LocalBackend)])
    copied = skipped = failed = 0
    for key, path in files:
        if await remote.exists(key):
            skipped += 1
            continue
        if dry_run:
            logger.info("would copy %s", key)
            copied += 1
            continue
        try:
            backend = await remote.put_file(key, path)
        except Exception as error:  # noqa: BLE001
            logger.error("could not copy %s: %s", key, error)
            failed += 1
            continue
        logger.info("copied %s to %s", key, backend)
        copied += 1
    return copied, skipped, failed


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = get_settings()
    storage = get_storage()
    if all(isinstance(b, LocalBackend) for b in storage.backends):
        logger.error("No S3 or SeaweedFS is configured: set S3_BUCKET or SEAWEEDFS_ENDPOINT_URL first.")
        return 2

    files = local_files(settings)
    logger.info("%d local files, remote backends: %s", len(files), [b.name for b in storage.backends[:-1]])
    copied, skipped, failed = await migrate(storage, files, dry_run=args.dry_run)
    logger.info("%s: %d, already stored: %d, failed: %d", "would copy" if args.dry_run else "copied", copied, skipped, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
