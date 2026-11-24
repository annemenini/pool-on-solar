# pool-on-solar

Automate the activation / deactivation of a Jandy iAquaLink pool cleaning system and lights using live Tesla Energy solar surplus, deployed on **Vercel** (Python 3.14 on Fluid Compute + Vercel Schedules + Vercel Runtime Cache + Private Vercel Blob + BigQuery).

## Architecture

| Path | Vercel Primitive | Purpose |
| :--- | :--- | :--- |
| [api/web.py](api/web.py) | Function (Fluid Compute) | Serves the live Tesla Energy & iAquaLink dashboard (`/`), `/api/status`, Pause/Start Schedule controls (`/scheduler/pause`, `/scheduler/resume`), `/.well-known/appspecific/com.tesla.3p.public-key.pem`, and optional `/oauth/*` setup routes. |
| [jobs.py](jobs.py) | Vercel Schedules (`@schedule`) | Code-declared Python schedules registered via `vercel-schedules`: `pool-on-solar.automation` (every 15 min, `America/Los_Angeles`) controls iAquaLink pool pumps & lights, and `pool-on-solar.analysis` (daily at `04:00`, `America/Los_Angeles`) pushes yesterday's power & energy history to BigQuery. |
| [pool_on_solar/](pool_on_solar/) | Shared Python package | Config ([config.json](config.json)), Private Vercel Blob token store (`vercel.blob` for rotating Tesla OAuth tokens), Vercel Runtime Cache wrapper (`vercel.cache.RuntimeCache` for dashboard snapshots, Tesla site ID, schedule idempotency, and last-run status), official `vercel.schedules.sync` adapter (for pause/resume & schedule status), Tesla Fleet API client, automation, BigQuery analysis, and dashboard renderer. |

## Requirements

