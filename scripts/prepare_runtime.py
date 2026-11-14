"""Prepare a fresh container: python -m scripts.prepare_runtime.

Valio's data must never be in the repository or the image, so a hosted gateway
(Aiven Runtime) fetches it at start with its own VALIO_API_KEY secret, builds the
slim catalogue and resolves the seeded customer history. Skips the download and
build when a catalogue already exists (a restart on the same disk, or local use).
Prints counts only, never URLs, keys or supplier data.
"""

import asyncio
import sys
from pathlib import Path
from urllib.parse import urljoin

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from gateway.settings import Settings
from scripts.build_catalog import build_catalog
from scripts.fetch_valio_data import DOWNLOADS, OUTPUT_DIRECTORY, download
from scripts.seed_customers import seed_customers

# The catalogue needs these three; the OpenAPI and schema files are reference only.
NEEDED = {"products.json", "delivery-reliability.csv", "carbon-footprint.csv"}


async def fetch(settings: Settings) -> None:
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    base_url = settings.valio_base_url.rstrip("/") + "/"
    origin = httpx.URL(base_url)
    async with httpx.AsyncClient(timeout=max(settings.valio_timeout_seconds, 120)) as client:
        for filename, endpoint in DOWNLOADS:
            if filename in NEEDED:
                await download(
                    client,
                    urljoin(base_url, endpoint),
                    OUTPUT_DIRECTORY / filename,
                    settings.valio_api_key,
                    origin,
                )
                print(f"Downloaded {filename}")


def main() -> int:
    settings = Settings()
    catalog = Path(settings.catalog_path)
    if not catalog.exists():
        if not (settings.valio_api_key and settings.valio_base_url):
            print("Set VALIO_API_KEY and VALIO_BASE_URL to fetch the catalogue.")
            return 1
        try:
            asyncio.run(fetch(settings))
        except (httpx.HTTPError, OSError, ValueError):
            print("Valio download failed. Check the key, the base URL and the network.")
            return 1
        records = build_catalog(OUTPUT_DIRECTORY, catalog)
        print(f"Catalogue built: {len(records)} products")
    else:
        print("Catalogue found, skipping download")
    try:
        customers, lines = seed_customers(catalog)
    except (OSError, ValueError):
        print("Customer seeding failed; check the seed terms against the catalogue.")
        return 1
    print(f"Customers seeded: {customers}, history lines: {lines}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
