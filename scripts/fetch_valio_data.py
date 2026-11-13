"""Download Valio files: python -m scripts.fetch_valio_data --schema-url URL.

Valio's supplied OpenAPI file has no product-schema download endpoint, so its
URL must be provided explicitly. The other download endpoints come from that spec.
"""

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urljoin

import httpx

from gateway.settings import Settings

DOWNLOADS = (
    ("products.json", "/api/products/download"),
    ("delivery-reliability.csv", "/api/delivery-reliability/download"),
    ("carbon-footprint.csv", "/api/carbon-footprint/download"),
    ("openapi.json", "/openapi.json"),
)
OUTPUT_DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "valio"


async def download(
    client: httpx.AsyncClient, url: str, destination: Path, api_key: str, api_origin: httpx.URL
) -> None:
    # Follow redirects explicitly: signed external downloads must not receive the API key.
    current = httpx.URL(url)
    for _ in range(21):
        if current.scheme not in ("https", "http") or current.userinfo:
            raise ValueError("Download URL must be HTTP(S) without embedded credentials.")
        if api_origin.scheme == "https" and current.scheme != "https":
            raise ValueError("Download redirects must not downgrade HTTPS.")
        same_origin = (current.scheme, current.host, current.port) == (
            api_origin.scheme,
            api_origin.host,
            api_origin.port,
        )
        headers = {"X-API-Key": api_key} if same_origin else {}
        async with client.stream("GET", current, headers=headers) as response:
            if response.is_redirect:
                location = response.headers.get("Location")
                if location is None:
                    raise ValueError("Download redirect is missing its location.")
                current = current.join(location)
                continue
            response.raise_for_status()
            partial = destination.with_suffix(destination.suffix + ".part")
            try:
                with partial.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        output.write(chunk)
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)
            return
    raise ValueError("Download exceeded the redirect limit.")


async def run(settings: Settings, schema_url: str) -> None:
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    base_url = settings.valio_base_url.rstrip("/") + "/"
    api_origin = httpx.URL(base_url)
    files = [(filename, urljoin(base_url, endpoint)) for filename, endpoint in DOWNLOADS] + [
        ("products.schema.json", urljoin(base_url, schema_url))
    ]
    async with httpx.AsyncClient(timeout=settings.valio_timeout_seconds) as client:
        for filename, url in files:
            await download(
                client, url, OUTPUT_DIRECTORY / filename, settings.valio_api_key, api_origin
            )
            print(f"Downloaded {filename}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema-url",
        help="Product-schema URL or endpoint, absent from the supplied OpenAPI spec.",
    )
    args = parser.parse_args()
    settings = Settings()
    if not settings.valio_api_key:
        print("Set VALIO_API_KEY in the environment before running this script.")
        return 1
    if not settings.valio_base_url:
        print("Set VALIO_BASE_URL in the environment before running this script.")
        return 1
    if not args.schema_url:
        parser.error("Supply --schema-url; the supplied OpenAPI spec has no schema endpoint.")
    try:
        asyncio.run(run(settings, args.schema_url))
    except (httpx.HTTPError, OSError, ValueError):
        # HTTP errors can include signed redirect URLs or arbitrary server messages.
        print("Valio download failed. Check the configured URLs, access and output directory.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
