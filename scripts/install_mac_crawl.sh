#!/bin/bash
# Mac 上に launchd + pmset で 6時間ごとクロールをセットアップ
# 実行: bash scripts/install_mac_crawl.sh

set -e

PROJECT_DIR="/Users/macsazandaia/Downloads/wp-search"
PLIST_SRC="$PROJECT_DIR/scripts/com.wpsearch.crawl.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.wpsearch.crawl.plist"
WRAPPER="$PROJECT_DIR/scripts/mac_crawl.sh"

echo "=== Mac クロール自動化 セットアップ ==="
echo

# 1. .env の存在チェック
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "❌ .env が見つかりません。先に作成してください:"
    echo "   cp $PROJECT_DIR/.env.example $PROJECT_DIR/.env"
    echo "   # .env を編集して SUPABASE_URL と SUPABASE_SERVICE_KEY を埋める"
    exit 1
fi

# 2. ラッパースクリプトに実行権限
chmod +x "$WRAPPER"
echo "✓ $WRAPPER 実行権限付与"

# 3. plist を LaunchAgents へ配置
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$PROJECT_DIR/logs"

# 既存があればアンロード
if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi
cp "$PLIST_SRC" "$PLIST_DST"
launchctl load "$PLIST_DST"
echo "✓ launchd ジョブ登録 ($PLIST_DST)"

# 4. 動作確認
echo
echo "=== 登録状況 ==="
launchctl list | grep com.wpsearch.crawl || echo "  (見つからない場合は確認してください)"

# 5. スリープからの自動復帰スケジュール
echo
echo "=== スリープからの自動復帰 ==="
echo "Mac がスリープ中でも JST 02:55 に起こします (3:00 のクロール用)。"
echo "9:00/15:00/21:00 は通常 Mac が稼働中の時間なのでスキップ。"
echo "もし夜帰宅後など 21:00 にスリープしていた場合は次の起動時に遅延実行されます。"
echo
echo "次のコマンドを実行 (sudo パスワードが必要):"
echo "  sudo pmset repeat wakeorpoweron MTWRFSU 02:55:00"
echo

read -p "今すぐ実行しますか? [y/N] " ok
if [[ "$ok" == "y" || "$ok" == "Y" ]]; then
    sudo pmset repeat wakeorpoweron MTWRFSU 02:55:00
    echo
    echo "現在のスケジュール:"
    pmset -g sched
fi

echo
echo "=== 完了 ==="
echo
echo "次回クロール: 3:00 / 9:00 / 15:00 / 21:00 JST"
echo "対象サイト: \$CRAWL_ONLY_SITE_IDS (デフォルト: とうかい,愛代協,末松会計)"
echo
echo "手動テスト:"
echo "  bash $WRAPPER"
echo
echo "ログ確認:"
echo "  tail -f $PROJECT_DIR/logs/mac_crawl.log"
echo
echo "アンインストール:"
echo "  launchctl unload $PLIST_DST && rm $PLIST_DST"
echo "  sudo pmset repeat cancel"
