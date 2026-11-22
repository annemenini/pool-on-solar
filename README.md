# pool-on-solar

Automate the activation / deactivation of a pool cleaning system and lights so they run when excess solar power (and Powerwall charge) is available.

## Components

| Directory | Cloud Run Resource | Purpose |
| :--- | :--- | :--- |
| [automation/](automation/) | Job (`pool-on-solar`) | Scheduled every 15 min: reads live Tesla Energy status and controls iAquaLink pool pumps and lights. |
| [tesla/analysis/](tesla/analysis/) | Job (`pool-on-solar-analysis`) | Scheduled daily: loads yesterday's Tesla power and energy history into BigQuery. |
| [tesla/authentication/](tesla/authentication/) | Service (`tesla-oauth`) | Hosts `/.well-known/appspecific/com.tesla.3p.public-key.pem` and handles one-time partner registration + OAuth login (`--max-instances=1`). |
| [dashboard/](dashboard/) | Service (`pool-on-solar-dashboard`) | Monitoring UI behind IAP (`pool.steren.fr`) showing live Tesla Energy stats, iAquaLink device states, job/scheduler/token health (60s TTL cache), and a Pause/Start Schedule control. |

## Requirements

* [Tesla Energy system](https://www.tesla.com/energy) (solar panels, and optionally Powerwall) using the **Tesla Fleet API**
* [Jandy iAquaLink](https://www.jandy.com/en/products/controls/system-components/interfaces/iaqualink) (pool pump controls)
* Google Cloud Project (Cloud Run, Cloud Scheduler, Secret Manager, IAP)

## Tesla Fleet API & OAuth Service (`tesla/authentication/`)

This project authenticates via the **Tesla Fleet API** and persists rotating OAuth2 refresh tokens in **Google Cloud Secret Manager** (destroying older versions on each rotation so only the latest version is kept, with local file fallback for local development).

1. **Generate an EC key pair** (`secp256r1` / `prime256v1`) and store it in Secret Manager:
   ```bash
   openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem
   openssl ec -in private-key.pem -pubout -out public-key.pem

   gcloud secrets create tesla-fleet-token --replication-policy=automatic
   gcloud secrets create tesla-fleet-public-key --replication-policy=automatic --data-file=public-key.pem
   gcloud secrets create tesla-fleet-private-key --replication-policy=automatic --data-file=private-key.pem
   ```

2. **Create a dedicated least-privilege service account** for `tesla-oauth`:
   ```bash
   gcloud iam service-accounts create tesla-oauth-sa \
     --display-name="Tesla OAuth Cloud Run Service"

   gcloud secrets add-iam-policy-binding tesla-fleet-public-key \
     --member="serviceAccount:tesla-oauth-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"

   gcloud secrets add-iam-policy-binding tesla-fleet-token \
     --member="serviceAccount:tesla-oauth-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretVersionManager"
   ```

3. **Deploy `tesla-oauth` to Cloud Run** (capped at `--max-instances=1`):
   ```bash
   gcloud run deploy tesla-oauth \
     --source=tesla/authentication \
     --region=us-west1 \
     --service-account="tesla-oauth-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
     --allow-unauthenticated \
     --max-instances=1 \
     --set-env-vars="TESLA_TOKEN_SECRET_NAME=projects/<PROJECT_ID>/secrets/tesla-fleet-token" \
     --set-secrets="TESLA_PUBLIC_KEY_PEM=tesla-fleet-public-key:latest"
   ```

4. **Create a Tesla Developer App** at [developer.tesla.com](https://developer.tesla.com):
   - Set **Allowed Origin** to `https://<your-tesla-oauth-domain>` and **Allowed Redirect URI** to `https://<your-tesla-oauth-domain>/callback`.
   - Enable the `Energy Product Information` (`energy_device_data`) scope.
   - Store the client secret in Secret Manager and attach credentials to `tesla-oauth`:
     ```bash
     gcloud secrets create tesla-client-secret --replication-policy=automatic --data-file=- <<< "<CLIENT_SECRET>"
     gcloud secrets add-iam-policy-binding tesla-client-secret \
       --member="serviceAccount:tesla-oauth-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
       --role="roles/secretmanager.secretAccessor"

     gcloud run services update tesla-oauth \
       --region=us-west1 \
       --update-env-vars="TESLA_CLIENT_ID=<CLIENT_ID>" \
       --update-secrets="TESLA_CLIENT_SECRET=tesla-client-secret:latest"
     ```

5. **Complete partner registration and OAuth login**:
   - Open `https://<your-tesla-oauth-domain>/` in your browser, click **1. Register Partner Domain**, then **2. Login with Tesla**.
   - On redirect to `/callback`, the service saves the OAuth tokens to `tesla-fleet-token` (destroying any older versions) and displays your discovered `ENERGY_SITE_ID`.
   - Lock down the OAuth routes while keeping `/.well-known/appspecific/com.tesla.3p.public-key.pem` active:
     ```bash
     gcloud run services update tesla-oauth \
       --region=us-west1 \
       --update-env-vars="OAUTH_ENABLED=false"
     ```
   *(An interactive CLI alternative is also available in [tesla/authentication/main.py](tesla/authentication/main.py).)*

## Monitoring Dashboard Service (`dashboard/`)

[dashboard/server.py](dashboard/server.py) serves an HTML dashboard (`/`), JSON endpoint (`/api/status`), and pause/resume controls (`POST /scheduler/pause`, `POST /scheduler/resume`) showing live Tesla Energy metrics, iAquaLink pool/light states, and Cloud Run job / Cloud Scheduler / Tesla OAuth token health (with a 60-second in-memory TTL cache).

1. **Create a dedicated least-privilege service account** for the dashboard:
   ```bash
   gcloud iam service-accounts create pool-on-solar-dashboard-sa \
     --display-name="Pool on Solar Dashboard Service"

   gcloud secrets add-iam-policy-binding tesla-fleet-token \
     --member="serviceAccount:pool-on-solar-dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"
   gcloud secrets add-iam-policy-binding tesla-fleet-token \
     --member="serviceAccount:pool-on-solar-dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretVersionManager"
   gcloud secrets add-iam-policy-binding iaqualink-password \
     --member="serviceAccount:pool-on-solar-dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"

   gcloud projects add-iam-policy-binding <PROJECT_ID> \
     --member="serviceAccount:pool-on-solar-dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
     --role="roles/run.viewer" \
     --condition=None
   gcloud projects add-iam-policy-binding <PROJECT_ID> \
     --member="serviceAccount:pool-on-solar-dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
     --role="roles/cloudscheduler.admin" \
     --condition=None
   ```

2. **Deploy behind Identity-Aware Proxy (IAP)** and grant user access:
   ```bash
   gcloud services enable iap.googleapis.com

   gcloud beta run deploy pool-on-solar-dashboard \
     --source=dashboard \
     --region=us-west1 \
     --service-account="pool-on-solar-dashboard-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
     --no-allow-unauthenticated \
     --iap \
     --max-instances=2 \
     --set-env-vars="TESLA_CLIENT_ID=<CLIENT_ID>,TESLA_TOKEN_SECRET_NAME=projects/<PROJECT_ID>/secrets/tesla-fleet-token,ENERGY_SITE_ID=<SITE_ID>,IAQUALINK_USER_ID=<EMAIL>,GCP_PROJECT_ID=<PROJECT_ID>,CLOUD_RUN_REGION=us-west1,CLOUD_RUN_JOB_NAME=pool-on-solar,SCHEDULER_JOB_NAME=pool-on-solar-scheduler-trigger" \
     --set-secrets="IAQUALINK_PASSWORD=iaqualink-password:latest"

   gcloud beta iap web add-iam-policy-binding \
     --resource-type=cloud-run \
     --service=pool-on-solar-dashboard \
     --region=us-west1 \
     --member="user:<USER_EMAIL>" \
     --role="roles/iap.httpsResourceAccessor"
   ```

3. **Configure a custom OAuth client for IAP** *(required for personal projects outside a Google Cloud Organization)*:
   - In **Google Auth Platform → Clients**, create a **Web application** OAuth client and add this Authorized redirect URI:
     ```text
     https://iap.googleapis.com/v1/oauth/clientIds/<OAUTH_CLIENT_ID>:handleRedirect
     ```
   - Save a temporary `iap-oauth.yaml`:
     ```yaml
     accessSettings:
       oauthSettings:
         clientId: <OAUTH_CLIENT_ID>
         clientSecret: <OAUTH_CLIENT_SECRET>
     ```
   - Apply it to the Cloud Run service:
     ```bash
     gcloud beta iap settings set iap-oauth.yaml \
       --resource-type=cloud-run \
       --service=pool-on-solar-dashboard \
       --region=us-west1
     ```

4. **Map a custom domain** (e.g. `pool.steren.fr`):
   ```bash
   gcloud beta run domain-mappings create \
     --service=pool-on-solar-dashboard \
     --domain=pool.steren.fr \
     --region=us-west1
   ```
   Add the printed DNS record (`CNAME` pointing to `ghs.googlehosted.com.`) at your DNS registrar.

## Job Configuration

Grant the Cloud Run Job service account `roles/secretmanager.secretAccessor` and `roles/secretmanager.secretVersionManager` on `tesla-fleet-token` (and `roles/secretmanager.secretAccessor` on `iaqualink-password`).

### Automation Job (`automation/`)

Environment variables:

- `CONFIG_FILE`: location of the pbtxt config file (default: `/app/config/config.pbtxt`, see [automation/config.proto](automation/config.proto))
- `TESLA_CLIENT_ID`: Tesla Fleet API OAuth Client ID
- `TESLA_TOKEN_SECRET_NAME`: Secret Manager resource name (`projects/<PROJECT_ID>/secrets/tesla-fleet-token`); if unset, falls back to `TESLA_TOKEN_FILE` / `config.tesla.cache_file`
- `TESLA_API_BASE_URL`: *(optional)* Regional Fleet API base URL (default NA: `https://fleet-api.prd.na.vn.cloud.tesla.com`; EU: `https://fleet-api.prd.eu.vn.cloud.tesla.com`)
- `ENERGY_SITE_ID`: *(optional)* ID of your Tesla Energy site (saves one `/api/1/products` call per run)
- `IAQUALINK_USER_ID`: email of your iAquaLink account
- `IAQUALINK_PASSWORD`: password of your iAquaLink account (mounted from Secret Manager)
- `DRY_RUN`: *(optional)* set to `true` to log actions without toggling iAquaLink devices

### Historical Analysis Job (`tesla/analysis/`)

Uses the same `TESLA_CLIENT_ID`, `TESLA_TOKEN_SECRET_NAME`, `TESLA_API_BASE_URL`, and optional `ENERGY_SITE_ID` environment variables, plus `TIMEZONE`, `DATASET_ID`, `POWER_TABLE_ID`, `ENERGY_TABLE_ID`, and optional `INITIALIZE_TABLES=true`.
