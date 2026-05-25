"""SQLite + FTS5 を使ったデータベース層。

日本語対応について:
- SQLite組み込みのFTS5 (unicode61) は CJK の連続文字を1トークンとして扱うため、
  そのままだと「東京駅周辺」が丸ごと1トークンになり、「東京駅」では検索できない。
- 解決策: 保存前に日本語文字の間に半角スペースを挿入することで、
  各文字を独立したトークンとしてインデックスさせる (擬似ユニグラム)。
- 検索時も同様に、日本語クエリを 1 文字ずつスペース区切りに変換して MATCH に渡す。
- 隣接マッチ ("東 京 駅" というフレーズ) を使うことで、文字順序を保ったまま検索可能。
- これにより、形態素解析エンジン(MeCab/Sudachi) を使わずに部分一致検索ができる。
- トレードオフ: 「東京都」で「東京」を含む文書もヒットするなど精度はやや甘いが、
  ローカル横断検索の用途では実用上問題ない。
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "posts.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    group_id        TEXT NOT NULL DEFAULT 'backlink',
    last_crawled_at TEXT,
    last_modified   TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id         TEXT NOT NULL,
    post_id         INTEGER NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    excerpt         TEXT,
    content         TEXT,
    author          TEXT,
    published_at    TEXT,
    modified_at     TEXT,
    categories      TEXT,
    tags            TEXT,
    -- FTS用に「日本語文字の間に空白を挿入した」版を保存。これがFTSのインデックス対象。
    title_idx       TEXT,
    excerpt_idx     TEXT,
    content_idx     TEXT,
    UNIQUE (site_id, post_id),
    FOREIGN KEY (site_id) REFERENCES sites(id)
);

CREATE INDEX IF NOT EXISTS idx_posts_site ON posts(site_id);
CREATE INDEX IF NOT EXISTS idx_posts_published ON posts(published_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    title_idx, excerpt_idx, content_idx,
    content='posts', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, title_idx, excerpt_idx, content_idx)
    VALUES (new.rowid, new.title_idx, new.excerpt_idx, new.content_idx);
END;
CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, title_idx, excerpt_idx, content_idx)
    VALUES('delete', old.rowid, old.title_idx, old.excerpt_idx, old.content_idx);
END;
CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, title_idx, excerpt_idx, content_idx)
    VALUES('delete', old.rowid, old.title_idx, old.excerpt_idx, old.content_idx);
    INSERT INTO posts_fts(rowid, title_idx, excerpt_idx, content_idx)
    VALUES (new.rowid, new.title_idx, new.excerpt_idx, new.content_idx);
END;
"""


# ----- トークン化ヘルパー -----------------------------------------------------

def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    if 0x3040 <= code <= 0x30FF:  # ひらがな・カタカナ
        return True
    if 0x4E00 <= code <= 0x9FFF:  # CJK統合漢字
        return True
    if 0x3400 <= code <= 0x4DBF:  # CJK拡張A
        return True
    if 0xF900 <= code <= 0xFAFF:  # CJK互換漢字
        return True
    if 0xFF66 <= code <= 0xFF9F:  # 半角カナ
        return True
    return False


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


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        # 既存DBへのマイグレーション: group_id カラム追加
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sites)").fetchall()}
        if "group_id" not in cols:
            conn.execute(
                "ALTER TABLE sites ADD COLUMN group_id TEXT NOT NULL DEFAULT 'backlink'"
            )


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


# ----- サイト操作 -------------------------------------------------------------

