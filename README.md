# pool-on-solar

Automate the activation / deactivation of a pool cleaning system and lights so they run when excess solar power (and Powerwall charge) is available.

## Requirements

* [Tesla Energy system](https://www.tesla.com/energy) (solar panels, and optionally Powerwall) using the **Tesla Fleet API**
* [Jandy iAquaLink](https://www.jandy.com/en/products/controls/system-components/interfaces/iaqualink) (pool pump controls)
* Google Cloud Project (Cloud Run + Secret Manager)

## Tesla Fleet API Setup (One-Time)

This project authenticates via the **Tesla Fleet API** and persists rotating OAuth2 refresh tokens in **Google Cloud Secret Manager** (with local file fallback for local development).

[tesla/authentication/server.py](tesla/authentication/server.py) is a lightweight Cloud Run service that hosts your public key at `/.well-known/appspecific/com.tesla.3p.public-key.pem`, registers your partner domain, and handles the OAuth2 `/login` and `/callback` flow directly into Secret Manager.

1. **Generate an EC key pair** (`secp256r1` / `prime256v1`):
   ```bash
   openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem
   openssl ec -in private-key.pem -pubout -out public-key.pem
   ```

2. **Create a Secret Manager secret** for the rotating Tesla OAuth token:
   ```bash
   gcloud secrets create tesla-fleet-token --replication-policy=automatic
   ```

3. **Deploy the OAuth helper service to Cloud Run** (grant its service account `roles/secretmanager.secretVersionManager` on `tesla-fleet-token`):
   ```bash
   gcloud run deploy tesla-oauth \
     --source tesla/authentication \
     --region us-west1 \
     --allow-unauthenticated \
     --set-env-vars "TESLA_TOKEN_SECRET_NAME=projects/<PROJECT_ID>/secrets/tesla-fleet-token,TESLA_PUBLIC_KEY_PEM=$(awk '{printf "%s\\n", $0}' public-key.pem)"
   ```
   *(If Tesla rejects the default `*.run.app` origin, map a custom domain to this Cloud Run service.)*

4. **Create a Tesla Developer App** at [developer.tesla.com](https://developer.tesla.com):
   - Set **Allowed Origin** to `https://<your-cloud-run-domain>` and **Allowed Redirect URI** to `https://<your-cloud-run-domain>/callback`.
   - Enable the `Energy Product Information` (`energy_device_data`) scope.
   - Update the Cloud Run service with your `TESLA_CLIENT_ID` and `TESLA_CLIENT_SECRET` (plus optional `SETUP_TOKEN` to gate `/register` and `/login`):
     ```bash
     gcloud run services update tesla-oauth \
       --region us-west1 \
       --update-env-vars "TESLA_CLIENT_ID=<CLIENT_ID>,TESLA_CLIENT_SECRET=<CLIENT_SECRET>"
     ```

5. **Complete partner registration and OAuth login**:
   - Open `https://<your-cloud-run-domain>/` in your browser.
   - Click **1. Register Partner Domain**, then **2. Login with Tesla**.
   - On redirect to `/callback`, the server exchanges the code, writes the tokens to Secret Manager (destroying any older versions), and prints your discovered `ENERGY_SITE_ID`.
   - Lock down the OAuth routes while keeping the public key hosted:
     ```bash
     gcloud run services update tesla-oauth \
       --region us-west1 \
       --update-env-vars "OAUTH_ENABLED=false"
     ```
   *(An interactive CLI alternative is also available in [tesla/authentication/main.py](tesla/authentication/main.py).)*

6. **Grant Secret Manager permissions** to your Cloud Run Job service account so it can read and rotate tokens (destroying older versions so only the latest version is kept):
   - `roles/secretmanager.secretAccessor` (read `latest` version)
   - `roles/secretmanager.secretVersionManager` (add the rotated token version, list older versions, and destroy them so only the latest version remains)

## Configuration

### Automation Job (`automation/`)

Set the following environment variables on the Cloud Run Job:

- `CONFIG_FILE`: location of the pbtxt config file (default: `/app/config/config.pbtxt`, see [automation/config.proto](automation/config.proto))
- `TESLA_CLIENT_ID`: Tesla Fleet API OAuth Client ID
- `TESLA_TOKEN_SECRET_NAME`: Secret Manager resource name (e.g. `projects/<PROJECT_ID>/secrets/tesla-fleet-token`); if unset, falls back to `TESLA_TOKEN_FILE` / `config.tesla.cache_file`
- `TESLA_API_BASE_URL`: *(optional)* Regional Fleet API base URL (defaults to NA: `https://fleet-api.prd.na.vn.cloud.tesla.com`; EU: `https://fleet-api.prd.eu.vn.cloud.tesla.com`)
- `ENERGY_SITE_ID`: *(optional)* ID of your Tesla Energy site (saves one `/api/1/products` call per run)
- `IAQUALINK_USER_ID`: email of your iAquaLink account
- `IAQUALINK_PASSWORD`: password of your iAquaLink account (store in Secret Manager)
- `DRY_RUN`: *(optional)* set to `true` to log actions without toggling iAquaLink devices

### Historical Analysis Job (`tesla/analysis/`)

Uses the same `TESLA_CLIENT_ID`, `TESLA_TOKEN_SECRET_NAME`, `TESLA_API_BASE_URL`, and optional `ENERGY_SITE_ID` environment variables, plus `TIMEZONE`, `DATASET_ID`, `POWER_TABLE_ID`, `ENERGY_TABLE_ID`, and optional `INITIALIZE_TABLES=true`.
