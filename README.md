# WP 横断検索

複数の WordPress サイトを横断検索する社内向けツールです。
GitHub Pages (UI) + Supabase (DB) + GitHub Actions (定期クロール) の構成で 24h 稼働します。

公開URL: <https://terasakisd.github.io/sd-wp-search/>

---

## 構成

```
[ユーザー] ─── ブラウザ
              │
              ↓
[GitHub Pages] HTML+JS+CSS    ← UI 配信
              │
              ↓ fetch
[Supabase] PostgreSQL + 全文検索 (RPC)
              ↑
[GitHub Actions] 4回/日 クロール (130サイト)
[Mac launchd] 4回/日 クロール (3サイトのみ・海外IP拒否対策)
```

| コンポーネント | 役割 |
|---|---|
| `docs/` | GitHub Pages 用の静的サイト (HTML / JS / CSS) |
| `supabase_schema.sql` | Supabase に流すスキーマ + 検索RPC |
| `app/crawler.py` | クローラ本体 (REST API + HTML スクレイピング両対応) |
| `app/db_supabase.py` | Supabase 書き込み層 (PostgREST 直叩き) |
| `.github/workflows/crawl.yml` | 定期クロール (cron) |
| `scripts/mac_crawl.sh` `scripts/com.wpsearch.crawl.plist` | Mac launchd 用 |
| `sites.yaml` | クロール対象サイト一覧 (これだけ編集すればOK) |

---

## サイトの追加 (運用)

### 編集者として参加してもらう

1. リポジトリ管理者が **Settings → Collaborators** から招待 (Write 権限)
2. 招待された人は GitHub の Web UI で [`sites.yaml`](sites.yaml) を編集
3. Commit すれば次回クロール (最大6時間後) で自動反映

### `sites.yaml` の書き方

```yaml
sites:
  - name: 新規サイト名
    url: https://example.com
    group: corporate     # corporate / backlink
```

| 項目 | 必須 | 説明 |
|---|---|---|
| `name` | ✓ | UI表示名 (内部 id にも使われる) |
| `url` | ✓ | サイトのトップ URL (末尾スラッシュなし) |
| `group` | ✓ | `corporate` (企業サイト) または `backlink` (被リンクサイト) |
| `post_type` | 任意 | カスタム投稿タイプ。例: `column` (省略時 `posts`) |
| `extra_params` | 任意 | REST API への追加クエリ。例: `{ column_cate: 47 }` |
| `scrape` | 任意 | REST 非公開サイト用、HTML スクレイピング設定 |

### カスタム投稿タイプ + カテゴリ絞り込み

```yaml
- name: okage (生活の知恵)
  url: https://okagekk.com
  group: corporate
  post_type: column
  extra_params:
    column_cate: 47
```

→ `https://okagekk.com/wp-json/wp/v2/column?column_cate=47&...` を叩く。

### HTML スクレイピング (REST 非公開サイト)

```yaml
- name: イーキャンパス
  url: https://www.ecampus.jp
  group: corporate
  scrape:
    archive_url: https://www.ecampus.jp/reading-category/borrowing/
    link_re: "^https://www\\.ecampus\\.jp/reading-(?!category)[a-z0-9-]+/?$"
```

アーカイブ HTML から `link_re` にマッチする URL を抽出 → 個別ページから本文を取得。

---

## クロールスケジュール

### GitHub Actions (本体・130サイト)

| 時刻 (JST) | モード | 内容 |
|---|---|---|
| **03:00** | 削除検知 (purge_stale) | 全件取り直し + 消えた記事を DB から削除 |
| 09:00 | 通常 | 差分のみ (`modified_after`) |
| 15:00 | 通常 | 差分のみ |
| 21:00 | 通常 | 差分のみ |

cron 設定は [`.github/workflows/crawl.yml`](.github/workflows/crawl.yml)。

### Mac launchd (3サイト専用)

GitHub Actions ランナー (US IP) からブロックされる以下 3 サイト向けに、日本IPの Mac から実行:

- `とうかい` / `愛代協` / `末松会計`

セットアップ:
```bash
bash scripts/install_mac_crawl.sh
```

これで `~/Library/LaunchAgents/com.wpsearch.crawl.plist` が登録され、JST 03:00 / 09:00 / 15:00 / 21:00 に自動実行 (スリープ中も `pmset` で復帰)。

### 手動実行

