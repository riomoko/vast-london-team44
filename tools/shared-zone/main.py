#!/usr/bin/env python3
"""Shared-zone watch: people vs warehouse robots/forklifts. Featured clips + live search."""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import quote, unquote

import requests
from flask import Flask, Response, jsonify, request

PORT = int(os.environ.get("PORT", "8080"))
VSS_URL = os.environ.get("VSS_URL", "").rstrip("/")
VSS_USERNAME = os.environ.get("VSS_USERNAME", "")
VSS_PASSWORD = os.environ.get("VSS_PASSWORD", "")

SEARCH_QUERY = "person too close to a forklift or robot in a warehouse aisle"
SEARCH_QUERY_ALT = "person colliding with a forklift"

FEATURED = [
    {
        "id": "ceiling-04",
        "filename": "20261001_075529_16face5576497e69c190_03a2937960b9e61f1c99_run_7_seed_900334964.ceiling_04.rgb_chunk_0000.mp4",
        "camera": "ceiling_04",
        "label": "Ceiling 04 — person enters robot path",
        "start": 5,
        "end": 10,
        "badge": "NEAR MISS",
    },
    {
        "id": "ceiling-01",
        "filename": "20261001_074955_0b12b2416a91a8686bf0_016c1396912bfc432997_run_3_seed_1053328212.ceiling_01.rgb_chunk_0000.mp4",
        "camera": "ceiling_01",
        "label": "Ceiling 01 — shared aisle",
        "start": 5,
        "end": 10,
        "badge": "SHARED ZONE",
    },
    {
        "id": "eye-00",
        "filename": "20261001_075554_16face5576497e69c190_03a2937960b9e61f1c99_run_7_seed_900334964.eye_00.rgb_chunk_0000.mp4",
        "camera": "eye_00",
        "label": "Eye 00 — contact window",
        "start": 0,
        "end": 5,
        "badge": "COLLISION",
    },
]

app = Flask(__name__)
_token_lock = threading.Lock()
_token = {"value": None, "exp": 0.0}


def login(force: bool = False) -> str:
    now = time.time()
    with _token_lock:
        if not force and _token["value"] and now < _token["exp"]:
            return _token["value"]
        r = requests.post(
            f"{VSS_URL}/api/v1/auth/login",
            json={"username": VSS_USERNAME, "password": VSS_PASSWORD},
            timeout=30,
        )
        r.raise_for_status()
        tok = r.json()["access_token"]
        _token["value"] = tok
        _token["exp"] = now + 20 * 60
        return tok


def vss_get(path: str, params=None, stream=False, headers=None, timeout=60):
    token = login()
    h = {"Authorization": f"Bearer {token}"}
    if headers:
        h.update(headers)
    r = requests.get(f"{VSS_URL}{path}", params=params, headers=h, stream=stream, timeout=timeout)
    if r.status_code == 401:
        token = login(force=True)
        h["Authorization"] = f"Bearer {token}"
        r = requests.get(f"{VSS_URL}{path}", params=params, headers=h, stream=stream, timeout=timeout)
    return r


