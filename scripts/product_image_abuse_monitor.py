#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse
from collections import Counter

import imagehash
import requests
import tldextract
import yaml
from PIL import Image, UnidentifiedImageError
from google.cloud import vision
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from google.cloud import visionai_v1 as _visionai
    _VISIONAI_AVAILABLE = True
except ImportError:
    _visionai = None  # type: ignore
    _VISIONAI_AVAILABLE = False


@dataclass
class Product:
    product_id: str
    brand: str
    product_name: str
    sku: str
    reference_images: list[str]
    asin: str | None = None


@dataclass
class Candidate:
    candidate_image_url: str
    source_page_url: str
    source_domain: str
    match_type: str
    score: float | None
    product_id: str
    discovery_method: str = "web_detection"
    query: str = ""
    title: str | None = None
    snippet: str | None = None


@dataclass
class PageMatch:
    timestamp_utc: str
    product_id: str
    source_page_url: str
    source_domain: str
    page_title: str | None
    web_detection_score: float | None
    reason: str


@dataclass
class Evidence:
    timestamp_utc: str
    product_id: str
    candidate_image_url: str
    source_page_url: str
    source_domain: str
    match_type: str
    web_detection_score: float | None
    phash_distance: int | None
    dhash_distance: int | None
    local_similarity_decision: str
    warehouse_relevance: float | None
    matched_asset: str | None
    decision: str
    reason: str
    local_image_path: str | None


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_products(path: str | Path) -> list[Product]:
    data = load_yaml(path)
    products: list[Product] = []
    for item in data.get("products", []):
        products.append(
            Product(
                product_id=str(item["product_id"]),
                brand=str(item.get("brand", "")),
                product_name=str(item.get("product_name", "")),
                sku=str(item.get("sku", "")),
                reference_images=list(item.get("reference_images", [])),
                asin=str(item["asin"]) if item.get("asin") else None,
            )
        )
    return products


def env_value(name: str | None, default: str | None = None) -> str | None:
    if not name:
        return default
    return os.getenv(name, default)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+$", "", parsed.path)
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def registered_domain(url_or_domain: str) -> str:
    raw = (url_or_domain or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).netloc or raw
    ext = tldextract.extract(host)
    if not ext.domain or not ext.suffix:
        return host.lower()
    return f"{ext.domain}.{ext.suffix}".lower()


def source_domain(url: str) -> str:
    return registered_domain(url)


def domain_matches(domain: str, patterns: Iterable[str]) -> bool:
    d = registered_domain(domain)
    for p in patterns:
        rp = registered_domain(p)
        if rp and (d == rp or d.endswith("." + rp)):
            return True
    return False


def domain_policy(source_url_or_domain: str, cfg: dict[str, Any]) -> tuple[str, str]:
    d = source_domain(source_url_or_domain)
    domains_cfg = cfg.get("domains", {})
    if domain_matches(d, domains_cfg.get("owned_domains", [])):
        return "ignored", "owned_domain"
    if domain_matches(d, domains_cfg.get("authorized_domains", [])):
        return "ignored", "authorized_domain"
    if domain_matches(d, domains_cfg.get("ignored_domains", [])):
        return "ignored", "ignored_domain"
    return "keep", "keep"


def should_skip_source(source_url: str, cfg: dict[str, Any]) -> tuple[bool, str]:
    policy, reason = domain_policy(source_url, cfg)
    return policy == "ignored", reason


def classify_platform(domain: str, cfg: dict[str, Any]) -> str:
    marketplace_map = cfg.get("domains", {}).get("target_marketplace_domains", {})
    for platform, domains in marketplace_map.items():
        if domain_matches(domain, domains):
            return str(platform)
    return "independent_or_other"


def build_negative_site_clause(cfg: dict[str, Any]) -> str:
    excluded = cfg.get("search", {}).get("excluded_query_sites", [])
    parts: list[str] = []
    for d in excluded:
        clean = registered_domain(d)
        if clean:
            parts.append(f"-site:{clean}")
    return " ".join(parts)


def render_query(template: str, product: Product, cfg: dict[str, Any]) -> str:
    q = template.format(
        brand=product.brand,
        product_name=product.product_name,
        sku=product.sku,
        product_id=product.product_id,
    ).strip()
    negative_sites = build_negative_site_clause(cfg)
    if negative_sites:
        q = f"{q} {negative_sites}"
    return q


def custom_search_page(service: Any, q: str, cx: str, num: int, start: int, product: Product) -> list[Candidate]:
    response = (
        service.cse()
        .list(q=q, cx=cx, searchType="image", num=min(num, 10), start=start, safe="off")
        .execute()
    )
    out: list[Candidate] = []
    for item in response.get("items", []):
        image_url = item.get("link")
        image_info = item.get("image", {}) or {}
        source_url = image_info.get("contextLink") or item.get("displayLink") or ""
        if not image_url or not source_url:
            continue
        source = normalize_url(source_url)
        out.append(
            Candidate(
                candidate_image_url=normalize_url(image_url),
                source_page_url=source,
                source_domain=source_domain(source),
                match_type="custom_search",
                score=None,
                product_id=product.product_id,
                discovery_method="custom_search_image",
                query=q,
                title=item.get("title"),
                snippet=item.get("snippet"),
            )
        )
    return out


