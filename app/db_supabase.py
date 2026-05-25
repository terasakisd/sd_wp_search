"""Supabase版 DB 層 (PostgREST 直接呼び出し版)。

supabase-py を使わず httpx で直接 REST API を叩く。理由:
- 新形式キー (sb_secret_*) も旧形式 JWT も両方そのまま使える
- 依存が少なく、Python 3.9 でもビルド問題なし

環境変数:
    SUPABASE_URL          - https://xxxxx.supabase.co
    SUPABASE_SERVICE_KEY  - service_role key (旧JWT も新 sb_secret_ も可)
"""
from __future__ import annotations

import os
import unicodedata
from typing import Any

import httpx


_SUPABASE_URL = ""
_HEADERS: dict[str, str] = {}


def _init_client() -> None:
    """環境変数から URL と認証ヘッダを構築。"""
    global _SUPABASE_URL, _HEADERS
    if _HEADERS:
        return
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    _SUPABASE_URL = url
    _HEADERS = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _rest(method: str, path: str, *, params: dict | None = None,
          json: Any = None, extra_headers: dict | None = None) -> httpx.Response:
    _init_client()
    headers = dict(_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    url = f"{_SUPABASE_URL}/rest/v1{path}"
    with httpx.Client(timeout=30) as c:
        r = c.request(method, url, params=params, json=json, headers=headers)
    if r.status_code >= 400:
        raise RuntimeError(
            f"Supabase REST error: {r.status_code} {r.text[:300]}"
        )
    return r


# ----- トークン化 (db.py から移植) ------------------------------------------

def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF
        or 0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF66 <= code <= 0xFF9F
    )


def tokenize_for_index(text: str) -> str:
    """インデックス用に、日本語文字の前後に空白を入れる。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    prev_cjk = False
    prev_space = True
    for ch in text:
        if ch.isspace():
            out.append(" ")
            prev_cjk = False
            prev_space = True
            continue
        if _is_cjk(ch):
            if not prev_space:
                out.append(" ")
            out.append(ch)
            out.append(" ")
            prev_cjk = True
            prev_space = True
        else:
            if not (ch.isalnum() or ch in "_-.@/"):
                out.append(" ")
                prev_space = True
                prev_cjk = False
                continue
            if prev_cjk and not prev_space:
                out.append(" ")
            out.append(ch.lower())
            prev_cjk = False
            prev_space = False
    return " ".join("".join(out).split())


# ----- ノーオペ関数 (FastAPI 互換) ------------------------------------------

def init_db() -> None:
    """Supabaseではスキーマは SQL Editor で別途作成済み。何もしない。"""
    pass


# ----- サイト操作 -----------------------------------------------------------

def upsert_site(site_id: str, name: str, url: str, group_id: str = "backlink") -> None:
    _rest(
        "POST",
        "/sites",
        params={"on_conflict": "id"},
        json={"id": site_id, "name": name, "url": url, "group_id": group_id},
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )


def update_site_crawl_state(
    site_id: str, last_crawled_at: str, last_modified: str | None
) -> None:
    payload: dict[str, Any] = {"last_crawled_at": last_crawled_at}
    if last_modified is not None:
        payload["last_modified"] = last_modified
    _rest(
        "PATCH",
        "/sites",
        params={"id": f"eq.{site_id}"},
        json=payload,
        extra_headers={"Prefer": "return=minimal"},
    )


def get_site(site_id: str) -> dict | None:
    r = _rest("GET", "/sites", params={"id": f"eq.{site_id}", "limit": 1})
    data = r.json()
    return data[0] if data else None


def list_sites() -> list[dict]:
    r = _rest("GET", "/sites", params={"order": "name.asc"})
    return r.json() or []


def reset_site_modified_state(site_id: str) -> None:
    _rest(
        "PATCH",
        "/sites",
        params={"id": f"eq.{site_id}"},
        json={"last_modified": None},
        extra_headers={"Prefer": "return=minimal"},
    )


def reset_all_modified_state() -> None:
    _rest(
        "PATCH",
        "/sites",
        params={"id": "neq.__never__"},
        json={"last_modified": None},
        extra_headers={"Prefer": "return=minimal"},
    )


# ----- 記事操作 -------------------------------------------------------------

def get_post_ids_for_site(site_id: str) -> set[int]:
    """サイト内の全 post_id を取得。ページングしながら集計。"""
    ids: set[int] = set()
    page_size = 1000
    offset = 0
    while True:
        r = _rest(
            "GET", "/posts",
            params={
                "select": "post_id",
                "site_id": f"eq.{site_id}",
                "limit": page_size,
                "offset": offset,
            },
        )
        rows = r.json() or []
        for row in rows:
            pid = row.get("post_id")
            if pid is not None:
                ids.add(int(pid))
        if len(rows) < page_size:
            break
        offset += page_size
    return ids


def delete_posts(site_id: str, post_ids: list[int]) -> int:
    """指定IDの記事を削除。削除件数を返す。"""
    if not post_ids:
        return 0
    deleted = 0
    # PostgREST の in.() 句に大量IDを渡せないのでバッチ分割
    for i in range(0, len(post_ids), 200):
        batch = post_ids[i:i + 200]
        ids_str = ",".join(str(x) for x in batch)
        r = _rest(
            "DELETE", "/posts",
            params={"site_id": f"eq.{site_id}", "post_id": f"in.({ids_str})"},
            extra_headers={"Prefer": "count=exact"},
        )
        cr = r.headers.get("content-range", "")
        if "/" in cr:
            try:
                deleted += int(cr.split("/")[-1])
            except ValueError:
                deleted += len(batch)
        else:
            deleted += len(batch)
    return deleted


def upsert_post(post: dict[str, Any]) -> None:
    p = dict(post)
    # PostgreSQL の生成カラム fts は送らない (送ると弾かれる)
    # *_idx は Pythonで生成して送る
    p["title_idx"] = tokenize_for_index(p.get("title") or "")
    p["excerpt_idx"] = tokenize_for_index(p.get("excerpt") or "")
    p["content_idx"] = tokenize_for_index(p.get("content") or "")

    # post_id は BIGINT 想定 (URLハッシュ由来のスクレイピング値も収まる)
    if p.get("post_id") is not None:
        p["post_id"] = int(p["post_id"])

    # 空文字の categories/tags は NULL に統一 (任意)
    if not p.get("categories"):
        p["categories"] = None
    if not p.get("tags"):
        p["tags"] = None

    _rest(
        "POST",
        "/posts",
        params={"on_conflict": "site_id,post_id"},
        json=p,
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )


# ----- 件数取得 (動作確認用) ------------------------------------------------

def count_posts() -> int:
    r = _rest(
        "GET", "/posts", params={"select": "id", "limit": 1},
        extra_headers={"Prefer": "count=exact"},
    )
    cr = r.headers.get("content-range", "")
    # content-range: "0-0/12345"
    if "/" in cr:
        try:
            return int(cr.split("/")[-1])
        except ValueError:
            return 0
    return 0


def count_posts_by_site() -> dict[str, int]:
    r = _rest("GET", "/posts", params={"select": "site_id"})
    counts: dict[str, int] = {}
    for row in r.json() or []:
        sid = row["site_id"]
        counts[sid] = counts.get(sid, 0) + 1
    return counts