* [Tesla Energy system](https://www.tesla.com/energy) (solar panels, and optionally Powerwall) via the **Tesla Fleet API**
* [Jandy iAquaLink](https://www.jandy.com/en/products/controls/system-components/interfaces/iaqualink) (pool pump controls)
* [Vercel](https://vercel.com) project with a **Private Vercel Blob** store
* Google Cloud **BigQuery** tables (`POWER_TABLE_ID` and `ENERGY_TABLE_ID`) for daily energy history

## Setup & Deployment

### 1. Link the project & create a Private Vercel Blob store

```bash
vercel link
vercel blob create-store pool-on-solar --access private
```

Connect the Blob store to the project so `BLOB_STORE_ID` (with automatic Vercel OIDC) or `BLOB_READ_WRITE_TOKEN` is injected. This stores only the rotating Tesla OAuth token (`tesla/token.json`).

### 2. Generate a Tesla Fleet API EC key pair

```bash
openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem
openssl ec -in private-key.pem -pubout -out public-key.pem
```

Add the public key to Vercel environment variables and deploy so `https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem` is live:

```bash
vercel env add TESLA_PUBLIC_KEY_PEM production < public-key.pem
vercel deploy --prod
```

*(Attach your custom domain, e.g. `hilo.steren.fr`, in **Project Settings → Domains** or with `vercel domains add`.)*

### 3. Register your Tesla Developer App & authorize

1. Create an app at [developer.tesla.com](https://developer.tesla.com):
   - **Allowed Origin:** `https://<your-domain>`
   - **Allowed Redirect URI:** `https://<your-domain>/oauth/callback`
   - **Scope:** enable **Energy Product Information** (`energy_device_data`)
2. Set the required Vercel environment variables (Production):
   - `TESLA_CLIENT_ID`
   - `TESLA_CLIENT_SECRET`
   - `IAQUALINK_USER_ID`
   - `IAQUALINK_PASSWORD`
   - `OAUTH_ENABLED=true` *(temporarily for initial login)*
   - *(Optional)* `ENERGY_SITE_ID` (saves one `/api/1/products` call per run)
   - *(Optional)* `DASHBOARD_ACCESS_TOKEN` (protects `/`, `/api/status`, `/scheduler/*`, and `/oauth/*` with a cookie gate while leaving `/.well-known/appspecific/com.tesla.3p.public-key.pem` public for Tesla)
3. Redeploy (`vercel deploy --prod`), then open `https://<your-domain>/oauth` in your browser:
   - Click **1. Register Partner Domain**, then **2. Login with Tesla**.
   - On callback, the rotating OAuth tokens are saved to Private Vercel Blob and your `ENERGY_SITE_ID` is displayed.
4. Disable the interactive OAuth routes and redeploy:
   ```bash
   vercel env rm OAUTH_ENABLED production
   vercel deploy --prod
   ```

### 4. Configure BigQuery credentials for daily analysis

Set `POWER_TABLE_ID` (e.g. `pool-on-solar.hilo_tesla.power`) and `ENERGY_TABLE_ID` (e.g. `pool-on-solar.hilo_tesla.energy`), plus **one** GCP authentication method:

- **Option A — Keyless Vercel OIDC → GCP Workload Identity Federation (recommended):**
  Configure a Workload Identity Pool & OIDC provider trusting Vercel (`https://oidc.vercel.com/<your-team-slug>`) with a GCP service account granted `roles/bigquery.dataEditor`, then set:
  - `GCP_PROJECT_ID`
  - `GCP_PROJECT_NUMBER`
  - `GCP_WORKLOAD_IDENTITY_POOL_ID`
  - `GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID`
  - `GCP_SERVICE_ACCOUNT_EMAIL`
- **Option B — Service Account JSON secret:**
  Store a JSON key for a least-privilege BigQuery service account (`roles/bigquery.dataEditor`) in the `GOOGLE_SERVICE_ACCOUNT_JSON` Vercel Secret environment variable.

### 5. Protecting the dashboard

- **Option A (`DASHBOARD_ACCESS_TOKEN`):** Works on all plans on the same domain as Tesla's public key endpoint. Open `https://<your-domain>/?token=<DASHBOARD_ACCESS_TOKEN>` once to set a 30-day `HttpOnly` session cookie; `/.well-known/appspecific/com.tesla.3p.public-key.pem` remains publicly readable by Tesla.
- **Option B (Vercel Deployment Protection / Vercel Passport):** Enable Deployment Protection in **Project Settings → Deployment Protection** (if Tesla needs to re-read `/.well-known/...` on the same domain, use Deployment Protection Exceptions or host the public key on an unprotected domain).

### 6. Managing Vercel Schedules

Schedules declared with `@schedule` in [jobs.py](jobs.py) (registered via `"schedules": { "pool-on-solar": { "root": ".", "entrypoint": "jobs" } }` in [vercel.json](vercel.json)) start active in Production as `pool-on-solar.automation` (every 15 min) and `pool-on-solar.analysis` (daily at `04:00` `America/Los_Angeles`). Inspect or trigger them with the CLI:

```bash
vercel schedules list
vercel schedules invoke pool-on-solar.automation
vercel schedules invoke pool-on-solar.analysis
vercel schedules executions list pool-on-solar.automation --since 7d
```

The **Pause Schedule / Start Schedule** button on the dashboard uses the official `vercel-schedules` Python SDK (`vercel.schedules.sync.disable_schedule` / `enable_schedule` / `get_schedule`, authenticated via the deployment's Vercel OIDC token). Enabled/disabled state persists across deployments, and recent run status & actions are surfaced via Vercel Runtime Cache.

## Configuration Reference

Edit [config.json](config.json) (or pass a JSON string in `POOL_CONFIG_JSON`) to adjust pool/light devices, power thresholds (`activation_excess_power`, `deactivation_excess_power`), operating windows, and location (`timezone`, `latitude`, `longitude`).

### Environment Variables

| Variable | Required | Description |
| :--- | :--- | :--- |
| `TESLA_CLIENT_ID` | Yes | Tesla Fleet API OAuth Client ID |
| `TESLA_CLIENT_SECRET` | Setup only | Required for `/oauth/register` and `/oauth/callback` |
| `TESLA_PUBLIC_KEY_PEM` | Yes | PEM public key served at `/.well-known/appspecific/com.tesla.3p.public-key.pem` |
| `TESLA_API_BASE_URL` | No | Regional Fleet API base URL (default NA: `https://fleet-api.prd.na.vn.cloud.tesla.com`) |
| `ENERGY_SITE_ID` | No | Tesla Energy site ID (auto-discovered if omitted) |
| `IAQUALINK_USER_ID` | Yes | iAquaLink account email |
| `IAQUALINK_PASSWORD` | Yes | iAquaLink account password |
| `BLOB_STORE_ID` / `BLOB_READ_WRITE_TOKEN` | Yes | Private Vercel Blob store credentials |
| `POWER_TABLE_ID` / `ENERGY_TABLE_ID` | Yes (analysis) | BigQuery target tables (`<project>.<dataset>.<table>`) |
| `GCP_PROJECT_ID`, `GCP_PROJECT_NUMBER`, `GCP_WORKLOAD_IDENTITY_POOL_ID`, `GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID`, `GCP_SERVICE_ACCOUNT_EMAIL` | For OIDC WIF | Keyless Vercel OIDC authentication to BigQuery |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Alternative to WIF | GCP service account JSON key for BigQuery |
| `OAUTH_ENABLED` | No | Set `true` temporarily to enable `/oauth/*` setup routes |
| `DASHBOARD_ACCESS_TOKEN` | No | Optional shared token cookie gate for `/`, `/api/status`, `/scheduler/*`, `/oauth/*` |
| `DRY_RUN` | No | Set `true` to log pool/light actions without switching devices |
| `STATUS_CACHE_TTL_SECONDS` | No | Dashboard Vercel Runtime Cache TTL in seconds (default `60`) |
| `POOL_CONFIG_JSON` | No | Optional JSON override for [config.json](config.json) |

## Local Development & Tests

```bash
python3 -m unittest discover -s tests -v
vercel dev
```