def custom_search_candidates(product: Product, cfg: dict[str, Any]) -> tuple[list[Candidate], list[PageMatch]]:
    search_cfg = cfg.get("search", {})
    google_cfg = cfg.get("google", {})
    api_key = env_value(google_cfg.get("custom_search_api_key_env"))
    cx = env_value(google_cfg.get("custom_search_engine_id_env"))
    if not api_key or not cx:
        raise RuntimeError("Missing Custom Search credentials. Set GCS_API_KEY and GCS_SEARCH_ENGINE_ID.")
    service = build("customsearch", "v1", developerKey=api_key)
    results_per_query = int(search_cfg.get("results_per_query", 10))
    max_pages = int(search_cfg.get("max_pages_per_query", 1))
    desired_filtered = int(search_cfg.get("desired_filtered_candidates_per_query", 5))
    query_templates = list(search_cfg.get("queries", []))
    query_templates.extend(search_cfg.get("target_platform_queries", {}).values())
    candidates: list[Candidate] = []
    for template in query_templates:
        q = render_query(template, product, cfg)
        kept_for_query = 0
        for page in range(max_pages):
            start = 1 + page * results_per_query
            try:
                page_candidates = custom_search_page(service, q, cx, results_per_query, start, product)
            except HttpError as e:
                print(f"[WARN] Custom Search failed for query={q!r}: {e}", file=sys.stderr)
                break
            for candidate in page_candidates:
                skip, _ = should_skip_source(candidate.source_page_url, cfg)
                if not skip:
                    kept_for_query += 1
                candidates.append(candidate)
            if kept_for_query >= desired_filtered:
                break
            time.sleep(float(search_cfg.get("delay_between_requests_seconds", 1.0)))
    return candidates, []


def _vision_image_from_reference(reference: str) -> vision.Image | None:
    if reference.startswith("gs://"):
        return vision.Image(source=vision.ImageSource(image_uri=reference))
    path = Path(reference)
    if not path.exists():
        print(f"[WARN] Reference image not found for Web Detection: {reference}", file=sys.stderr)
        return None
    return vision.Image(content=path.read_bytes())


def _web_image_score(img: Any) -> float | None:
    score = getattr(img, "score", None)
    try:
        return float(score) if score is not None else None
    except Exception:
        return None


def web_detection_candidates(
    product: Product,
    cfg: dict[str, Any],
    reference_gcs_uri: str | None = None,
) -> tuple[list[Candidate], list[PageMatch]]:
    client = vision.ImageAnnotatorClient()
    candidates: list[Candidate] = []
    page_matches: list[PageMatch] = []
    refs = [reference_gcs_uri] if reference_gcs_uri else product.reference_images

    for ref in refs:
        if not ref:
            continue
        image = _vision_image_from_reference(ref)
        if image is None:
            continue
        try:
            response = client.web_detection(image=image)
        except Exception as e:
            print(f"[WARN] Web Detection failed for {ref}: {e}", file=sys.stderr)
            continue
        if response.error.message:
            print(f"[WARN] Web Detection API error: {response.error.message}", file=sys.stderr)
            continue

        web = response.web_detection
        page_to_url: dict[str, str] = {}
        for page in web.pages_with_matching_images:
            page_url = normalize_url(getattr(page, "url", "") or "")
            if not page_url:
                continue
            page_domain = source_domain(page_url)
            page_score = _web_image_score(page)
            page_title = getattr(page, "page_title", None)
            page_matches.append(
                PageMatch(
                    timestamp_utc=datetime.now(timezone.utc).isoformat(),
                    product_id=product.product_id,
                    source_page_url=page_url,
                    source_domain=page_domain,
                    page_title=page_title,
                    web_detection_score=page_score,
                    reason="page_match_without_direct_candidate_image_url",
                )
            )
            for img in page.full_matching_images:
                image_url = normalize_url(getattr(img, "url", "") or "")
                if image_url:
                    page_to_url[image_url] = page_url
                    candidates.append(Candidate(image_url, page_url, page_domain, "full_match", _web_image_score(img), product.product_id, "web_detection", f"web_detection:{ref}", page_title))
            for img in page.partial_matching_images:
                image_url = normalize_url(getattr(img, "url", "") or "")
                if image_url:
                    page_to_url[image_url] = page_url
                    candidates.append(Candidate(image_url, page_url, page_domain, "partial_match", _web_image_score(img), product.product_id, "web_detection", f"web_detection:{ref}", page_title))

        for img in web.full_matching_images:
            image_url = normalize_url(getattr(img, "url", "") or "")
            if image_url:
                page_url = page_to_url.get(image_url, "")
                domain = source_domain(page_url or image_url)
                candidates.append(Candidate(image_url, page_url, domain, "full_match", _web_image_score(img), product.product_id, "web_detection", f"web_detection:{ref}"))
        for img in web.partial_matching_images:
            image_url = normalize_url(getattr(img, "url", "") or "")
            if image_url:
                page_url = page_to_url.get(image_url, "")
                domain = source_domain(page_url or image_url)
                candidates.append(Candidate(image_url, page_url, domain, "partial_match", _web_image_score(img), product.product_id, "web_detection", f"web_detection:{ref}"))
        for img in web.visually_similar_images:
            image_url = normalize_url(getattr(img, "url", "") or "")
            if image_url:
                domain = source_domain(image_url)
                candidates.append(Candidate(image_url, "", domain, "visually_similar", _web_image_score(img), product.product_id, "web_detection", f"web_detection:{ref}"))

    return candidates, page_matches


def dedupe_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    priority = {"full_match": 0, "partial_match": 1, "visually_similar": 2, "custom_search": 3}
    best: dict[tuple[str, str], Candidate] = {}
    for c in candidates:
        key = (normalize_url(c.candidate_image_url), normalize_url(c.source_page_url))
        existing = best.get(key)
        if existing is None or priority.get(c.match_type, 99) < priority.get(existing.match_type, 99):
            best[key] = c
    return list(best.values())