def vss_post(path: str, body: dict):
    token = login()
    r = requests.post(
        f"{VSS_URL}{path}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if r.status_code == 401:
        token = login(force=True)
        r = requests.post(
            f"{VSS_URL}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
    return r


def explore_all():
    items = []
    offset = 0
    total = 1
    while offset < total and offset < 500:
        r = vss_get("/api/v1/videos/explore", params={"scope": "all", "limit": 100, "offset": offset})
        r.raise_for_status()
        data = r.json()
        batch = data.get("items") or data.get("videos") or data.get("results") or data.get("chunks") or []
        if isinstance(data, list):
            batch = data
            total = offset + len(batch)
        else:
            total = int(data.get("total") or data.get("count") or offset + len(batch))
            if not batch:
                break
        items.extend(batch)
        offset += 100
        if len(batch) < 100:
            break
    return items


def filename_of(item: dict) -> str:
    for key in ("filename", "original_video", "source", "preview_source", "name"):
        val = item.get(key) or ""
        if isinstance(val, str) and val:
            return val.split("/")[-1]
    return ""


def source_of(item: dict) -> str:
    return (
        item.get("preview_source")
        or item.get("source")
        or item.get("original_video")
        or ""
    )


def match_featured(items: list[dict]) -> list[dict]:
    out = []
    for feat in FEATURED:
        hit = None
        for item in items:
            name = filename_of(item)
            orig = str(item.get("original_video") or "")
            if feat["filename"] in name or feat["filename"] in orig:
                hit = item
                break
        card = dict(feat)
        if hit:
            card["source"] = source_of(hit)
            card["original_video"] = hit.get("original_video") or ""
            card["reasoning"] = (
                hit.get("reasoning_content")
                or hit.get("description")
                or hit.get("summary")
                or "Person and warehouse vehicle share the aisle."
            )
            card["found"] = True
        else:
            card["source"] = ""
            card["original_video"] = ""
            card["reasoning"] = "Clip is in the team index — open VSS Explore if playback fails."
            card["found"] = False
        out.append(card)
    return out


def search_hits() -> list[dict]:
    featured_names = {f["filename"] for f in FEATURED}
    hits = []
    for query in (SEARCH_QUERY, SEARCH_QUERY_ALT):
        r = vss_post(
            "/api/v1/search",
            {
                "query": query,
                "top_k": 8,
                "llm_top_n": 0,
                "min_similarity": 0.25,
                "time_filter": "all",
                "include_public": True,
            },
        )
        if r.status_code >= 400:
            continue
        data = r.json()
        rows = data.get("results") or []
        for row in rows:
            src = row.get("source") or row.get("preview_source") or ""
            orig = str(row.get("original_video") or row.get("source") or "")
            name = orig.split("/")[-1]
            if any(fn in orig or fn in src for fn in featured_names):
                continue
            start = row.get("best_match_start_sec") or row.get("start_time") or row.get("start_sec") or 0
            try:
                start = float(start)
            except (TypeError, ValueError):
                start = 0
            hits.append(
                {
                    "id": f"search-{len(hits)}",
                    "filename": name,
                    "camera": row.get("camera_id") or "warehouse",
                    "label": (row.get("reasoning_content") or query)[:90],
                    "start": max(0, int(start)),
                    "end": max(0, int(start)) + 5,
                    "badge": "INDEX HIT",
                    "source": src or orig,
                    "original_video": orig,
                    "reasoning": (row.get("reasoning_content") or "")[:280],
                    "found": bool(src or orig),
                    "score": row.get("similarity_score"),
                }
            )
        if hits:
            break
    # de-dupe by source
    seen = set()
    uniq = []
    for h in hits:
        key = h.get("source") or h.get("filename")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(h)
    return uniq[:6]


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Shared Zone Watch — team-44</title>
<style>
  :root { --bg:#0b0f14; --card:#141b24; --line:#243042; --txt:#e8eef6; --muted:#93a4bb; --red:#ff4d4f; --amber:#ffb020; --ok:#3dd68c; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--txt); }
  header { padding:28px 32px 12px; display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap; align-items:flex-end; }
  h1 { margin:0; font-size:28px; letter-spacing:-.03em; }
  .sub { color:var(--muted); max-width:640px; line-height:1.45; margin-top:8px; }
  .stats { display:flex; gap:12px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px 18px; min-width:120px; }
  .stat b { display:block; font-size:26px; }
  .stat span { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
  .grid { padding:12px 32px 40px; display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px; overflow:hidden; display:flex; flex-direction:column; }
  video { width:100%; height:220px; background:#000; object-fit:cover; }
  .body { padding:14px 16px 16px; }
  .badge { display:inline-block; font-size:11px; font-weight:700; letter-spacing:.06em; padding:4px 8px; border-radius:999px; }
  .NEAR, .COLLISION, .NEAR\ MISS { background:rgba(255,77,79,.18); color:var(--red); }
  .SHARED, .SHARED\ ZONE { background:rgba(255,176,32,.16); color:var(--amber); }
  .INDEX { background:rgba(61,214,140,.12); color:var(--ok); }
  h2 { margin:8px 0 6px; font-size:16px; }
  p { margin:0; color:var(--muted); font-size:13px; line-height:1.4; }
  .meta { margin-top:10px; font-size:12px; color:var(--muted); }
  button { margin-top:10px; background:#e8eef6; color:#0b0f14; border:0; border-radius:10px; padding:8px 12px; font-weight:700; cursor:pointer; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Shared Zone Watch</h1>
    <p class="sub">Warehouse mixed traffic: flag the instant a person enters a robot / forklift path. Featured clips jump to the risk window — not the full take.</p>
  </div>
  <div class="stats">
    <div class="stat"><b id="n-feat">0</b><span>featured triggers</span></div>
    <div class="stat"><b id="n-red">0</b><span>shared-zone alerts</span></div>
  </div>
</header>
<section class="grid" id="grid"><p class="sub" style="padding:12px">Loading index…</p></section>
<script>
async function load() {
  const res = await fetch('api/events');
  const data = await res.json();
  const events = data.events || [];
  document.getElementById('n-feat').textContent = data.featured_count ?? events.length;
  document.getElementById('n-red').textContent = events.length;
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  events.forEach((ev, i) => {
    const start = Number(ev.start || 0);
    const end = Number(ev.end || start + 5);
    const src = ev.source ? ('stream?source=' + encodeURIComponent(ev.source)) : '';
    const card = document.createElement('article');
    card.className = 'card';
    const badgeClass = (ev.badge || 'INDEX').split(' ')[0];
    card.innerHTML = `
      ${src ? `<video id="v${i}" playsinline muted controls preload="metadata" src="${src}#t=${start},${end}"></video>` : '<video></video>'}
      <div class="body">
        <span class="badge ${badgeClass}">${ev.badge || 'ALERT'}</span>
        <h2>${ev.label || ev.camera}</h2>
        <p>${ev.reasoning || ''}</p>
        <div class="meta">${ev.camera || ''} · window ${start}s–${end}s</div>
        <button data-i="${i}" data-start="${start}">Play risk window</button>
      </div>`;
    grid.appendChild(card);
    const v = card.querySelector('video');
    if (v) {
      v.addEventListener('loadedmetadata', () => { try { v.currentTime = start; } catch (e) {} });
    }
  });
  grid.querySelectorAll('button').forEach(btn => {
    btn.onclick = () => {
      const v = document.getElementById('v' + btn.dataset.i);
      if (!v) return;
      v.currentTime = Number(btn.dataset.start);
      v.play();
    };
  });
}
load().catch(err => {
  document.getElementById('grid').innerHTML = '<p class="sub">Could not load events. ' + err + '</p>';
});
</script>
</body>
</html>
"""


@app.get("/health")
def health():
    return jsonify(ok=True, service="shared-zone-watch")


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.get("/api/events")
def events():
    try:
        items = explore_all()
        featured = match_featured(items)
        extra = search_hits()
        events = featured + extra
        return jsonify(
            {
                "query": SEARCH_QUERY,
                "featured_count": len(featured),
                "events": events,
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc), "events": FEATURED, "featured_count": len(FEATURED)}), 200


@app.get("/stream")
def stream():
    source = unquote(request.args.get("source") or "")
    if not source:
        return jsonify(error="missing source"), 400
    token = login()
    url = f"{VSS_URL}/api/v1/videos/stream?source={quote(source, safe='')}&token={quote(token)}"
    headers = {}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    upstream = requests.get(url, headers=headers, stream=True, timeout=120)
    skip = {"content-encoding", "transfer-encoding", "connection"}
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in skip}
    return Response(
        upstream.iter_content(64 * 1024),
        status=upstream.status_code,
        headers=out_headers,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