```bash
# 通常の差分クロール
gh workflow run "WordPress横断検索クローラ" --repo terasakisd/sd-wp-search

# 削除検知モード (全件取り直し)
gh workflow run "WordPress横断検索クローラ" --repo terasakisd/sd-wp-search \
  -F purge_stale=true

# 0件のサイトだけ
gh workflow run "WordPress横断検索クローラ" --repo terasakisd/sd-wp-search \
  -F only_failed=true

# 特定サイトだけ
gh workflow run "WordPress横断検索クローラ" --repo terasakisd/sd-wp-search \
  -F site_ids="とうかい,愛代協"
```

---

## 検索の仕様

### 検索対象

| 対象 | 重み | 備考 |
|---|---|---|
| 記事タイトル | A (最大) | |
| 記事抜粋 | B | |
| 記事本文 | C | HTMLタグ除去済 |
| サイト共通エリア | C | `post_id=-1` のウィジット領域 (サイドバー・フッター等) |

検索しない: カテゴリ名 / タグ名 / 著者名 / URL / 公開日 (これらは表示用)。

### 日本語の扱い

日本語は1文字ずつ空白で分けて PostgreSQL `tsvector` (`simple` config) に保存。「東京駅」を検索すると本文の「東京駅周辺」もヒット。フレーズ検索 `"東京駅"` (ダブルクォート) で連続文字を指定可。

---

## 初期デプロイ (引き継ぎ用)

新しい環境にゼロから建てる場合の手順:

1. **Supabase プロジェクト作成** (Region: Northeast Asia (Tokyo), Free プラン)
2. SQL Editor で [`supabase_schema.sql`](supabase_schema.sql) を実行
3. Project Settings → API で取得:
   - Project URL
   - publishable (anon) key
   - service_role secret key
4. GitHub リポジトリに **Secrets** 追加:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY` (service_role key)
5. [`docs/config.js`](docs/config.js) に `SUPABASE_URL` と `SUPABASE_ANON_KEY` (publishable key) をセット → commit
6. Settings → Pages: Source = `main` / folder = `/docs`
7. Actions タブ → "WordPress横断検索クローラ" → Run workflow で初回クロール
8. 必要なら Mac 側に `scripts/install_mac_crawl.sh` をセットアップ
9. 完了。URL は `https://<ユーザ名>.github.io/<リポジトリ名>/`

---

## トラブルシューティング

### サイトが 0 件のまま

1. クロールログを確認: `gh run view <RUN_ID> --repo terasakisd/sd-wp-search --log | grep "<サイト名>"`
2. **403 Forbidden**: 海外IP拒否 → Mac launchd 側に追加 (`CRAWL_ONLY_SITE_IDS` に追記)
3. **404 / 非JSON応答**: REST API が無効化されている → `scrape:` 設定でHTML経由
4. **401 Unauthorized**: 認証必須サイト、取得不可
5. **非JSON (text/html)**: WAF/プラグインで遮断、`scrape:` か諦め

### 検索結果に古い記事が出る (もう削除されているのに)

毎日 03:00 JST の purge_stale で消えるはず。すぐ消したい場合:
```bash
gh workflow run "WordPress横断検索クローラ" --repo terasakisd/sd-wp-search \
  -F purge_stale=true -F site_ids="該当サイト名"
```

### GitHub Actions が止まった

公開リポジトリでも 60日アクティビティ無しで停止します。本ワークフローは履歴ファイル `logs/crawl_history.log` を毎回 commit するので回避済み。それでも止まった場合は何でもいいから commit すれば復活。

### Supabase 容量警告

Free 枠は 500MB。`logs/crawl_history.log` 等の git 履歴ではなく、`posts` テーブルが肥大化した場合:
- 古い記事の本文を切り詰める (例: 5000文字でカット)
- もしくは Pro プラン ($25/月) に移行

---

## ファイル構成

```
wp-search/
├── README.md
├── sites.yaml                       # 編集するのはここがメイン
├── supabase_schema.sql              # 初回デプロイ時のみ使用
├── requirements.txt
├── .github/workflows/crawl.yml      # 定期クロール定義
├── docs/                            # GitHub Pages
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   └── config.js                    # Supabase 接続情報
├── app/                             # クローラ + (開発用) FastAPI
│   ├── crawler.py
│   ├── db_supabase.py
│   ├── db.py                        # 開発用 SQLite (本番未使用)
│   └── main.py                      # 開発用 FastAPI (本番未使用)
├── scripts/
│   ├── mac_crawl.sh                 # Mac launchd ラッパー
│   ├── com.wpsearch.crawl.plist     # launchd 定義
│   └── install_mac_crawl.sh         # Mac セットアップ
└── logs/
    └── crawl_history.log            # クロール実行履歴
```