def load_seen_cache(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


def save_seen_cache(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def candidate_cache_key(candidate: Candidate, product_id: str) -> str:
    raw = f"{product_id}|{normalize_url(candidate.candidate_image_url)}|{normalize_url(candidate.source_page_url)}|{candidate.match_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_filename_from_url(url: str, default_ext: str = ".jpg") -> str:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    if not ext or len(ext) > 8:
        ext = default_ext
    return h + ext


def download_image(candidate: Candidate, cfg: dict[str, Any], product: Product) -> Path | None:
    out_dir = Path(cfg.get("output", {}).get("evidence_dir", "evidence")) / "images" / product.product_id
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": cfg.get("search", {}).get("user_agent", "Mozilla/5.0")}
    timeout = int(cfg.get("search", {}).get("download_timeout_seconds", 20))
    try:
        resp = requests.get(candidate.candidate_image_url, headers=headers, timeout=timeout, stream=True)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] Download failed: {candidate.candidate_image_url} ({e})", file=sys.stderr)
        return None
    content_type = resp.headers.get("Content-Type", "").split(";")[0].lower()
    if content_type and not content_type.startswith("image/"):
        print(f"[WARN] Skipping non-image content: {candidate.candidate_image_url} ({content_type})", file=sys.stderr)
        return None
    ext = mimetypes.guess_extension(content_type) if content_type else None
    path = out_dir / safe_filename_from_url(candidate.candidate_image_url, ext or ".jpg")
    try:
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    except Exception as e:
        print(f"[WARN] Could not save image: {path} ({e})", file=sys.stderr)
        return None
    try:
        with Image.open(path) as img:
            img.verify()
    except Exception:
        print(f"[WARN] Invalid image downloaded: {candidate.candidate_image_url}", file=sys.stderr)
        try:
            path.unlink()
        except Exception:
            pass
        return None
    return path


def compute_hash(path: str | Path, kind: str) -> imagehash.ImageHash | None:
    try:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            return imagehash.phash(rgb) if kind == "phash" else imagehash.dhash(rgb)
    except (UnidentifiedImageError, OSError):
        return None


def reference_hashes(product: Product, kind: str) -> list[imagehash.ImageHash]:
    hashes: list[imagehash.ImageHash] = []
    for p in product.reference_images:
        if str(p).startswith("gs://"):
            continue
        h = compute_hash(p, kind)
        if h is not None:
            hashes.append(h)
        else:
            print(f"[WARN] Could not compute {kind} for reference image: {p}", file=sys.stderr)
    return hashes


def min_hash_distance(candidate_path: Path, ref_hashes: list[imagehash.ImageHash], kind: str) -> int | None:
    candidate_hash = compute_hash(candidate_path, kind)
    if candidate_hash is None or not ref_hashes:
        return None
    return min(candidate_hash - ref_hash for ref_hash in ref_hashes)


def local_similarity_decision(phash_distance: int | None, dhash_distance: int | None, cfg: dict[str, Any]) -> str:
    val = cfg.get("validation", {})
    p_strict = int(val.get("phash_max_distance_strict", 8))
    p_review = int(val.get("phash_max_distance_review", 14))
    d_strict = int(val.get("dhash_max_distance_strict", 8))
    d_review = int(val.get("dhash_max_distance_review", 14))
    if (phash_distance is not None and phash_distance <= p_strict) or (dhash_distance is not None and dhash_distance <= d_strict):
        return "high"
    if (phash_distance is not None and phash_distance <= p_review) or (dhash_distance is not None and dhash_distance <= d_review):
        return "medium"
    if phash_distance is None and dhash_distance is None:
        return "unknown"
    return "low"


def is_warehouse_configured(cfg: dict[str, Any]) -> bool:
    if not _VISIONAI_AVAILABLE:
        return False
    return bool(env_value(cfg.get("google", {}).get("index_endpoint_env")))


def warehouse_search_validate(image_path: Path, cfg: dict[str, Any]) -> tuple[float | None, str | None, str | None]:
    index_endpoint = env_value(cfg.get("google", {}).get("index_endpoint_env"))
    if not index_endpoint:
        return None, None, "warehouse_not_configured"
    client = _visionai.WarehouseClient()
    request = _visionai.SearchIndexEndpointRequest(
        index_endpoint=index_endpoint,
        image_query=_visionai.ImageQuery(input_image=image_path.read_bytes()),
    )
    try:
        response = client.search_index_endpoint(request)
    except Exception as e:
        return None, None, f"warehouse_error={type(e).__name__}: {str(e)[:200]}"
    items = list(response.search_result_items)
    if not items:
        return None, None, "warehouse_no_result"
    best = items[0]
    return (float(best.relevance) if best.relevance else None, str(best.asset) if best.asset else None, None)


def decide(
    candidate: Candidate,
    domain_reason: str,
    local_decision: str,
    warehouse_score: float | None,
    warehouse_reason: str | None,
    cfg: dict[str, Any],
) -> tuple[str, str]:
    reasons: list[str] = []
    if domain_reason != "keep":
        return "ignored", domain_reason
    if local_decision != "unknown":
        reasons.append(f"local_similarity={local_decision}")
    if warehouse_reason:
        reasons.append(warehouse_reason)
    val = cfg.get("validation", {})
    strict_score = float(val.get("warehouse_min_score_strict", 0.75))
    review_score = float(val.get("warehouse_min_score_review", 0.50))

    if candidate.match_type == "full_match":
        return "probable_abuse", ";".join(["web_detection_full_match", *reasons])
    if candidate.match_type == "partial_match" and local_decision == "high":
        return "probable_abuse", ";".join(["web_detection_partial_match_local_high", *reasons])
    if warehouse_score is not None and warehouse_score >= strict_score:
        return "probable_abuse", ";".join([f"warehouse_similarity_score={warehouse_score:.4f}", *reasons])
    if candidate.match_type == "partial_match":
        return "needs_review", ";".join(["web_detection_partial_match", *reasons])
    if candidate.match_type == "visually_similar":
        return "needs_review", ";".join(["web_detection_visually_similar", *reasons])
    if warehouse_score is not None and warehouse_score >= review_score:
        return "needs_review", ";".join([f"possible_warehouse_match_score={warehouse_score:.4f}", *reasons])
    if warehouse_score is not None and warehouse_score < review_score and local_decision == "low":
        return "not_match", ";".join(["warehouse_low_similarity_and_local_low", *reasons])
    if candidate.match_type == "custom_search" and local_decision in {"high", "medium"}:
        return "probable_abuse" if local_decision == "high" else "needs_review", ";".join([f"custom_search_local_{local_decision}", *reasons])
    return "needs_review", ";".join(["insufficient_evidence_fail_soft", *reasons])


def append_jsonl(path: Path, evidence: Evidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(evidence), ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(asdict(rows[0]).keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def evidence_summary(rows: list[Evidence], report_csv: Path, page_matches_csv: Path) -> dict[str, Any]:
    counts = Counter(row.decision for row in rows)
    suspicious = [row for row in rows if row.decision in {"probable_abuse", "needs_review"}]
    top_domains = Counter(row.source_domain for row in suspicious if row.source_domain).most_common(10)
    return {
        "total_candidates": len(rows),
        "ignored_count": counts.get("ignored", 0),
        "probable_abuse_count": counts.get("probable_abuse", 0),
        "needs_review_count": counts.get("needs_review", 0),
        "top_suspicious_domains": [{"domain": domain, "count": count} for domain, count in top_domains],
        "report_csv": str(report_csv),
        "page_matches_csv": str(page_matches_csv),
    }


def print_evidence_summary(summary: dict[str, Any]) -> None:
    print("[SUMMARY] " + json.dumps(summary, ensure_ascii=False))
    print(f"[SUMMARY] total_candidates={summary['total_candidates']}")
    print(f"[SUMMARY] ignored_count={summary['ignored_count']}")
    print(f"[SUMMARY] probable_abuse_count={summary['probable_abuse_count']}")
    print(f"[SUMMARY] needs_review_count={summary['needs_review_count']}")
    print(f"[SUMMARY] top_suspicious_domains={summary['top_suspicious_domains']}")
    print(f"[SUMMARY] report_csv={summary['report_csv']}")
    print(f"[SUMMARY] page_matches_csv={summary['page_matches_csv']}")


BASE_FIELDS = [
    "timestamp_utc",
    "product_id",
    "decision",
    "source_domain",
    "source_page_url",
    "candidate_image_url",
    "match_type",
    "phash_distance",
    "dhash_distance",
    "local_similarity_decision",
    "warehouse_relevance",
    "matched_asset",
    "reason",
    "local_image_path",
    "status",
    "notes",
]


def run_lark_cli(args: list[str]) -> dict[str, Any]:
    cmd = ["lark-cli", *args]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "lark-cli failed")[-1200:])
    text = proc.stdout.strip()
    start = text.find("{")
    if start < 0:
        raise RuntimeError("lark-cli returned non-JSON output")
    return json.loads(text[start:])


def load_csv_rows(path: Path, expected_decision: str) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("decision") == expected_decision]


