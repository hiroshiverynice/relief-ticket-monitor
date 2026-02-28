# RELIEF Ticket リセールチケット監視システム

STARTO ENTERTAINMENT所属アーティスト（Travis Japan, SixTONES等）の公式リセールサービス [RELIEF Ticket](https://relief-ticket.jp/) を自動監視し、チケットが出品された瞬間に LINE で通知するシステム。

## システム構成図

```
┌─────────────────────────────────────────────────────────────┐
│                    GitHub Actions (クラウド)                   │
│                      cron: 5分間隔                            │
│                                                             │
│  ┌───────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │  Checkout  │───▶│ Python 3.11  │───▶│   monitor.py     │  │
│  │  + Cache   │    │ + Playwright │    │   --once モード   │  │
│  └───────────┘    └──────────────┘    └────────┬─────────┘  │
│                                                │             │
│                   ┌────────────────────────────┘             │
│                   ▼                                          │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                    スクレイピング                         │  │
│  │                                                        │  │
│  │  1. アーティストページ取得                                │  │
│  │     /events/artist/38 (Travis Japan)                   │  │
│  │     /events/artist/40 (SixTONES)                       │  │
│  │                                                        │  │
│  │  2. 各イベントページを巡回                                │  │
│  │     /events/artist/38/124 (ツアー本公演)                 │  │
│  │     /events/artist/38/123 (追加公演)                     │  │
│  │     /events/artist/40/127 (MILESixTONES)               │  │
│  │                                                        │  │
│  │  3. リセール在庫判定                                     │  │
│  │     .ticket-select 存在 → 在庫あり!                      │  │
│  │     .perform-list.text-muted → 在庫なし                  │  │
│  └────────────────────────┬───────────────────────────────┘  │
│                           │                                  │
│            ┌──────────────┴──────────────┐                   │
│            ▼                             ▼                   │
│     在庫なし                       在庫あり!                   │
│     → ログ出力のみ                  → LINE通知送信             │
│                                         │                   │
│  ┌──────────────┐                       │                   │
│  │  state.json   │ ← 通知済みチケット記録  │                   │
│  │  (Actions     │   (重複通知防止)       │                   │
│  │   Cache)      │                       │                   │
│  └──────────────┘                       │                   │
└─────────────────────────────────────────┼───────────────────┘
                                          │
                                          ▼
                              ┌──────────────────────┐
                              │  LINE Messaging API   │
                              │  Push Message         │
                              └──────────┬───────────┘
                                         │
                                         ▼
                              ┌──────────────────────┐
                              │    📱 LINE アプリ      │
                              │                      │
                              │  🎫 リセールチケット出品! │
                              │  【Travis Japan】      │
                              │  📅 2026/03/28 12:00  │
                              │  📍 横浜アリーナ        │
                              │  🎟 (1枚, 2枚)        │
                              │  ▶ 購入ページ: ...     │
                              └──────────────────────┘
```

## チケット検出ロジック

RELIEF Ticket のイベントページは、公演ごとに以下の HTML 構造を持つ：

```
在庫なし（グレー表示）           在庫あり（購入可能）
─────────────────────       ─────────────────────
div.perform-list             div.perform-list
    .text-muted  ← ★            (text-mutedなし) ← ★
  ┌─────────────────┐        ┌─────────────────────┐
  │ 2026/03/28 12:00│        │ 2026/03/28 12:00    │
  │ [神奈川] 横浜    │        │ [神奈川] 横浜        │
  │ アリーナ         │        │ アリーナ             │
  │                 │        │                     │
  │ (何もなし)       │        │ [▼ 1枚 ▼] ← select │
  │                 │        │ [購入手続きへ] ← btn │
  └─────────────────┘        └─────────────────────┘
```

3段階のフォールバックで検出：

| 優先度 | 方法 | セレクタ | 信頼度 |
|--------|------|----------|--------|
| 1 | 枚数選択UI | `.ticket-select` | 最高 |
| 2 | アクティブ公演 | `.perform-list` に `text-muted` なし | 高 |
| 3 | 購入ボタン | `button` 内に「購入手続き」 | 中 |

## ファイル構成

```
relief_ticket_monitor/
├── .github/
│   └── workflows/
│       └── monitor.yml      # GitHub Actions ワークフロー（5分cron）
├── monitor.py               # メインスクリプト（スクレイピング + 通知）
├── requirements.txt          # Python依存パッケージ
├── .env.example              # 環境変数テンプレート
├── .env                      # 実際の設定（Git管理外）
├── .gitignore
└── README.md
```

## 技術スタック

| コンポーネント | 技術 | 用途 |
|---------------|------|------|
| スクレイピング | Playwright (Chromium) | JSレンダリング後のDOM解析 |
| HTML解析 | BeautifulSoup4 | CSSセレクタによるチケット検出 |
| 通知 | LINE Messaging API | Push Messageでスマホ即時通知 |
| 通知(ローカル) | macOS osascript | デスクトップ通知（ローカル実行時） |
| スケジューラ | GitHub Actions cron | 5分間隔の自動実行 |
| 状態管理 | state.json + Actions Cache | 通知済みチケットの重複排除 |
| 実行環境 | GitHub Actions (Ubuntu) | 無料・24/7稼働 |

## 実行モード

### クラウド（GitHub Actions）— 本番運用

- 5分ごとに自動実行（`*/5 * * * *`）
- `python monitor.py --once` で1回チェック → 終了
- 状態は Actions Cache で実行間を跨いで保持
- Secrets に LINE トークンを格納

### ローカル（macOS）— デバッグ・開発用

```bash
cd ~/relief_ticket_monitor
python3 monitor.py          # 60秒間隔のループ実行
python3 monitor.py --once   # 1回だけ実行
DEBUG=true python3 monitor.py --once  # スクリーンショット保存
```

## 環境変数 / Secrets

| 変数名 | 設定場所 | 説明 |
|--------|----------|------|
| `LINE_CHANNEL_ACCESS_TOKEN` | GitHub Secrets | LINE Messaging API チャネルアクセストークン（長期） |
| `LINE_USER_ID` | GitHub Secrets | 通知先の LINE ユーザーID（`U`で始まる） |
| `ARTISTS` | GitHub Variables | 監視対象アーティスト（カンマ区切り） |
| `CHECK_INTERVAL` | .env（ローカル用） | チェック間隔（秒）。GitHub Actions では cron が制御 |
| `DEBUG` | .env（ローカル用） | `true` でスクリーンショットを `debug/` に保存 |

## RELIEF Ticket サイト構造

```
relief-ticket.jp/
├── /                              # トップ（アーティスト一覧）
├── /events/artist/38              # Travis Japan イベント一覧
│   ├── /events/artist/38/124      #   └ ツアー本公演 → 公演リスト
│   └── /events/artist/38/123      #   └ 追加公演 → 公演リスト
├── /events/artist/40              # SixTONES イベント一覧
│   └── /events/artist/40/127      #   └ MILESixTONES → 公演リスト
├── /events/artist/41              # King & Prince
├── /events/artist/42              # 中島健人
└── /events/artist/15              # ジュニア
```

## 処理フロー（シーケンス図）

```
GitHub Actions          RELIEF Ticket           LINE API          📱 スマホ
    │                       │                      │                 │
    │── GET /events/38 ────▶│                      │                 │
    │◀── イベント一覧 ───────│                      │                 │
    │                       │                      │                 │
    │── GET /events/38/124 ▶│                      │                 │
    │◀── 公演一覧HTML ──────│                      │                 │
    │                       │                      │                 │
    │  [HTML解析]           │                      │                 │
    │  .ticket-select       │                      │                 │
    │  あり → 在庫検出!      │                      │                 │
    │                       │                      │                 │
    │  [state.json確認]     │                      │                 │
    │  未通知 → 新規!        │                      │                 │
    │                       │                      │                 │
    │──────── POST /v2/bot/message/push ──────────▶│                 │
    │◀──────── 200 OK ───────────────────────────│                 │
    │                       │                      │── 🎫 通知 ─────▶│
    │                       │                      │                 │
    │  [state.json更新]     │                      │                 │
    │  → Actions Cache保存  │                      │                 │
    │                       │                      │                 │
```

## 注意事項

- RELIEF Ticket の利用規約（第18条）では、サービスに影響を与える外部ツールの使用が禁止されています
- 本システムは5分に1回のアクセスであり、通常のブラウジングと同程度の負荷です
- 自動購入機能は搭載していません（通知のみ）
- チケットの購入は必ず手動で行ってください
