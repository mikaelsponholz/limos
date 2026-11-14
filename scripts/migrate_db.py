"""Apply store migrations explicitly: uv run python -m scripts.migrate_db."""

import logging

from gateway.settings import Settings
from gateway.store.postgres import PostgresStore


def main() -> int:
    # Pool connection retries can log exception text containing the connection URL.
    logger = logging.getLogger("psycopg.pool")
    was_disabled = logger.disabled
    logger.disabled = True
    try:
        settings = Settings()
        if not settings.database_url:
            print("Set DATABASE_URL before running migrations.")
            return 1
        store = PostgresStore(settings.database_url, migrate=False)
        try:
            applied = store.migrate()
        finally:
            store.close()
    except Exception:
        # Connection and configuration errors may contain credentials; never echo them.
        print("Database migration failed. Check the configuration and connection.")
        return 1
    finally:
        logger.disabled = was_disabled
    for filename in applied:
        print(filename)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