def dedupe_key(row: dict[str, Any]) -> str:
    return "|".join([row.get("product_id", ""), row.get("source_page_url", ""), row.get("candidate_image_url", "")])


def row_to_base_values(row: dict[str, Any]) -> list[Any]:
    def num(name: str) -> Any:
        v = row.get(name, "")
        if v in (None, ""):
            return ""
        try:
            return float(v)
        except Exception:
            return v
    return [
        row.get("timestamp_utc", ""),
        row.get("product_id", ""),
        row.get("decision", ""),
        row.get("source_domain", ""),
        row.get("source_page_url", ""),
        row.get("candidate_image_url", ""),
        row.get("match_type", ""),
        num("phash_distance"),
        num("dhash_distance"),
        row.get("local_similarity_decision", ""),
        num("warehouse_relevance"),
        row.get("matched_asset", ""),
        row.get("reason", ""),
        row.get("local_image_path", ""),
        "new",
        "",
    ]


def ensure_base_fields(base_token: str, table_id: str, create_missing: bool = False) -> tuple[set[str], list[str]]:
    resp = run_lark_cli(["base", "+field-list", "--base-token", base_token, "--table-id", table_id])
    fields = {f.get("name", "") for f in resp.get("data", {}).get("fields", [])}
    missing = [f for f in BASE_FIELDS if f not in fields]
    if create_missing and missing:
        field_types = {
            "phash_distance": "number",
            "dhash_distance": "number",
            "warehouse_relevance": "number",
        }
        for name in missing:
            ftype = field_types.get(name, "text")
            payload = {"name": name, "type": ftype}
            run_lark_cli(["base", "+field-create", "--base-token", base_token, "--table-id", table_id, "--json", json.dumps(payload, ensure_ascii=False)])
        resp = run_lark_cli(["base", "+field-list", "--base-token", base_token, "--table-id", table_id])
        fields = {f.get("name", "") for f in resp.get("data", {}).get("fields", [])}
        missing = [f for f in BASE_FIELDS if f not in fields]
    return fields, missing


