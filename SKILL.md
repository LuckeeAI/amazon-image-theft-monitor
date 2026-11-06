---
name: amazon-image-theft-monitor
version: 2.4.0
license: MIT
description: "Find listings on Temu, eBay, Walmart, TikTok Shop and independent websites that reuse an Amazon seller's product photos, collect evidence, and draft complaint letters plus an Amazon Buy Box (Featured Offer) case. Uses Google Cloud Vision Web Detection for reverse-image discovery, local pHash/dHash checks, and optional Vertex AI Vision Warehouse validation. Use when a seller mentions a lost or suppressed Buy Box, price matching to a cheaper external listing, stolen product images, image theft on Temu/eBay/TikTok/Walmart, or asks to monitor or report unauthorized use of their product photos. Also trigger for 产品盗图、盗图监控、购物车丢失、亚马逊丢购物车、Temu侵权、eBay盗图、以图搜图采集链接、抢购物车申诉."
metadata:
  short-description: "Find stolen Amazon product images on other marketplaces, collect evidence, draft takedown notices and a Buy Box case"
  compatibility:
    - python
    - claude-code
  tags:
    - amazon-seller
    - buy-box
    - image-theft
    - reverse-image-search
    - google-cloud-vision
    - dmca
    - temu
    - ebay-vero
---

# Amazon Image Theft Monitor

## Why this skill exists

Amazon's price competitiveness check can suppress a seller's Featured Offer (Buy Box) when it finds the same product cheaper elsewhere. The edge case that hurts brand owners: a Temu, eBay, Walmart or TikTok Shop seller copies your product photos and lists a cheaper look-alike. Amazon matches the images, sees a lower price, and your Buy Box disappears, along with your Sponsored Products ads, which only serve to the Featured Offer.

This skill finds those listings, saves evidence, and drafts the complaints, so the seller can act before (or right after) the Buy Box is lost.

## What Claude should do

### Step 1 — Understand the situation

Ask the seller:

- Which products to monitor (brand, product name, SKU, ASIN)
- Where their original product images are saved locally
- Whether the Buy Box is already lost, or this is preventive monitoring
- Whether they already have a Google Cloud project with the Cloud Vision API enabled. If not, they can still use the tool: search the main photo with Google Lens, Bing Visual Search or TinEye, put the suspicious links in a CSV (`templates/candidates.csv`), and run with `--candidates-file`. Skip the Google Cloud prerequisites and the dry run in that case.

If `products.yaml` and `monitor_config.yaml` exist in the working directory, read them. If not, copy them from `templates/` and fill them in with the seller.

### Step 2 — Check prerequisites

- `products.yaml` has at least one product whose `reference_images` point to real files
- `monitor_config.yaml` has `owned_domains` and `authorized_domains` filled in
- `GOOGLE_APPLICATION_CREDENTIALS` points to a service account JSON with Cloud Vision access (needed for the default Web Detection discovery)
- Only for `--discovery-provider custom-search` or `both`: `GCS_API_KEY` and `GCS_SEARCH_ENGINE_ID`
- Optional: `VISION_INDEX_ENDPOINT` for Vision Warehouse validation. Without it the script runs in local pHash/dHash mode, which is enough for most sellers.

Full Google Cloud setup: `docs/google-cloud-setup.md`.

Never print service account JSON, private keys, API keys or access tokens.