def upsert_site(site_id: str, name: str, url: str, group_id: str = "backlink") -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sites (id, name, url, group_id) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                url=excluded.url,
                group_id=excluded.group_id
            """,
            (site_id, name, url, group_id),
        )


def update_site_crawl_state(site_id: str, last_crawled_at: str, last_modified: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE sites SET last_crawled_at = ?, last_modified = COALESCE(?, last_modified) WHERE id = ?",
            (last_crawled_at, last_modified, site_id),
        )


def reset_site_modified_state(site_id: str) -> None:
    """last_modified をクリアして次回クロールを全件取り直しにする。"""
    with connect() as conn:
        conn.execute("UPDATE sites SET last_modified = NULL WHERE id = ?", (site_id,))


def reset_all_modified_state() -> None:
    with connect() as conn:
        conn.execute("UPDATE sites SET last_modified = NULL")


def get_site(site_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()


def list_sites() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM sites ORDER BY name").fetchall()


# ----- 記事操作 ---------------------------------------------------------------

def get_post_ids_for_site(site_id: str) -> set[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT post_id FROM posts WHERE site_id = ?", (site_id,)
        ).fetchall()
    return {r["post_id"] for r in rows}


def delete_posts(site_id: str, post_ids: list[int]) -> int:
    """指定IDの記事を削除。削除件数を返す。"""
    if not post_ids:
        return 0
    with connect() as conn:
        # SQLite の IN リテラルに大量IDを渡せないのでバッチ
        deleted = 0
        for i in range(0, len(post_ids), 500):
            batch = post_ids[i:i + 500]
            placeholders = ",".join("?" * len(batch))
            cur = conn.execute(
                f"DELETE FROM posts WHERE site_id = ? AND post_id IN ({placeholders})",
                (site_id, *batch),
            )
            deleted += cur.rowcount
    return deleted


def upsert_post(post: dict[str, Any]) -> None:
    p = dict(post)
    p["title_idx"] = tokenize_for_index(p.get("title") or "")
    p["excerpt_idx"] = tokenize_for_index(p.get("excerpt") or "")
    p["content_idx"] = tokenize_for_index(p.get("content") or "")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO posts (site_id, post_id, url, title, excerpt, content,
                               author, published_at, modified_at, categories, tags,
                               title_idx, excerpt_idx, content_idx)
            VALUES (:site_id, :post_id, :url, :title, :excerpt, :content,
                    :author, :published_at, :modified_at, :categories, :tags,
                    :title_idx, :excerpt_idx, :content_idx)
            ON CONFLICT(site_id, post_id) DO UPDATE SET
                url=excluded.url,
                title=excluded.title,
                excerpt=excluded.excerpt,
                content=excluded.content,
                author=excluded.author,
                published_at=excluded.published_at,
                modified_at=excluded.modified_at,
                categories=excluded.categories,
                tags=excluded.tags,
                title_idx=excluded.title_idx,
                excerpt_idx=excluded.excerpt_idx,
                content_idx=excluded.content_idx
            """,
            p,
        )


def count_posts() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()["c"]


def count_posts_by_site() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT site_id, COUNT(*) AS c FROM posts GROUP BY site_id"
        ).fetchall()
        return {r["site_id"]: r["c"] for r in rows}


# ----- 検索クエリ構築 ---------------------------------------------------------

_FTS_SPECIAL = re.compile(r'["\(\)\*:]')


def _to_index_tokens(text: str) -> list[str]:
    return [t for t in tokenize_for_index(text).split(" ") if t]


def _is_ascii_word(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-\.@/]+", s))


def _build_fts_query(raw: str) -> str:
    """ユーザー入力を FTS5 MATCH クエリに変換する。"""
    raw = unicodedata.normalize("NFKC", raw.strip())
    if not raw:
        return ""

    # フレーズ ("...") 抽出
    parts: list[tuple[str, bool]] = []
    buf: list[str] = []
    in_phrase = False
    phrase_buf: list[str] = []
    for ch in raw:
        if ch == '"':
            if in_phrase:
                p = "".join(phrase_buf).strip()
                if p:
                    parts.append((p, True))
                phrase_buf = []
                in_phrase = False
            else:
                if buf:
                    parts.append(("".join(buf), False))
                    buf = []
                in_phrase = True
        elif in_phrase:
            phrase_buf.append(ch)
        else:
            buf.append(ch)
    if buf:
        parts.append(("".join(buf), False))
    if in_phrase and phrase_buf:
        parts.append(("".join(phrase_buf), False))

    fts_parts: list[str] = []
    for text, is_phrase in parts:
        text = _FTS_SPECIAL.sub(" ", text)
        if is_phrase:
            tokens = _to_index_tokens(text)
            if tokens:
                fts_parts.append('"' + " ".join(tokens) + '"')
        else:
            for tok in re.split(r"[\s\u3000]+", text):
                if not tok:
                    continue
                idx_tokens = _to_index_tokens(tok)
                if not idx_tokens:
                    continue
                if len(idx_tokens) == 1:
                    t = idx_tokens[0]
                    if _is_ascii_word(t):
                        fts_parts.append(f"{t}*")
                    else:
                        fts_parts.append(f'"{t}"')
                else:
                    if all(not _is_ascii_word(t) for t in idx_tokens):
                        # 日本語連続 → 隣接フレーズ
                        fts_parts.append('"' + " ".join(idx_tokens) + '"')
                    else:
                        sub = [f"{t}*" if _is_ascii_word(t) else f'"{t}"' for t in idx_tokens]
                        fts_parts.append("(" + " AND ".join(sub) + ")")

    return " AND ".join(fts_parts)