def existing_base_keys(base_token: str, table_id: str) -> set[str]:
    keys: set[str] = set()
    offset = 0
    while True:
        resp = run_lark_cli([
            "base", "+record-list", "--base-token", base_token, "--table-id", table_id,
            "--field-id", "product_id", "--field-id", "source_page_url", "--field-id", "candidate_image_url",
            "--field-id", "source_url", "--field-id", "image_url",
            "--limit", "200", "--offset", str(offset), "--format", "json",
        ])
        data = resp.get("data", {})
        fields = data.get("fields", [])
        for record in data.get("data", []):
            row = dict(zip(fields, record))
            product_id = str(row.get("product_id", "") or "")
            source_page = str(row.get("source_page_url", row.get("source_url", "")) or "")
            image_url = str(row.get("candidate_image_url", row.get("image_url", "")) or "")
            if product_id and (source_page or image_url):
                keys.add("|".join([product_id, source_page, image_url]))
        if not data.get("has_more"):
            break
        offset += 200
    return keys


def write_rows_to_base(base_token: str, table_id: str, rows: list[dict[str, Any]], dry_run: bool, limit: int | None = None) -> tuple[int, int, list[dict[str, Any]]]:
    _, missing = ensure_base_fields(base_token, table_id, create_missing=not dry_run)
    if missing:
        raise RuntimeError("Missing Base fields: " + ", ".join(missing))
    existing = existing_base_keys(base_token, table_id)
    seen_batch: set[str] = set()
    to_insert: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        key = dedupe_key(row)
        if key in existing or key in seen_batch:
            skipped += 1
            continue
        seen_batch.add(key)
        to_insert.append(row)
    if limit is not None:
        skipped += max(0, len(to_insert) - limit)
        to_insert = to_insert[:limit]
    if dry_run or not to_insert:
        return 0, skipped, to_insert
    payload = {"fields": BASE_FIELDS, "rows": [row_to_base_values(r) for r in to_insert]}
    run_lark_cli(["base", "+record-batch-create", "--base-token", base_token, "--table-id", table_id, "--json", json.dumps(payload, ensure_ascii=False)])
    return len(to_insert), skipped, to_insert


def feishu_sync_from_evidence(cfg: dict[str, Any], dry_run: bool = False, limit_probable: int | None = None, send_alerts: bool = False, alert_limit: int | None = None) -> dict[str, Any]:
    feishu_cfg = cfg.get("feishu", {})
    base_token = feishu_cfg.get("base_token") or os.getenv(feishu_cfg.get("base_token_env", ""), "")
    table_id = feishu_cfg.get("table_id") or os.getenv(feishu_cfg.get("table_id_env", ""), "")
    if not base_token or not table_id:
        raise RuntimeError("Missing feishu.base_token or feishu.table_id")
    output = cfg.get("output", {})
    probable_path = Path(output.get("probable_abuse_csv", "evidence/probable_abuse.csv"))
    needs_path = Path(output.get("needs_review_csv", "evidence/needs_review.csv"))
    report_path = Path(output.get("report_csv", "evidence/report.csv"))
    rows = load_csv_rows(probable_path, "probable_abuse") + load_csv_rows(needs_path, "needs_review")
    probable_rows = [r for r in rows if r.get("decision") == "probable_abuse"]
    needs_rows = [r for r in rows if r.get("decision") == "needs_review"]
    fields, missing = ensure_base_fields(base_token, table_id, create_missing=not dry_run)
    if dry_run and missing:
        result = {
            "dry_run": True,
            "fields_ok": False,
            "missing_fields": missing,
            "would_insert_count": 0,
            "would_skip_duplicate_count": 0,
            "would_alert_count": 0,
            "base_url": feishu_cfg.get("base_url", ""),
            "table_name": feishu_cfg.get("table_name", ""),
            "table_id": table_id,
            "report_csv": str(report_path),
            "probable_abuse_csv": str(probable_path),
            "needs_review_csv": str(needs_path),
        }
        print("[FEISHU_SYNC] " + json.dumps(result, ensure_ascii=False))
        return result
    inserted, skipped, inserted_rows = write_rows_to_base(base_token, table_id, rows, dry_run=dry_run, limit=limit_probable)
    alert_candidates = [r for r in inserted_rows if r.get("decision") == "probable_abuse"]
    if alert_limit is not None:
        alert_candidates = alert_candidates[:alert_limit]
    alert_sent = 0
    if send_alerts and not dry_run:
        for r in alert_candidates:
            msg = (
                "**图片滥用监控告警：probable_abuse**\n"
                f"product_id: {r.get('product_id','')}\n"
                f"domain: {r.get('source_domain','')}\n"
                f"match_type: {r.get('match_type','')}\n"
                f"source: {r.get('source_page_url','')}\n"
                f"image: {r.get('candidate_image_url','')}\n"
                f"reason: {r.get('reason','')}"
            )
            # Alert delivery is left to the caller (e.g. an agent or a cron wrapper) that reads this payload.
            print("[FEISHU_ALERT] " + json.dumps({"message": msg}, ensure_ascii=False))
            alert_sent += 1
    result = {
        "dry_run": dry_run,
        "fields_ok": True,
        "missing_fields": [],
        "source_probable_abuse_count": len(probable_rows),
        "source_needs_review_count": len(needs_rows),
        "inserted_count" if not dry_run else "would_insert_count": inserted if not dry_run else len(inserted_rows),
        "skipped_duplicate_count" if not dry_run else "would_skip_duplicate_count": skipped,
        "alert_sent_count" if not dry_run else "would_alert_count": alert_sent if not dry_run else len([r for r in inserted_rows if r.get("decision") == "probable_abuse"]),
        "alert_rule": "only_newly_inserted_probable_abuse_records_trigger_alerts",
        "base_url": feishu_cfg.get("base_url", ""),
        "table_name": feishu_cfg.get("table_name", ""),
        "table_id": table_id,
        "report_csv": str(report_path),
        "probable_abuse_csv": str(probable_path),
        "needs_review_csv": str(needs_path),
    }
    print("[FEISHU_SYNC] " + json.dumps(result, ensure_ascii=False))
    return result


