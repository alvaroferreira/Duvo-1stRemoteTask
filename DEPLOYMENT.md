# Deployment — Korral StoreLink MCP Server

> **MVP-level deployment notes.** This covers the essentials so the deliverable is
> deployable. The full production runbook (exact VPC connector config, CI YAML, log
> retention policy wording) is a **fast-follow**.

## Where it runs

- **Cloud Run** inside **Korral's own GCP project** — co-located with StoreLink so no data
  leaves Korral's tenancy.
- **Serverless VPC Access / VPC connector** so the service can reach StoreLink's **internal**
  endpoint (StoreLink is not public).
- **Logs → Korral Cloud Logging.** The debug stream (JSON on stderr) is picked up
  automatically by Cloud Run. The business **audit log** is written to a file — for Cloud Run,
  point `KORRAL_AUDIT_LOG` at a path on a mounted volume (GCS via Cloud Storage FUSE, or a
  Filestore mount) so it persists and is append-only.
- **Secrets → Korral Secret Manager**, mounted as a file (or env) at deploy time.

**No data leaves Korral's tenancy.** The server runs beside StoreLink; the only open
question is where the *agent* runs (see Day-1 confirmations).

## Secrets & rotation

- The store-key map lives in **Secret Manager**. Mount it as a file and set
  `KORRAL_KEYS_FILE` to the mount path (e.g. `/var/run/korral/keys.json`).
- **Weekly rotation needs no redeploy:** update the secret version; the server's TTL cache
  (`KORRAL_KEY_TTL_SECONDS`, default 300s) re-reads it on the next cache miss.
- **Never bake keys into the image.** The Dockerfile copies code only; keys arrive at runtime.

## Ownership split

- **Duvo owns** the container image + CI: builds it, tags by **git SHA**, pushes to **Korral's
  Artifact Registry**.
- **Korral owns** the infrastructure and **approves deploys** (or grants Duvo a scoped deploy
  service account). Korral owns the VPC, Secret Manager, and the StoreLink endpoint.

## Build & deploy

```bash
# Duvo CI: build + tag by git SHA, push to Korral's Artifact Registry
SHA=$(git rev-parse --short HEAD)
REPO=europe-west3-docker.pkg.dev/korral-prod/duvo/korral-storelink
docker build -t "$REPO:$SHA" .
docker push "$REPO:$SHA"

# Deploy (Korral approves, or runs via scoped deploy SA)
gcloud run deploy korral-storelink \
  --image "$REPO:$SHA" \
  --region europe-west3 \
  --vpc-connector korral-internal \
  --no-allow-unauthenticated \
  --set-secrets /var/run/korral/keys.json=storelink-store-keys:latest \
  --set-env-vars KORRAL_KEYS_FILE=/var/run/korral/keys.json,MAX_REPLENISHMENT_QTY=500
```

## The 11pm fix — roll back / roll forward

Images are immutable and pinned **by digest**, so recovery is one command.

```bash
# ROLL BACK to the previous known-good digest (seconds, no rebuild):
gcloud run services update-traffic korral-storelink \
  --region europe-west3 --to-revisions PREVIOUS_REVISION=100

# or redeploy a specific prior digest:
gcloud run deploy korral-storelink \
  --image "$REPO@sha256:<previous-digest>" --region europe-west3

# ROLL FORWARD: push the fix through CI (new git SHA) and deploy that tag.
```

## Day-1 confirmations to get from Korral IT

1. **Where does the agent run, and how does it reach the MCP server?** ⚠️ **Key open
   question** — this determines whether any data crosses the tenancy boundary. If the agent
   runs outside Korral's tenancy, we need a private path (and to confirm what, if anything,
   transits). This drives the transport choice (stdio vs authenticated HTTP/SSE).
2. **StoreLink internal hostname + network path/firewall** — exact host, port, and which
   subnet/connector is allowed to reach it.
3. **Secret Manager access + rotation mechanism** — secret name, who rotates, and how (so we
   confirm the TTL-reload assumption holds).
4. **Artifact Registry + deploy permissions** — repo path and whether Duvo gets a scoped
   deploy SA or hands images to Korral to deploy.
5. **Log retention / PII policy for the audit log** — retention period and whether the
   business sentences (store names, SKU names, quantities) are acceptable under their policy.
