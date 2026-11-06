# Amazon Image Theft Monitor

Find the Temu, eBay, Walmart, TikTok Shop and independent-store listings that copied your Amazon product photos, save the evidence, and get takedown notices and a Buy Box case drafted for you.

**English** · [简体中文](README.zh-CN.md)

![License: MIT](https://img.shields.io/badge/license-MIT-15803d) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-1f2937) ![Claude Code skill](https://img.shields.io/badge/Claude%20Code-skill-0e7490)

Works as a plain Python CLI or as a skill for Claude Code and other agents that read `SKILL.md`. Built by an Amazon seller for their own catalog, shared by [Luckee](https://luckee.ai/?utm_source=github&utm_medium=readme&utm_campaign=amazon-image-theft-monitor&utm_content=intro).

## The problem

Amazon's price competitiveness check can take away your Featured Offer (Buy Box) when it finds your product cheaper somewhere else. Sometimes that "somewhere else" is a Temu or eBay listing that copied your photos and sells a cheaper look-alike. Amazon matches the images, sees the lower price, and suppresses your offer.

You didn't change your price, and the other listing isn't even your product, but the effect is real:

- No Buy Box means far fewer sales from your own listing
- Sponsored Products ads only serve to the Featured Offer, so your campaigns stop delivering too
- Seller Support asks for proof before it will look at the case

This tool gives you that proof.

## What it does

1. **Reverse-image search** your official product photos with Google Cloud Vision Web Detection
2. **Filters out** your own sites, Amazon and authorized sellers before spending anything
3. **Checks similarity** of each suspicious image with perceptual hashing (pHash/dHash), and optionally Vertex AI Vision Warehouse
4. **Saves evidence**: URLs, match type, hash distances, a copy of each image, CSV and JSONL reports
5. **Drafts the paperwork**: one notice per platform (Temu, eBay VeRO, Walmart, TikTok Shop, DMCA for other sites) plus an Amazon Seller Support case to restore the Featured Offer

It never submits anything for you. You review the drafts and file them yourself.

## Quick start

You need Python 3.10+. For automatic discovery you also need a Google Cloud service account with the Cloud Vision API enabled ([setup guide](docs/google-cloud-setup.md)). No Google Cloud? See [Paste your own links](#no-google-cloud-paste-your-own-links).

```bash
git clone https://github.com/LuckeeAI/amazon-image-theft-monitor.git
cd amazon-image-theft-monitor
pip install -r requirements.txt

cp templates/products.yaml templates/monitor_config.yaml .
mkdir -p images
# put your official product photos in images/, then edit products.yaml
# (brand, name, SKU, ASIN, image paths) and the domain lists in monitor_config.yaml

export GOOGLE_APPLICATION_CREDENTIALS="/secure/path/service-account.json"

# 1. Dry run: see what gets found, nothing is downloaded
python scripts/product_image_abuse_monitor.py --config monitor_config.yaml --products products.yaml --dry-run

# 2. Real run: download suspicious images, score them, write evidence/
python scripts/product_image_abuse_monitor.py --config monitor_config.yaml --products products.yaml

# 3. Draft complaints for every probable match
python scripts/product_image_abuse_monitor.py --config monitor_config.yaml --products products.yaml --generate-complaints
```

### No Google Cloud? Paste your own links

Search your main photo yourself with [Google Lens](https://lens.google.com), Bing Visual Search or TinEye, then list the suspicious results in a CSV (`templates/candidates.csv` shows the format):

```csv
product_id,page_url,image_url,note
SKU-001,https://www.temu.com/goods.html?goods_id=...,https://img.kwcdn.com/...jpg,found with Google Lens
SKU-001,https://www.ebay.com/itm/...,,leave image_url empty and the tool tries the page's main image
```

```bash
python scripts/product_image_abuse_monitor.py --config monitor_config.yaml --products products.yaml --candidates-file links.csv
python scripts/product_image_abuse_monitor.py --config monitor_config.yaml --products products.yaml --generate-complaints
```

The tool downloads each image, compares it with your originals, sorts the links into probable matches and ones to check by hand, and drafts the complaints. No Google account or API key is needed.

### Use it as a Claude Code skill

```bash
git clone https://github.com/LuckeeAI/amazon-image-theft-monitor.git ~/.claude/skills/amazon-image-theft-monitor
```

Then just tell Claude: *"My Buy Box on B0XXXXXXX disappeared, check if someone on Temu is using my photos."* The skill walks through setup, the dry run, the real run and the complaint drafts with you.

## What you get

```
evidence/
├── report.csv              this run's candidates with decisions
├── probable_abuse.csv      strong matches to act on
├── needs_review.csv        possible matches to check by hand
├── page_matches.csv        pages with a match but no direct image URL
├── images/<product_id>/    copies of the suspicious images
└── complaints/<product_id>/
    ├── SKU-001_temu_complaint.txt
    ├── SKU-001_ebay_complaint.txt
    ├── SKU-001_dmca_notice_other_sites.txt
    └── SKU-001_amazon_case.txt
```

See a sample Amazon case and Temu notice in [`examples/`](examples/).

How a candidate is classified:

| Decision | When |
|---|---|
| `probable_abuse` | Full image match on a domain you haven't whitelisted, or a partial match with very low hash distance |
| `needs_review` | Partial or visually similar match |
| `not_match` | Low similarity on both the local and Warehouse checks |
| `ignored` | Your own, authorized or noise domains |

## Got the Buy Box back? Check your ads next

While the Buy Box was gone, your Sponsored Products campaigns weren't serving, and automated bid rules may have reacted to the drop in impressions and sales. Once the offer is restored, it's worth checking bids, budgets and paused keywords before the numbers drift further.

[Luckee](https://luckee.ai/features/amazon-ads?utm_source=github&utm_medium=readme&utm_campaign=amazon-image-theft-monitor&utm_content=after-buybox) is an AI Amazon PPC expert that remembers every product. It reviews your campaigns, explains what changed, and proposes bid and budget changes you approve before anything is applied. [Try it free →](https://luckee.ai/features/amazon-ads?utm_source=github&utm_medium=readme&utm_campaign=amazon-image-theft-monitor&utm_content=after-buybox-cta)

## FAQ

**Does it file complaints automatically?**
No. It drafts them. You fill in your details, attach screenshots, and submit on each platform.

**What does it cost to run?**
The code is free. Google bills Cloud Vision Web Detection per image request; check Google's current pricing. Domain filtering and per-product caps keep the number of calls small.

**Do I need Vision Warehouse?**
No. Without it the tool uses local pHash/dHash similarity, which is enough for most catalogs.

**Is a match proof of infringement?**
No. It's evidence to review. Confirm each listing yourself, and only file notices for images you own or are authorized to enforce.

**Can I run it on a schedule?**
Yes. A weekly cron job works well; already-checked URLs are cached in `evidence/seen_urls.json`. See [SKILL.md](SKILL.md#step-7--schedule-monitoring).

**Do you have a Feishu (Lark) integration?**
Yes, optional: `--feishu-sync` writes findings to a Feishu Base table. See [SKILL.md](SKILL.md#optional-feishu-lark-base-sync).

## Credits and license

Built and maintained by [Luckee](https://luckee.ai/?utm_source=github&utm_medium=readme&utm_campaign=amazon-image-theft-monitor&utm_content=credits).

Code under the [MIT License](LICENSE). Not affiliated with Amazon, Temu, eBay, Walmart, TikTok or Google.