PLATFORM_NOTICE = {
    "temu": ("Temu", "Temu Intellectual Property Protection portal (search 'Temu IP complaint' or use the report link on the listing)"),
    "ebay": ("eBay", "eBay VeRO program (Verified Rights Owner) - Notice of Claimed Infringement"),
    "walmart": ("Walmart", "Walmart Marketplace IP claim form"),
    "tiktok": ("TikTok Shop", "TikTok Intellectual Property Protection Center"),
}


def _load_probable_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("decision") != "probable_abuse":
                continue
            key = f"{row.get('product_id')}|{row.get('source_page_url') or row.get('candidate_image_url')}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def _evidence_lines(rows: list[dict[str, Any]]) -> str:
    out = []
    for i, r in enumerate(rows, 1):
        page = r.get("source_page_url") or "(page URL not captured)"
        out.append(
            f"{i}. Infringing page: {page}\n"
            f"   Copied image: {r.get('candidate_image_url', '')}\n"
            f"   Match: {r.get('match_type', '')}, pHash distance {r.get('phash_distance')}, "
            f"found {str(r.get('timestamp_utc', ''))[:10]}"
        )
    return "\n".join(out)


def _product_header(product: Product) -> str:
    asin = product.asin or "[ASIN]"
    return (
        f"Brand: {product.brand or '[BRAND]'}\n"
        f"Product: {product.product_name or '[PRODUCT NAME]'}\n"
        f"Amazon listing: https://www.amazon.com/dp/{asin}\n"
    )


SIGNATURE = (
    "Sincerely,\n[YOUR NAME]\n[COMPANY NAME]\n[EMAIL] | [PHONE]\n[ADDRESS]\n"
)


def _platform_notice(platform: str, product: Product, rows: list[dict[str, Any]]) -> str:
    name, channel = PLATFORM_NOTICE.get(platform, ("the website owner / host", "DMCA agent or abuse contact of the site's host, registrar or storefront platform"))
    return (
        f"COPYRIGHT INFRINGEMENT NOTICE - {name}\n"
        f"Submit via: {channel}\n\n"
        f"{_product_header(product)}\n"
        "To whom it may concern,\n\n"
        f"I am the copyright owner (or authorized agent of the owner) of the original product photographs for {product.brand or '[BRAND]'} "
        f"{product.product_name or '[PRODUCT NAME]'}. The listings below use these photographs without permission. "
        "The original images are published on our Amazon listing above and were created by us.\n\n"
        "Infringing listings:\n"
        f"{_evidence_lines(rows)}\n\n"
        "I request that these listings or images be removed.\n\n"
        "I have a good faith belief that the use of the material described above is not authorized by the copyright owner, its agent, or the law. "
        "The information in this notice is accurate, and under penalty of perjury, I am the owner, or an agent authorized to act on behalf of the owner, "
        "of the copyright that is allegedly infringed.\n\n"
        f"{SIGNATURE}"
    )


def _amazon_case(product: Product, rows: list[dict[str, Any]], platforms: list[str]) -> str:
    asin = product.asin or "[ASIN]"
    labels = sorted({PLATFORM_NOTICE.get(p, ("independent websites",))[0] for p in platforms}, key=lambda x: (x == "independent websites", x))
    names = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    return (
        f"AMAZON SELLER SUPPORT CASE - Featured Offer (Buy Box) suppressed by unauthorized external listings\n"
        "Open via: Seller Central > Help > Get support > Selling on Amazon > Pricing > Featured Offer / Buy Box\n\n"
        f"Subject: ASIN {asin} - Buy Box suppressed due to unauthorized listings using our copyrighted images\n\n"
        "Hello Seller Support,\n\n"
        f"Our ASIN {asin} ({product.brand or '[BRAND]'} {product.product_name or '[PRODUCT NAME]'}) lost the Featured Offer. "
        f"We believe Amazon's price competitiveness check matched our product to external listings on {names} "
        "that copied our product photos and show a lower price. These listings are not authorized sellers of our product "
        "and are not a comparable offer.\n\n"
        "Unauthorized listings:\n"
        f"{_evidence_lines(rows)}\n\n"
        "We have filed infringement reports with each platform. Complaint IDs: [ADD COMPLAINT IDS]\n\n"
        "Please remove the incorrect external price reference for this ASIN and refresh the Featured Offer eligibility. "
        "Screenshots of the infringing listings and our original images are attached.\n\n"
        "Thank you,\n[YOUR NAME]\n[SELLER / MERCHANT TOKEN]\n"
    )


