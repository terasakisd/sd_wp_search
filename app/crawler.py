"""WordPress REST API クローラー。

各サイトに対して `/wp-json/wp/v2/posts?_embed&per_page=N&page=M&modified_after=...` を叩き、
記事をDBに保存する。差分更新対応。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from bs4 import BeautifulSoup

# USE_SUPABASE=1 で Supabase 書き込みモードに切り替え (GitHub Actions 用)
if os.getenv("USE_SUPABASE") == "1":
    from . import db_supabase as db  # type: ignore
else:
    from . import db  # type: ignore

log = logging.getLogger("crawler")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "sites.yaml"


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # id 省略時は name を id として使う (両方同じ値になる)
    for s in cfg.get("sites") or []:
        if not s.get("id") and s.get("name"):
            s["id"] = s["name"]
    return cfg


# ----- HTML/テキスト変換 -----------------------------------------------------

def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # script/style 除去
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    # 連続空白の正規化
    return " ".join(text.split())


def extract_embedded_terms(post: dict, taxonomy: str) -> list[str]:
    """_embedded から category / tag 名のリストを取り出す。"""
    embedded = post.get("_embedded", {})
    terms_groups = embedded.get("wp:term", [])
    names: list[str] = []
    for group in terms_groups:
        for term in group:
            if term.get("taxonomy") == taxonomy:
                name = term.get("name")
                if name:
                    names.append(name)
    return names


def extract_author(post: dict) -> str:
    embedded = post.get("_embedded", {})
    authors = embedded.get("author", [])
    if authors and isinstance(authors, list):
        return authors[0].get("name", "") or ""
    return ""


def normalize_post(site_id: str, post: dict, base_url: str = "") -> dict:
    title_html = (post.get("title") or {}).get("rendered", "")
    content_html = (post.get("content") or {}).get("rendered", "")
    excerpt_html = (post.get("excerpt") or {}).get("rendered", "")

    categories = extract_embedded_terms(post, "category")
    tags = extract_embedded_terms(post, "post_tag")

    # WP REST が link を相対パスで返すサイトがあるのでホストを補う
    link = post.get("link", "") or ""
    if link.startswith("/") and base_url:
        from urllib.parse import urlparse
        p = urlparse(base_url)
        link = f"{p.scheme}://{p.netloc}{link}"

    return {
        "site_id": site_id,
        "post_id": post.get("id"),
        "url": link,
        "title": html_to_text(title_html),
        "excerpt": html_to_text(excerpt_html),
        "content": html_to_text(content_html),
        "author": extract_author(post),
        "published_at": post.get("date_gmt") or post.get("date"),
        "modified_at": post.get("modified_gmt") or post.get("modified"),
        "categories": "|" + "|".join(categories) + "|" if categories else "",
        "tags": "|" + "|".join(tags) + "|" if tags else "",
    }


# ----- クロール処理 -----------------------------------------------------------

TIMEOUT_EXC = (
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.WriteTimeout,
)


async def fetch_page(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    page: int,
    per_page: int,
    embed: bool,
    modified_after: str | None,
    rest_base: str = "posts",
    extra_params: dict | None = None,
) -> tuple[list[dict], int, int]:
    """1ページ取得して (記事リスト, 総ページ数, 総件数) を返す。

    ページネーションが安定するよう orderby=id, order=asc を使用する。
    rest_base/extra_params でカスタム投稿タイプ・追加フィルタに対応。
    """
    params: dict[str, str | int] = {
        "per_page": per_page,
        "page": page,
        "orderby": "id",
        "order": "asc",
    }
    if embed:
        params["_embed"] = "1"
    if modified_after:
        params["modified_after"] = modified_after
    if extra_params:
        for k, v in extra_params.items():
            params[k] = v

    def _is_json_response(r) -> bool:
        ctype = r.headers.get("content-type", "")
        return "json" in ctype.lower()

    # /wp-json/ 形式を試す (リダイレクトは追跡しない)
    url = f"{base_url}/wp-json/wp/v2/{rest_base}"
    resp = await client.get(url, params=params, follow_redirects=False)

    # 403 → WAF が TLS フィンガープリントで弾いてる可能性
    # curl_cffi で Chrome 偽装してリトライ
    if resp.status_code == 403:
        cffi_result = await _try_curl_cffi(url, params, dict(client.headers))
        if cffi_result is not None:
            return cffi_result

    # 3xx (wp-json が別ページに転送される構成) / 404 / 非JSON →
    # ?rest_route= 形式にフォールバック
    needs_fallback = (
        resp.status_code == 404
        or 300 <= resp.status_code < 400
        or not _is_json_response(resp)
    )
    if needs_fallback:
        fallback_params = {"rest_route": f"/wp/v2/{rest_base}", **params}
        fallback_url = f"{base_url}/"
        resp = await client.get(
            fallback_url, params=fallback_params, follow_redirects=False
        )
        # フォールバックでも 403 なら curl_cffi で再試行
        if resp.status_code == 403:
            cffi_result = await _try_curl_cffi(fallback_url, fallback_params, dict(client.headers))
            if cffi_result is not None:
                return cffi_result
        # フォールバックも 301/302 を返すサイトがあるので 1段だけ追跡
        if 300 <= resp.status_code < 400:
            loc = resp.headers.get("location")
            if loc:
                # 相対URLなら base_url で補完
                if loc.startswith("/"):
                    from urllib.parse import urlparse
                    p = urlparse(base_url)
                    loc = f"{p.scheme}://{p.netloc}{loc}"
                resp = await client.get(loc, follow_redirects=False)

    # 範囲外ページは 400 → 空として終端扱い
    if resp.status_code == 400:
        return [], 0, 0
    resp.raise_for_status()
    # フォールバック後も JSON が返らない場合は明示的エラー
    if not _is_json_response(resp):
        raise httpx.HTTPError(
            f"non-JSON response from {resp.request.url} "
            f"(content-type={resp.headers.get('content-type')})"
        )
    total_pages = int(resp.headers.get("X-WP-TotalPages", "1") or "1")
    total = int(resp.headers.get("X-WP-Total", "0") or "0")
    return resp.json(), total_pages, total


async def _try_curl_cffi(
    url: str, params: dict, headers: dict
) -> tuple[list[dict], int, int] | None:
    """curl_cffi で Chrome TLS 指紋を再現して再取得。

    成功すれば (data, total_pages, total)、失敗すれば None を返す。
    """
    try:
        from curl_cffi.requests import AsyncSession  # type: ignore
    except ImportError:
        log.warning("curl_cffi が未インストール。403 リトライをスキップ")
        return None

    try:
        async with AsyncSession(impersonate="chrome124") as cf:
            r = await cf.get(url, params=params, headers=headers, timeout=30, allow_redirects=False)
        if r.status_code != 200:
            log.warning(f"curl_cffi {url} → {r.status_code}")
            return None
        ctype = r.headers.get("content-type", "")
        if "json" not in ctype.lower():
            log.warning(f"curl_cffi {url} → non-JSON ({ctype})")
            return None
        total_pages = int(r.headers.get("X-WP-TotalPages", "1") or "1")
        total = int(r.headers.get("X-WP-Total", "0") or "0")
        log.info(f"curl_cffi success: {url} ({total} posts)")
        return r.json(), total_pages, total
    except Exception as e:
        log.warning(f"curl_cffi error for {url}: {e}")
        return None


async def _walk_all_pages(
    client: httpx.AsyncClient,
    site_id: str,
    base_url: str,
    *,
    per_page: int,
    embed: bool,
    modified_after: str | None,
    delay: float,
    rest_base: str = "posts",
    extra_params: dict | None = None,
) -> tuple[int, str | None, int, set[int]]:
    """指定モードで全ページを1回最後まで走る。

    タイムアウト等で途中失敗した場合は例外を上位に伝播させ、
    上位がより弱いモードで page=1 から再走できるようにする。
    返り値: (取得件数, 最新 modified, サーバ側の総件数, 取得した post_id 集合)
    """
    count = 0
    page = 1
    newest_modified = modified_after
    server_total = 0
    fetched_ids: set[int] = set()
    while True:
        posts, total_pages, server_total = await fetch_page(
            client, base_url,
            page=page, per_page=per_page, embed=embed,
            modified_after=modified_after,
            rest_base=rest_base, extra_params=extra_params,
        )
        if not posts:
            break

        for post in posts:
            normalized = normalize_post(site_id, post, base_url=base_url)
            db.upsert_post(normalized)
            count += 1
            pid = normalized.get("post_id")
            if pid is not None:
                try:
                    fetched_ids.add(int(pid))
                except (ValueError, TypeError):
                    pass
            mod = normalized.get("modified_at")
            if mod and (newest_modified is None or mod > newest_modified):
                newest_modified = mod

        log.info(
            f"[{site_id}] page {page}/{total_pages} embed={embed} pp={per_page}: "
            f"+{len(posts)} (cumulative {count}/{server_total})"
        )
        if page >= total_pages:
            break
        page += 1
        await asyncio.sleep(delay)

    return count, newest_modified, server_total, fetched_ids


async def crawl_site(
    client: httpx.AsyncClient,
    site: dict,
    crawl_cfg: dict,
    *,
    purge_stale: bool = False,
) -> int:
    """1サイトをクロール。新規追加 + 更新された件数を返す。

    リトライ戦略: 各モードは page=1 から最後まで完走する。途中で例外なら
    モードを弱めて最初からやり直す (upsert は冪等なので重複は問題ない)。

    purge_stale=True の場合:
      - modified_after を無視して全件取得
      - 成功時、DB にあって取得結果に含まれなかった post を削除
    """
    site_id = site["id"]
    base_url = site["url"].rstrip("/")
    db.upsert_site(site_id, site["name"], base_url, site.get("group") or "backlink")

    state = db.get_site(site_id)
    # purge_stale 時は強制全件 (modified_after を無視)
    last_modified = None if purge_stale else (state["last_modified"] if state else None)

    default_pp = int(crawl_cfg.get("per_page", 100))
    delay = crawl_cfg.get("delay_between_requests", 0.5)

    rest_base = site.get("post_type") or "posts"
    extra_params = site.get("extra_params") or {}

    log.info(
        f"[{site_id}] start (since={last_modified or 'all'}) "
        f"rest_base={rest_base} extra={extra_params} purge_stale={purge_stale}"
    )

    # 試行モード: リッチ → 軽量 → さらに小さく
    attempts = [
        {"embed": True,  "per_page": default_pp},
        {"embed": False, "per_page": default_pp},
        {"embed": False, "per_page": max(10, default_pp // 5)},
    ]

    last_exc: Exception | None = None
    for attempt in attempts:
        try:
            log.info(
                f"[{site_id}] attempt embed={attempt['embed']} per_page={attempt['per_page']}"
            )
            count, newest, server_total, fetched_ids = await _walk_all_pages(
                client, site_id, base_url,
                per_page=attempt["per_page"],
                embed=attempt["embed"],
                modified_after=last_modified,
                delay=delay,
                rest_base=rest_base,
                extra_params=extra_params,
            )
            db.update_site_crawl_state(
                site_id, datetime.now(timezone.utc).isoformat(), newest
            )
            if server_total and count < server_total:
                log.warning(
                    f"[{site_id}] obtained {count}/{server_total} "
                    f"(差分クロール時は X-WP-Total と一致しない場合あり)"
                )
            log.info(f"[{site_id}] done: {count} posts (server reports {server_total})")

            # purge_stale 成功時: 取得できなかった post を削除 (安全装置なし)
            # 全件取得 (last_modified is None) のときのみ
            if purge_stale and last_modified is None:
                db_ids = db.get_post_ids_for_site(site_id)
                stale_ids = list(db_ids - fetched_ids)
                if stale_ids:
                    deleted = db.delete_posts(site_id, stale_ids)
                    log.info(f"[{site_id}] purged {deleted} stale posts")
                else:
                    log.info(f"[{site_id}] no stale posts")
            return count
        except TIMEOUT_EXC as e:
            log.warning(
                f"[{site_id}] timeout in attempt embed={attempt['embed']} "
                f"per_page={attempt['per_page']} → restart from page=1 with weaker mode"
            )
            last_exc = e
            continue
        except httpx.HTTPError as e:
            log.warning(f"[{site_id}] http error: {e}")
            return 0

    log.error(f"[{site_id}] all attempts failed: {last_exc}")
    return 0


# ----- HTMLスクレイピング経路 ----------------------------------------------

def _url_to_post_id(url: str) -> int:
    """URLから決定的な正整数 post_id を生成 (SQLiteのINTEGER範囲内)。"""
    h = hashlib.md5(url.encode("utf-8")).hexdigest()
    return int(h[:15], 16)  # 15桁hex ≈ 60bit


def _extract_dates(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """記事HTMLから (published_at, modified_at) を ISO 8601 文字列で抽出。

    優先順位:
      1. JSON-LD (datePublished / dateModified)
      2. <meta property="article:published_time|modified_time" content="...">
      3. <time datetime="..."> タグ (最初のものを公開日として採用)
    取得できない値は None を返す。
    """
    import json as _json

    published: str | None = None
    modified: str | None = None

    # 1) JSON-LD
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = _json.loads(raw)
        except Exception:
            continue
        candidates = []
        if isinstance(data, list):
            candidates.extend(data)
        elif isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                candidates.extend(data["@graph"])
            else:
                candidates.append(data)
        for item in candidates:
            if not isinstance(item, dict):
                continue
            dp = item.get("datePublished")
            dm = item.get("dateModified")
            if dp and not published:
                published = str(dp)
            if dm and not modified:
                modified = str(dm)
        if published or modified:
            break

    # 2) meta タグ
    if not published:
        el = soup.find("meta", attrs={"property": "article:published_time"})
        if el and el.get("content"):
            published = el["content"]
    if not modified:
        el = soup.find("meta", attrs={"property": "article:modified_time"})
        if el and el.get("content"):
            modified = el["content"]

    # 3) <time datetime="..."> フォールバック
    if not published:
        el = soup.find("time", attrs={"datetime": True})
        if el:
            published = el["datetime"]

    return published, modified


def _extract_article(html: str) -> tuple[str, str, str | None, str | None]:
    """記事ページのHTMLから (タイトル, 本文, 公開日ISO, 更新日ISO) を抽出。"""
    soup = BeautifulSoup(html, "html.parser")
    # タイトル候補
    title = ""
    for sel in ["h1.c--entry-title", "h1.entry-title", "article h1", "h1", "title"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break
    # 日付 (本文用 soup を改変する前に取得)
    published, modified = _extract_dates(soup)
    # 本文候補（記事本体らしき要素を優先）
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
        tag.decompose()
    body_el = None
    for sel in ["article.jinr-article", "article", "main", ".entry-content", "#content"]:
        body_el = soup.select_one(sel)
        if body_el:
            break
    if body_el is None:
        body_el = soup.body or soup
    content = " ".join(body_el.get_text(separator=" ", strip=True).split())
    return title, content, published, modified


async def crawl_site_scrape(
    client: httpx.AsyncClient,
    site: dict,
    crawl_cfg: dict,
    *,
    purge_stale: bool = False,
) -> int:
    """HTMLアーカイブから記事URLを抽出して個別にfetchするモード。

    YAML 例:
      scrape:
        archive_url: https://example.com/category/foo/
        link_re: "^https://example\\.com/post-[a-z0-9-]+/?$"

    purge_stale=True の場合:
      - アーカイブに載っていない記事を DB から削除
      - 安全策: 削除予定が DB の50%を超える場合は中止 (アーカイブ取得失敗を疑う)
    """
    site_id = site["id"]
    base_url = site["url"].rstrip("/")
    db.upsert_site(site_id, site["name"], base_url, site.get("group") or "backlink")

    cfg = site["scrape"]
    archive_url = cfg["archive_url"]
    link_re = re.compile(cfg["link_re"])
    delay = crawl_cfg.get("delay_between_requests", 0.5)

    log.info(f"[{site_id}] scrape start archive={archive_url} purge_stale={purge_stale}")

    # アーカイブ取得 → リンク抽出
    resp = await client.get(archive_url)
    resp.raise_for_status()
    archive_soup = BeautifulSoup(resp.text, "html.parser")
    seen: set[str] = set()
    for a in archive_soup.find_all("a", href=True):
        href = a["href"]
        if link_re.match(href):
            seen.add(href.rstrip("/") + "/")  # 正規化: 末尾スラッシュ統一
    article_urls = sorted(seen)
    log.info(f"[{site_id}] scrape: {len(article_urls)} article links found")

    count = 0
    newest_modified = None
    fetched_ids: set[int] = set()
    fetch_errors = 0
    for url in article_urls:
        # アーカイブに載った URL は (取得成否に関係なく) 「存在する記事」
        # として post_id を集めておく → 削除対象から除外
        fetched_ids.add(_url_to_post_id(url))
        try:
            r = await client.get(url)
            r.raise_for_status()
            title, content, published, modified = _extract_article(r.text)
            if not title:
                log.warning(f"[{site_id}] no title for {url}")
                continue
            # 日付が取れなかった場合のフォールバック (現在時刻)
            now_iso = datetime.now(timezone.utc).isoformat()
            published_at = published or now_iso
            modified_at = modified or published_at
            db.upsert_post({
                "site_id": site_id,
                "post_id": _url_to_post_id(url),
                "url": url,
                "title": title,
                "excerpt": content[:200] if content else "",
                "content": content,
                "author": "",
                "published_at": published_at,
                "modified_at": modified_at,
                "categories": "",
                "tags": "",
            })
            count += 1
            if newest_modified is None or modified_at > newest_modified:
                newest_modified = modified_at
            log.info(
                f"[{site_id}] scrape +1: {title[:40]} pub={published_at[:10]} ({url})"
            )
        except httpx.HTTPError as e:
            log.warning(f"[{site_id}] scrape failed for {url}: {e}")
            fetch_errors += 1
        await asyncio.sleep(delay)

    db.update_site_crawl_state(
        site_id, datetime.now(timezone.utc).isoformat(), newest_modified
    )
    log.info(f"[{site_id}] scrape done: {count} articles ({fetch_errors} errors)")

    # purge_stale: アーカイブに無い旧記事を削除 (安全装置なし、消してもDBだけ)
    if purge_stale:
        db_ids = db.get_post_ids_for_site(site_id)
        stale_ids = list(db_ids - fetched_ids)
        if stale_ids:
            deleted = db.delete_posts(site_id, stale_ids)
            log.info(f"[{site_id}] purged {deleted} stale posts")
        else:
            log.info(f"[{site_id}] no stale posts")

    return count


async def crawl_all(
    only_site_ids: list[str] | None = None,
    *,
    purge_stale: bool = False,
) -> dict[str, int]:
    """設定ファイル上の全サイトをクロール。

    purge_stale=True: 全件取得し直し、DBから削除済み記事を消す。
    """
    config = load_config()
    crawl_cfg = config.get("crawl", {})
    timeout = crawl_cfg.get("request_timeout", 30)

    sites = config.get("sites", [])
    if only_site_ids:
        sites = [s for s in sites if s["id"] in only_site_ids]

    # WAF/CDN ブロック回避のため、Chrome ライクなヘッダ一式を送る
    _browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    # 旧式SSL (DH_KEY_TOO_SMALL 等) サイト対応: SECLEVEL を 1 に下げる
    import ssl as _ssl
    ssl_ctx = _ssl.create_default_context()
    try:
        ssl_ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except _ssl.SSLError:
        pass

    results: dict[str, int] = {}
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=_browser_headers,
        follow_redirects=True,
        verify=ssl_ctx,
    ) as client:
        # サイトごとに並列実行 (ただし同時実行数は抑える)
        sem = asyncio.Semaphore(5)

        async def run(site):
            async with sem:
                try:
                    if site.get("scrape"):
                        n = await crawl_site_scrape(
                            client, site, crawl_cfg, purge_stale=purge_stale
                        )
                    else:
                        n = await crawl_site(
                            client, site, crawl_cfg, purge_stale=purge_stale
                        )
                    results[site["id"]] = n
                except Exception as e:
                    log.exception(f"[{site['id']}] failed: {e}")
                    results[site["id"]] = -1

        await asyncio.gather(*(run(s) for s in sites))

    return results


def main() -> None:
    db.init_db()
    # 環境変数 CRAWL_ONLY_SITE_IDS で対象サイトを絞れる
    # (カンマ区切り、空白可。空なら全件)
    only_env = os.getenv("CRAWL_ONLY_SITE_IDS", "").strip()
    only_ids = [s.strip() for s in only_env.split(",") if s.strip()] or None
    # CRAWL_PURGE_STALE=1 で削除検知モード (全件取得 + 取れなかった記事をDBから削除)
    purge_stale = os.getenv("CRAWL_PURGE_STALE", "").strip() in ("1", "true", "True")
    if only_ids:
        log.info(f"crawl restricted to {len(only_ids)} sites: {only_ids}")
    if purge_stale:
        log.info("purge_stale=True: 削除検知モードで実行 (全件取り直し)")
    start = time.time()
    results = asyncio.run(crawl_all(only_site_ids=only_ids, purge_stale=purge_stale))
    elapsed = time.time() - start
    log.info(f"all done in {elapsed:.1f}s: {results}")


if __name__ == "__main__":
    main()
