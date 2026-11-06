# Google Cloud setup

The monitor needs one thing to run: a Google Cloud service account that can call the **Cloud Vision API** (Web Detection). Everything else is optional.

| What | Needed for | Required? |
|---|---|---|
| Cloud Vision API + service account | Default reverse-image discovery (Web Detection) | **Yes** |
| Custom Search JSON API key + Programmable Search Engine | `--discovery-provider custom-search` or `both` | Optional |
| Vertex AI Vision Warehouse (corpus, index, endpoint) | Second-opinion similarity check against your official images | Optional |

Cost: Web Detection is billed per image request; check the current Cloud Vision pricing page. The script filters out owned, authorized and ignored domains before downloading anything, and caps validations per product.

## 1. Cloud Vision (required)

1. Create or pick a Google Cloud project and enable billing.
2. Enable the **Cloud Vision API**.
3. Create a service account, grant it access to Cloud Vision, and download a JSON key.
4. Point the script at the key (keep the file outside this repository):

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/secure/path/service-account.json"
```

## 2. Custom Search (optional)

1. Create a search engine at <https://programmablesearchengine.google.com>.
2. Under **Sites to search**, add the marketplaces you want to watch:
   ```
   https://www.walmart.com/*
   https://www.ebay.com/*
   https://www.temu.com/*
   https://www.tiktok.com/*
   https://shop.tiktok.com/*
   ```
3. Turn on **Image search**.
4. Copy the Search Engine ID and create a Custom Search JSON API key:

```bash
export GCS_SEARCH_ENGINE_ID="your-search-engine-id"
export GCS_API_KEY="your-custom-search-api-key"
```

Notes:

- A service account JSON is **not** a Custom Search API key. They are separate credentials.
- Don't put `-site:amazon.com` in the search engine's site box. The script adds negative operators from `search.excluded_query_sites`.
- Custom Search can fail with `This project does not have the access to Custom Search JSON API` even when the API is enabled, if the key belongs to a different project or its restrictions block Custom Search.

## 3. Vision Warehouse (optional)

Cloud Vision Product Search is discontinued, so the script uses **Vertex AI Vision Warehouse** to compare downloaded candidates with your official images. If `VISION_INDEX_ENDPOINT` is unset or `google-cloud-visionai` isn't installed, the script falls back to local pHash/dHash mode with no error.

Setup order: Corpus → import images → Analyze → Index → IndexEndpoint → DeployIndex.

```bash
PROJECT_NUMBER="<YOUR_PROJECT_NUMBER>"
CORPUS_ID="<YOUR_CORPUS_ID>"
INDEX_ID="<YOUR_INDEX_ID>"
INDEX_ENDPOINT_ID="<YOUR_INDEX_ENDPOINT_ID>"
HOST="https://warehouse-visionai.googleapis.com/v1/projects/${PROJECT_NUMBER}/locations/us-central1"
AUTH="Authorization: Bearer $(gcloud auth print-access-token)"
```

1. Enable the **Vision AI API**.
2. Upload your official product images to Cloud Storage and import them. Use the Warehouse host above, not the generic Vision AI host. The manifest is JSONL with camelCase fields, one line per image: `{"assetGcsSource":{"gcsUri":"gs://<YOUR_BUCKET>/path/image.jpg"}}`

   ```bash
   curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
     -d '{"assetsGcsUri":"gs://<YOUR_BUCKET>/manifests/manifest.jsonl"}' \
     "$HOST/corpora/${CORPUS_ID}/assets:import"
   ```
3. Generate embeddings. The method is `:analyze`, not `:analyzeCorpus`. Poll the returned operation until `done: true`.
   ```bash
   curl -X POST -H "$AUTH" -H "Content-Type: application/json" -d '{}' \
     "$HOST/corpora/${CORPUS_ID}:analyze"
   ```
4. Create an index and wait until its state is `CREATED`:
   ```bash
   curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
     -d '{"displayName":"Image Theft Monitor Index"}' \
     "$HOST/corpora/${CORPUS_ID}/indexes?index_id=${INDEX_ID}"
   ```
5. Create an index endpoint. The display name must be **32 characters or fewer**, or the call returns `INVALID_ARGUMENT`.
   ```bash
   curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
     -d '{"displayName":"Image Theft Endpoint"}' \
     "$HOST/indexEndpoints?index_endpoint_id=${INDEX_ENDPOINT_ID}"
   ```
6. Deploy the index. This can take 20+ minutes; poll until `done: true`.
   ```bash
   curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
     -d "{\"deployedIndex\":{\"index\":\"projects/${PROJECT_NUMBER}/locations/us-central1/corpora/${CORPUS_ID}/indexes/${INDEX_ID}\"}}" \
     "$HOST/indexEndpoints/${INDEX_ENDPOINT_ID}:deployIndex"
   ```
7. Export the endpoint:
   ```bash
   export VISION_INDEX_ENDPOINT="projects/${PROJECT_NUMBER}/locations/us-central1/indexEndpoints/${INDEX_ENDPOINT_ID}"
   ```

Known quirk: `warehouse-visionai.googleapis.com` may not show up in `gcloud services list` even when Warehouse REST calls work, as long as `visionai.googleapis.com` is enabled. Check the corpus, index and endpoint with real REST calls instead.

## Read-only audit

To check a project without changing anything:

```bash
gcloud config get-value project
for svc in vision.googleapis.com customsearch.googleapis.com visionai.googleapis.com storage.googleapis.com; do
  gcloud services list --enabled --filter="config.name=$svc" --format='value(config.name)'
done
```

If the API Keys or Cloud Billing APIs are disabled, report "unable to confirm" for key restrictions or billing rather than enabling them during an audit.