def generate_complaints(cfg: dict[str, Any], products: list[Product]) -> int:
    """Write ready-to-edit complaint drafts for probable_abuse findings. Nothing is sent."""
    out_cfg = cfg.get("output", {})
    rows = _load_probable_rows(Path(out_cfg.get("report_jsonl", "evidence/report.jsonl")))
    by_id = {p.product_id: p for p in products}
    base_dir = Path(out_cfg.get("evidence_dir", "evidence")) / "complaints"
    written = 0
    for pid, product in by_id.items():
        prows = [r for r in rows if r.get("product_id") == pid]
        if not prows:
            continue
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in prows:
            platform = classify_platform(r.get("source_domain") or r.get("source_page_url", ""), cfg)
            groups.setdefault(platform, []).append(r)
        out_dir = base_dir / pid
        out_dir.mkdir(parents=True, exist_ok=True)
        for platform, grows in groups.items():
            fname = f"{pid}_{platform}_complaint.txt" if platform in PLATFORM_NOTICE else f"{pid}_dmca_notice_other_sites.txt"
            (out_dir / fname).write_text(_platform_notice(platform, product, grows), encoding="utf-8")
            written += 1
        (out_dir / f"{pid}_amazon_case.txt").write_text(_amazon_case(product, prows, list(groups)), encoding="utf-8")
        written += 1
        print(f"[COMPLAINTS] {pid}: {len(prows)} probable_abuse findings -> {out_dir}")
    if not written:
        print("[COMPLAINTS] No probable_abuse findings in the evidence log. Nothing to draft.")
    else:
        print("[COMPLAINTS] Drafts only. Review, fill in the [BRACKETS], attach screenshots, then submit yourself.")
    return written


def collect_candidates(product: Product, cfg: dict[str, Any], provider: str, reference_gcs_uri: str | None) -> tuple[list[Candidate], list[PageMatch]]:
    search_cfg = cfg.get("search", {})
    all_candidates: list[Candidate] = []
    all_pages: list[PageMatch] = []
    if provider in {"web-detection", "both"} and search_cfg.get("enable_web_detection", True):
        c, p = web_detection_candidates(product, cfg, reference_gcs_uri)
        all_candidates.extend(c)
        all_pages.extend(p)
    if provider in {"custom-search", "both"} and search_cfg.get("enable_custom_search", False):
        c, p = custom_search_candidates(product, cfg)
        all_candidates.extend(c)
        all_pages.extend(p)
    return dedupe_candidates(all_candidates), all_pages


def budget_skip_evidence(product: Product, candidate: Candidate, reason: str) -> Evidence:
    return Evidence(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        product_id=product.product_id,
        candidate_image_url=candidate.candidate_image_url,
        source_page_url=candidate.source_page_url,
        source_domain=candidate.source_domain,
        match_type=candidate.match_type,
        web_detection_score=candidate.score,
        phash_distance=None,
        dhash_distance=None,
        local_similarity_decision="unknown",
        warehouse_relevance=None,
        matched_asset=None,
        decision="skipped_due_to_budget_limit",
        reason=reason,
        local_image_path=None,
    )


