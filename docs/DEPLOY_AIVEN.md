# Deploy the Limos gateway on Aiven Runtime

The gateway (FastAPI) runs as a container next to our Aiven PostgreSQL. The decision log, quotes, approvals and orders live in Postgres; Valio's data is fetched by the container at start and never stored in the repository or the image.

## What is in the repo

- `Containerfile`: Python 3.12 image, installs the project and psycopg, exposes port 8000.
- `scripts/prepare_runtime.py`: runs before the server. Downloads Valio's product, reliability and CO2 files with the `VALIO_API_KEY` secret, builds `catalog.slim.json`, and seeds the demo customers' history. Takes about 35 s. Prints counts only.
- `.containerignore` / `.dockerignore`: keep `.env`, `data/`, `.runtime/` and `web/` out of any build.

## Steps in the Aiven console

1. **Runtime → Deploy application.** Connect the GitHub repo, pick the branch that has the `Containerfile` (`web` until Mikael merges it to `main`), and choose `Containerfile` as the manifest. Scan, then deploy.
2. **Link the PostgreSQL service** to the app so Aiven adds its connection string. Check the variable name in **Environment variables**: the gateway reads `DATABASE_URL`. If Aiven uses another name, add `DATABASE_URL` yourself as a secret with the same value.
3. **Secrets tab:** `VALIO_API_KEY`, `GEMINI_API_KEY`.
4. **Variables tab:**
   - `VALIO_BASE_URL` = `https://junction26.valioaimo.fi` (no `/api` at the end)
   - `GEMINI_MODEL` = the model in `.env.example`
   - `WEB_ORIGIN` = the web app's URL (for example the Vercel URL), comma separated if several
   - `STORE` = `postgres` and `PORT` = `8000` are already set in the image.
5. **Port:** 8000.
6. After the first deploy, open `https://<app-url>/health`. Expect `{"status":"ok","valio_api":"ok","database":"ok"}`. Then `/customers` should list Pena and the chef.

Runtime does not redeploy on push. After new commits, redeploy from the console. Editing secrets or variables redeploys automatically.

## Point the web app at it

In the web host (or `web/.env.local`): `NEXT_PUBLIC_USE_MOCK=false` and `NEXT_PUBLIC_API_BASE=https://<app-url>`. Add the web URL to the gateway's `WEB_ORIGIN`, or the browser blocks the calls (CORS).

## Connections

The Aiven Developer plan allows 15 PostgreSQL connections for the whole team. The gateway's pool uses at most `DB_POOL_MAX` (default 3), so one hosted gateway plus each person's local gateway fits.

## Not verified yet

- The image has not been built here (no Docker on the build machine). `prepare_runtime.py` and the package build were tested locally.
- The exact name of the variable Aiven adds for the linked database.
