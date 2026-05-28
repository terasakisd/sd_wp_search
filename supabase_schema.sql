-- ============================================================================
-- WP Multi-Site Search - Supabase スキーマ
-- 既存の SQLite FTS5 構成を PostgreSQL の tsvector で再現
-- 日本語は「空白挿入済み *_idx カラム」を simple config で tsvector 化
-- ============================================================================

-- ----- sites: クロール対象サイト -----
CREATE TABLE IF NOT EXISTS sites (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    group_id        TEXT NOT NULL DEFAULT 'backlink',
    last_crawled_at TIMESTAMPTZ,
    last_modified   TIMESTAMPTZ
);

-- ----- posts: 記事本体 -----
CREATE TABLE IF NOT EXISTS posts (
    id              BIGSERIAL PRIMARY KEY,
    site_id         TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    post_id         BIGINT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    excerpt         TEXT,
    content         TEXT,
    author          TEXT,
    published_at    TIMESTAMPTZ,
    modified_at     TIMESTAMPTZ,
    categories      TEXT,   -- "|cat1|cat2|" 形式 (既存と同じ)
    tags            TEXT,
    -- 日本語ユニグラム化済みのテキスト (Pythonクローラ側で文字間に空白挿入)
    title_idx       TEXT,
    excerpt_idx     TEXT,
    content_idx     TEXT,
    -- 生成カラム: tsvector を STORED で保持して検索高速化
    -- 'simple' config は語幹処理をしないので、日本語ユニグラム検索に向く
    fts             tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(title_idx, '')),   'A') ||
        setweight(to_tsvector('simple', coalesce(excerpt_idx, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(content_idx, '')), 'C')
    ) STORED,
    UNIQUE (site_id, post_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_fts        ON posts USING GIN (fts);
CREATE INDEX IF NOT EXISTS idx_posts_site       ON posts (site_id);
CREATE INDEX IF NOT EXISTS idx_posts_published  ON posts (published_at DESC);

-- ============================================================================
-- 検索RPC関数
--   フロントエンドから supabase.rpc('search_posts', {...}) で呼ぶ
--   fts_query: クライアント側で組み立てた tsquery 文字列
--     例: 'クレジット & カード'  /  '東 <-> 京 <-> 駅' (フレーズ)
-- ============================================================================

-- 旧版が残っていると overload 衝突するので明示的に DROP
DROP FUNCTION IF EXISTS search_posts(TEXT, TEXT[], TEXT, TEXT, INT, INT);
DROP FUNCTION IF EXISTS search_posts(TEXT, TEXT[], TEXT, TEXT, INT, INT, BOOLEAN);

CREATE OR REPLACE FUNCTION search_posts(
    fts_query TEXT DEFAULT '',
    p_site_ids TEXT[] DEFAULT NULL,
    p_group_id TEXT DEFAULT NULL,
    p_sort TEXT DEFAULT 'relevance',
    p_limit INT DEFAULT 30,
    p_offset INT DEFAULT 0,
    p_widgets_only BOOLEAN DEFAULT FALSE
)
RETURNS TABLE (
    site_id TEXT,
    post_id BIGINT,
    url TEXT,
    title TEXT,
    excerpt TEXT,
    author TEXT,
    published_at TIMESTAMPTZ,
    categories TEXT,
    tags TEXT,
    total_count BIGINT,
    rank REAL
)
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    use_fts BOOLEAN := length(coalesce(fts_query, '')) > 0;
    q tsquery;
BEGIN
    IF use_fts THEN
        q := to_tsquery('simple', fts_query);
    END IF;

    RETURN QUERY
    WITH filtered AS (
        SELECT p.*, s.group_id AS s_group_id
        FROM posts p
        JOIN sites s ON s.id = p.site_id
        WHERE
            (NOT use_fts OR p.fts @@ q)
            AND (p_site_ids IS NULL OR p.site_id = ANY(p_site_ids))
            AND (p_group_id IS NULL OR s.group_id = p_group_id)
            -- ウィジット領域 (post_id=-1) は通常検索では除外、
            -- p_widgets_only=TRUE のときのみ対象にする
            AND (
                (p_widgets_only AND p.post_id = -1)
                OR (NOT p_widgets_only AND p.post_id <> -1)
            )
    ),
    counted AS (
        SELECT *,
               COUNT(*) OVER ()::BIGINT AS total_count,
               CASE WHEN use_fts THEN ts_rank(filtered.fts, q) ELSE 0 END AS rank
        FROM filtered
    )
    SELECT
        c.site_id, c.post_id, c.url, c.title, c.excerpt, c.author,
        c.published_at, c.categories, c.tags, c.total_count, c.rank
    FROM counted c
    ORDER BY
        CASE WHEN p_sort = 'newest' THEN c.published_at END DESC NULLS LAST,
        CASE WHEN p_sort = 'oldest' THEN c.published_at END ASC  NULLS LAST,
        CASE WHEN p_sort = 'relevance' AND use_fts THEN c.rank END DESC NULLS LAST,
        CASE WHEN p_sort = 'relevance' AND NOT use_fts THEN c.published_at END DESC NULLS LAST,
        c.id ASC  -- 安定ソートのタイブレーカ
    LIMIT p_limit
    OFFSET p_offset;
END;
$$;

-- ============================================================================
-- ファセット用RPC: サイト一覧 + 各サイトの記事件数
-- ============================================================================

CREATE OR REPLACE FUNCTION list_sites_with_counts()
RETURNS TABLE (
    id TEXT,
    name TEXT,
    group_id TEXT,
    url TEXT,
    count BIGINT
)
LANGUAGE sql STABLE
AS $$
    SELECT s.id, s.name, s.group_id, s.url, COALESCE(c.cnt, 0)::BIGINT AS count
    FROM sites s
    LEFT JOIN (
        -- ウィジット領域 (post_id=-1) はカウントから除外
        SELECT site_id, COUNT(*) AS cnt FROM posts WHERE post_id <> -1 GROUP BY site_id
    ) c ON c.site_id = s.id
    ORDER BY s.group_id, s.name;
$$;

-- ============================================================================
-- 行レベルセキュリティ (RLS): 読み取りは誰でも可、書き込みは service_role のみ
-- ============================================================================

ALTER TABLE sites ENABLE ROW LEVEL SECURITY;
ALTER TABLE posts ENABLE ROW LEVEL SECURITY;

-- 読み取りポリシー (anon キーでも SELECT 可)
DROP POLICY IF EXISTS "public read sites" ON sites;
CREATE POLICY "public read sites" ON sites FOR SELECT USING (true);

DROP POLICY IF EXISTS "public read posts" ON posts;
CREATE POLICY "public read posts" ON posts FOR SELECT USING (true);

-- 書き込みは service_role のみ (RLSをバイパスする特権キー)
-- RLSなしで service_role が書き込み可能なのは Supabase の標準動作

-- ============================================================================
-- RPC を anon にも公開
-- ============================================================================

GRANT EXECUTE ON FUNCTION search_posts          TO anon, authenticated;
GRANT EXECUTE ON FUNCTION list_sites_with_counts TO anon, authenticated;