def process_product(
    product: Product,
    cfg: dict[str, Any],
    dry_run: bool,
    provider: str,
    reference_gcs_uri: str | None,
    use_warehouse: bool,
    seen: set[str],
) -> tuple[list[Evidence], list[PageMatch]]:
    rows: list[Evidence] = []
    phashes = reference_hashes(product, "phash")
    dhashes = reference_hashes(product, "dhash")
    candidates, page_matches = collect_candidates(product, cfg, provider, reference_gcs_uri)
    max_validations = int(cfg.get("search", {}).get("max_candidates_for_warehouse_per_product", 20))
    validations_used = 0
    print(f"[INFO] {product.product_id}: discovered {len(candidates)} unique image candidates, {len(page_matches)} page matches")

    for candidate in candidates:
        key = candidate_cache_key(candidate, product.product_id)
        if key in seen:
            continue
        domain_for_policy = candidate.source_page_url or candidate.candidate_image_url or candidate.source_domain
        _, domain_reason = domain_policy(domain_for_policy, cfg)
        if dry_run:
            print(f"[DRY] candidate match_type={candidate.match_type} domain={candidate.source_domain} image={candidate.candidate_image_url}")
            seen.add(key)
            continue

        if domain_reason != "keep":
            # Domain policy has highest priority. Record it as ignored without spending
            # network/download/Warehouse budget.
            evidence = Evidence(
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                product_id=product.product_id,
                candidate_image_url=candidate.candidate_image_url,
                source_page_url=candidate.source_page_url,
                source_domain=candidate.source_domain,
                match_type=candidate.match_type,
                web_detection_score=candidate.score,
                phash_distance=None,
                dhash_distance=None,
                local_similarity_decision="unknown",
                warehouse_relevance=None,
                matched_asset=None,
                decision="ignored",
                reason=domain_reason,
                local_image_path=None,
            )
            rows.append(evidence)
            append_jsonl(Path(cfg.get("output", {}).get("report_jsonl", "evidence/report.jsonl")), evidence)
            seen.add(key)
            continue

        if validations_used >= max_validations:
            evidence = budget_skip_evidence(product, candidate, "max_candidates_for_warehouse_per_product reached")
            rows.append(evidence)
            append_jsonl(Path(cfg.get("output", {}).get("report_jsonl", "evidence/report.jsonl")), evidence)
            seen.add(key)
            continue

        local_path = download_image(candidate, cfg, product)
        if not local_path:
            seen.add(key)
            continue
        phash_dist = min_hash_distance(local_path, phashes, "phash")
        dhash_dist = min_hash_distance(local_path, dhashes, "dhash")
        local_decision = local_similarity_decision(phash_dist, dhash_dist, cfg)

        warehouse_score = None
        matched_asset_name = None
        warehouse_reason = None
        if use_warehouse:
            warehouse_score, matched_asset_name, warehouse_reason = warehouse_search_validate(local_path, cfg)
            validations_used += 1

        decision, reason = decide(candidate, domain_reason, local_decision, warehouse_score, warehouse_reason, cfg)
        evidence = Evidence(
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            product_id=product.product_id,
            candidate_image_url=candidate.candidate_image_url,
            source_page_url=candidate.source_page_url,
            source_domain=candidate.source_domain,
            match_type=candidate.match_type,
            web_detection_score=candidate.score,
            phash_distance=phash_dist,
            dhash_distance=dhash_dist,
            local_similarity_decision=local_decision,
            warehouse_relevance=warehouse_score,
            matched_asset=matched_asset_name,
            decision=decision,
            reason=reason,
            local_image_path=str(local_path),
        )
        rows.append(evidence)
        append_jsonl(Path(cfg.get("output", {}).get("report_jsonl", "evidence/report.jsonl")), evidence)
        print(f"[RESULT] {decision} product={product.product_id} domain={candidate.source_domain} match_type={candidate.match_type} phash={phash_dist} dhash={dhash_dist} warehouse={warehouse_score} reason={reason}")
        seen.add(key)
    return rows, page_matches


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor product image abuse across marketplaces and independent sites.")
    parser.add_argument("--config", required=True, help="Path to monitor_config.yaml")
    parser.add_argument("--products", required=True, help="Path to products.yaml")
    parser.add_argument("--product-id", help="Process only one product_id")
    parser.add_argument("--dry-run", action="store_true", help="Discover and filter candidates only; no downloads or Warehouse validation")
    parser.add_argument(
        "--discovery-provider",
        choices=["web-detection", "custom-search", "both"],
        default=None,
        help="Candidate discovery provider. Defaults to search.discovery_provider or web-detection.",
    )
    parser.add_argument("--reference-gcs-uri", help="GCS URI of official reference image for Web Detection, e.g. gs://<YOUR_GCS_BUCKET>/path.jpg")
    parser.add_argument("--no-web-detection", action="store_true", help="Deprecated alias: use --discovery-provider custom-search")
    parser.add_argument("--generate-complaints", action="store_true", help="Read existing evidence JSONL and generate complaint templates for probable_abuse findings")
    parser.add_argument("--feishu-sync", action="store_true", help="Sync evidence/probable_abuse.csv and evidence/needs_review.csv to Feishu Base")
    parser.add_argument("--feishu-sync-dry-run", action="store_true", help="Dry-run Feishu sync: inspect CSV, Base fields, and dedupe keys without writing")
    parser.add_argument("--feishu-limit-probable", type=int, help="Limit Feishu sync to N non-duplicate rows, used for single-record testing")
    parser.add_argument("--feishu-send-alerts", action="store_true", help="Emit probable_abuse alert payloads for inserted rows")
    parser.add_argument("--feishu-alert-limit", type=int, help="Limit probable_abuse alert payload count")
    args = parser.parse_args()

    cfg = load_yaml(args.config)
    provider = args.discovery_provider or cfg.get("search", {}).get("discovery_provider", "web-detection")
    if args.no_web_detection:
        provider = "custom-search"

    products = load_products(args.products)
    if args.product_id:
        products = [p for p in products if p.product_id == args.product_id]
    if not products:
        print("[ERROR] No products to process.", file=sys.stderr)
        return 2

    if args.generate_complaints:
        generate_complaints(cfg, products)
        return 0

    if args.feishu_sync or args.feishu_sync_dry_run:
        feishu_sync_from_evidence(
            cfg,
            dry_run=args.feishu_sync_dry_run,
            limit_probable=args.feishu_limit_probable,
            send_alerts=args.feishu_send_alerts,
            alert_limit=args.feishu_alert_limit,
        )
        return 0

    use_warehouse = is_warehouse_configured(cfg)
    if not use_warehouse:
        print("[INFO] Vision Warehouse not configured. Running local/Web Detection decision mode.", file=sys.stderr)

    cache_path = Path(cfg.get("output", {}).get("seen_cache", "evidence/seen_urls.json"))
    seen = load_seen_cache(cache_path)
    all_rows: list[Evidence] = []
    all_pages: list[PageMatch] = []
    try:
        for product in products:
            rows, pages = process_product(product, cfg, args.dry_run, provider, args.reference_gcs_uri, use_warehouse, seen)
            all_rows.extend(rows)
            all_pages.extend(pages)
    finally:
        save_seen_cache(cache_path, seen)

    csv_path = Path(cfg.get("output", {}).get("report_csv", "evidence/report.csv"))
    pages_csv_path = Path(cfg.get("output", {}).get("page_matches_csv", "evidence/page_matches.csv"))
    probable_csv_path = Path(cfg.get("output", {}).get("probable_abuse_csv", "evidence/probable_abuse.csv"))
    needs_review_csv_path = Path(cfg.get("output", {}).get("needs_review_csv", "evidence/needs_review.csv"))
    probable_rows = [row for row in all_rows if row.decision == "probable_abuse"]
    needs_review_rows = [row for row in all_rows if row.decision == "needs_review"]
    write_csv(csv_path, all_rows)
    write_csv(pages_csv_path, all_pages)
    write_csv(probable_csv_path, probable_rows)
    write_csv(needs_review_csv_path, needs_review_rows)
    summary = evidence_summary(all_rows, csv_path, pages_csv_path)
    print(f"[INFO] Done. New evidence rows: {len(all_rows)}; page matches: {len(all_pages)}")
    print(f"[INFO] JSONL report: {cfg.get('output', {}).get('report_jsonl', 'evidence/report.jsonl')}")
    print(f"[INFO] CSV report: {csv_path}")
    print(f"[INFO] Page matches CSV: {pages_csv_path}")
    print(f"[INFO] Probable abuse CSV: {probable_csv_path}")
    print(f"[INFO] Needs review CSV: {needs_review_csv_path}")
    print_evidence_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