# ----- 検索 -------------------------------------------------------------------

def search_posts(
    query: str,
    *,
    site_ids: list[str] | None = None,
    group_id: str | None = None,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    sort: str = "relevance",
    limit: int = 30,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    fts_query = _build_fts_query(query) if query else ""

    where_parts: list[str] = []
    params: list[Any] = []

    base_from = "FROM posts p"
    if fts_query:
        base_from += " JOIN posts_fts f ON f.rowid = p.rowid"
        where_parts.append("posts_fts MATCH ?")
        params.append(fts_query)

    if group_id:
        base_from += " JOIN sites s ON s.id = p.site_id"
        where_parts.append("s.group_id = ?")
        params.append(group_id)

    if site_ids:
        where_parts.append(f"p.site_id IN ({','.join('?' * len(site_ids))})")
        params.extend(site_ids)

    for col, vals in (("categories", categories), ("tags", tags)):
        if vals:
            ors = []
            for v in vals:
                ors.append(f"p.{col} LIKE ?")
                params.append(f"%|{v}|%")
            where_parts.append("(" + " OR ".join(ors) + ")")

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    select_cols = ("p.site_id, p.post_id, p.url, p.title, p.excerpt, p.author, "
                   "p.published_at, p.categories, p.tags")
    count_sql = f"SELECT COUNT(*) AS c {base_from}{where_sql}"

    if sort == "newest":
        order_sql = " ORDER BY p.published_at DESC"
    elif sort == "oldest":
        order_sql = " ORDER BY p.published_at ASC"
    elif fts_query:
        order_sql = " ORDER BY bm25(posts_fts), p.published_at DESC"
    else:
        order_sql = " ORDER BY p.published_at DESC"

    body_sql = f"SELECT {select_cols} {base_from}{where_sql}{order_sql} LIMIT ? OFFSET ?"

    with connect() as conn:
        try:
            total = conn.execute(count_sql, params).fetchone()["c"]
            rows = conn.execute(body_sql, [*params, limit, offset]).fetchall()
        except sqlite3.OperationalError:
            return [], 0

    results = []
    for r in rows:
        d = dict(r)
        d["categories"] = _split_pipe(d.get("categories"))
        d["tags"] = _split_pipe(d.get("tags"))
        results.append(d)
    return results, total


def _split_pipe(s: str | None) -> list[str]:
    if not s:
        return []
    return [x for x in s.strip("|").split("|") if x]


# ----- ファセット -------------------------------------------------------------

def get_facets() -> dict[str, list[dict[str, Any]]]:
    with connect() as conn:
        sites = conn.execute("""
            SELECT s.id, s.name, s.group_id, COUNT(p.rowid) AS count
            FROM sites s
            LEFT JOIN posts p ON p.site_id = s.id
            GROUP BY s.id
            ORDER BY s.name
        """).fetchall()

        cat_counts: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        for row in conn.execute("SELECT categories, tags FROM posts").fetchall():
            for c in _split_pipe(row["categories"]):
                cat_counts[c] = cat_counts.get(c, 0) + 1
            for t in _split_pipe(row["tags"]):
                tag_counts[t] = tag_counts.get(t, 0) + 1

    return {
        "sites": [dict(r) for r in sites],
        "categories": sorted(
            [{"name": k, "count": v} for k, v in cat_counts.items()],
            key=lambda x: (-x["count"], x["name"]),
        ),
        "tags": sorted(
            [{"name": k, "count": v} for k, v in tag_counts.items()],
            key=lambda x: (-x["count"], x["name"]),
        ),
    }