Without Google Cloud, run on hand-collected links instead (columns `product_id`, `page_url`, `image_url`, `note`; `image_url` may be empty, then the page's og:image is tried):

```bash
python scripts/product_image_abuse_monitor.py \
  --config monitor_config.yaml --products products.yaml --candidates-file links.csv
```

Manual links are judged on local image similarity only: high → `probable_abuse`, anything else → `needs_review`. Links whose image could not be downloaded also land in `needs_review` (reason `image_download_failed` or `no_image_url`) so the seller can check them by hand.

### Step 3 — Dry run first

```bash
python scripts/product_image_abuse_monitor.py \
  --config monitor_config.yaml --products products.yaml --dry-run
```

The dry run calls Web Detection but downloads nothing. Help the seller read the output: do candidates show up as `full_match`, `partial_match` and `visually_similar`? Are their own domains and Amazon marked as ignored? If results are noisy, tune `domains.owned_domains`, `authorized_domains` and `ignored_domains` first.

### Step 4 — Real run

```bash
# Default: Cloud Vision Web Detection discovery
python scripts/product_image_abuse_monitor.py \
  --config monitor_config.yaml --products products.yaml

# One product only
python scripts/product_image_abuse_monitor.py \
  --config monitor_config.yaml --products products.yaml --product-id SKU-001

# Use an official image stored in Google Cloud Storage as the query image
python scripts/product_image_abuse_monitor.py \
  --config monitor_config.yaml --products products.yaml \
  --reference-gcs-uri gs://<YOUR_BUCKET>/official/sku-001-front.jpg

# Text-query discovery via Google Custom Search (legacy fallback)
python scripts/product_image_abuse_monitor.py \
  --config monitor_config.yaml --products products.yaml --discovery-provider custom-search
```

### Step 5 — Read the results

Outputs in `evidence/`:

| File | What it holds |
|---|---|
| `report.jsonl` | Append-only log of every candidate ever checked |
| `report.csv` | This run's candidates |
| `probable_abuse.csv` | Strong matches on non-whitelisted domains |
| `needs_review.csv` | Possible matches a person should check |
| `page_matches.csv` | Pages that contain a match but no direct image URL |
| `images/<product_id>/` | Downloaded copies of the suspicious images |

| Decision | Meaning | Seller action |
|---|---|---|
| `probable_abuse` | Full image match, or partial match with very low hash distance | Draft complaints now |
| `needs_review` | Partial or visually similar match | Open the page and decide |
| `not_match` | Low similarity on both local and Warehouse checks | Skip |
| `ignored` | Owned, authorized or ignored domain | Nothing to do |
| `skipped_due_to_budget_limit` | Per-product validation cap reached | Raise `max_candidates_for_warehouse_per_product` |

Treat Web Detection `score` with care: image candidates often come back with `0.0`. Rank by `match_type` (`full_match` > `partial_match` > `visually_similar`) and let the hash distances and a human decide. A similarity score is not legal proof.

### Step 6 — Draft complaints

If there are `probable_abuse` findings, offer to draft complaints:

```bash
python scripts/product_image_abuse_monitor.py \
  --config monitor_config.yaml --products products.yaml --generate-complaints
```

This writes to `evidence/complaints/<product_id>/`:

- `<id>_temu_complaint.txt`, `<id>_ebay_complaint.txt`, `<id>_walmart_complaint.txt`, `<id>_tiktok_complaint.txt`: one notice per platform with all infringing URLs
- `<id>_dmca_notice_other_sites.txt`: DMCA-style notice for independent websites
- `<id>_amazon_case.txt`: Seller Support case asking Amazon to drop the external price reference and restore the Featured Offer

Tell the seller to:

1. Fill in the `[BRACKETS]` (name, company, contact details)
2. Attach screenshots of each infringing listing and of their original listing
3. File the platform complaints first and note the complaint IDs
4. Add those IDs to the Amazon case, then open it

Remind them to confirm they own the images (or are authorized by the owner) before submitting. Nothing is sent automatically.

### Step 7 — Schedule monitoring

```bash
# Weekly scan, Mondays 09:00
0 9 * * 1 cd /path/to/project && python scripts/product_image_abuse_monitor.py \
  --config monitor_config.yaml --products products.yaml >> evidence/weekly.log 2>&1
```

`evidence/seen_urls.json` caches processed URLs, so repeat runs only check new candidates. Do not delete it between runs.

## Discovery pipeline

```
Cloud Vision Web Detection (reference image)
        ↓
Candidates: full_match / partial_match / visually_similar / page_match
        ↓
Domain policy (owned / authorized / ignored → ignored, no budget spent)
        ↓
Download suspicious images only
        ↓
Local pHash + dHash similarity
        ↓
Vision Warehouse validation (optional, fail-soft)
        ↓
evidence/*.csv + report.jsonl → human review → complaint drafts
```

## Domain lists

| Key | Put here | Effect | Careful |
|---|---|---|---|
| `owned_domains` | Your brand site, your Shopify store, your CDN | Always `ignored` | Never add a marketplace here |
| `authorized_domains` | Amazon domains, approved distributors and retailers | Always `ignored` | A broad domain here can hide real theft |
| `ignored_domains` | Google, Pinterest, Facebook, Instagram, image CDNs | Always `ignored` | Don't add suspicious seller domains just to cut noise |

Start strict. Add a domain to `authorized_domains` only after confirming that channel may sell or show the product.

## Cost control

Domain filtering happens before any download or Warehouse call. Budget knobs in `monitor_config.yaml` under `search:` — `max_candidates_for_warehouse_per_product`, `max_pages_per_query`, `desired_filtered_candidates_per_query`, `delay_between_requests_seconds`.

## Optional: Feishu (Lark) Base sync

`--feishu-sync` writes `probable_abuse` and `needs_review` rows to a Feishu Base table through `lark-cli`, deduplicated by `product_id + source_page_url + candidate_image_url`. `--feishu-sync-dry-run` checks fields and dedupe keys without writing. Configure the `feishu:` block in `monitor_config.yaml` first.

## Common pitfalls

| Problem | Fix |
|---|---|
| Web Detection returns nothing | Check `GOOGLE_APPLICATION_CREDENTIALS`; on a transient `503`, retry once |
| `-site:amazon.com` typed into the Custom Search site box | Put it in `search.excluded_query_sites` instead |
| Vision Warehouse errors | Not configured; the script falls back to local hash mode |
| Only one reference image | Add front, side, lifestyle and packaging shots for better recall |
| Treating a match score as proof | Always review by hand before filing a takedown |

## Safe use

This skill finds and documents possible image misuse. It does not send takedown notices, log into third-party accounts, bypass site protections or scrape private content. Complaint letters are drafts that the seller reviews and submits.
