#!/usr/bin/env python3
"""Entry point run on a daily schedule (systemd timer, cron, ...).

Copies the live SQLite DB into config.BACKUP_DIR using sqlite3's online
backup API (safe to run while sync.py/app.py have the DB open), then prunes
backups older than config.BACKUP_RETENTION_DAYS.
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backup")


def run():
    backup_dir = Path(config.BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_path = backup_dir / f"fitness-{stamp}.db"

    src = sqlite3.connect(config.DB_PATH)
    dest = sqlite3.connect(dest_path)
    with dest:
        src.backup(dest)
    dest.close()
    src.close()
    log.info("Backed up %s -> %s", config.DB_PATH, dest_path)

    cutoff = datetime.now() - timedelta(days=config.BACKUP_RETENTION_DAYS)
    for old_file in backup_dir.glob("fitness-*.db"):
        if datetime.fromtimestamp(old_file.stat().st_mtime) < cutoff:
            old_file.unlink()
            log.info("Pruned old backup %s", old_file)


if __name__ == "__main__":
    run()
