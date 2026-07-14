# Claude Code Log Viewer — 設計書

作成: 2026-07-14 / 状態: 承認済み（実装待ち）

## 1. 目的とスコープ

Claude Code が `~/.claude/projects/<encoded-project-dir>/<session-id>.jsonl` に
残す会話ログを、Mac で手軽に **読み返す・検索する・一覧する** ローカル Web アプリ。

- ゴール（全部入り）: ①過去会話を読み返す ②全セッション横断で検索 ③一覧から選んで開く
- 非ゴール（YAGNI）: 編集・削除・エクスポート・複数 OS 対応・認証・ログの改変

## 2. 方式（承認済み A 案）

Python 標準ライブラリのみの常駐 HTTP サーバ + 1 枚の HTML フロント。

- 起動: `python3 viewer.py`（任意で `--dir <path>` `--port <n>` `--no-open`）
- 起動時に `localhost:<port>` を立て、ブラウザを自動で開く（`webbrowser` モジュール）
- サーバは JSON API を返すだけ。描画はフロント（インライン JS）。
- 依存ゼロ: `http.server`, `socketserver`, `json`, `pathlib`, `webbrowser`, `argparse`, `html`, `datetime`。

## 3. ログ構造（実物で確認済み）

各 `.jsonl` は 1 行 1 イベントの JSON。`type` で分類する。

| type | 扱い |
|---|---|
| `user` | ユーザー発言。`message.role="user"`。`message.content` は **str**（ツール結果を含む時は **list**） |
| `assistant` | アシスタント返答。`message.content` は **list**（ブロック: `text` / `thinking` / `tool_use`） |
| `system` | システムメッセージ。生ログ表示時のみ出す |
| `attachment` | フック出力等。生ログ表示時のみ出す |
| `ai-title` | 自動生成タイトル。**セッション一覧の見出し**に使う |
| `mode` / `permission-mode` / `last-prompt` / `file-history-snapshot` / `queue-operation` | メタ情報。無視 |

補足:
- どのメッセージ行にも `cwd` が入っており、これが **本当のプロジェクトパス**。
  ディレクトリ名（`-Users-road-dev-...`）のダッシュ復元は不要。cwd を採用、無ければディレクトリ名フォールバック。
- assistant の `content` 各ブロックは `{"type": "text"|"thinking"|"tool_use", ...}`。
  `tool_use` は `name` と `input`（`input.description` があれば見出しに使う）。
- user が list の時は `tool_result` ブロックを含む。
- **壊れた行・未知の type はスキップして落ちない**こと。

## 4. サーバ API

すべて JSON を返す GET。

- `GET /` — フロントの HTML を返す
- `GET /api/sessions`
  - 全プロジェクト×全セッションを走査。返り値: 配列
    `{ project: <cwd or dir>, sessionId, title, mtime(ISO), messageCount }`
  - project 単位でグルーピングしやすい形。mtime 降順推奨。
  - 一覧は軽量に: 各 jsonl を舐めて title(ai-title) と user/assistant 件数、mtime だけ取る。
- `GET /api/session?id=<uuid>` — 1 セッションをパースし、表示用に整形した配列
  `{ role, blocks:[{kind, text/…}], ts }` を順序どおり返す。
- `GET /api/search?q=<語>` — 全セッション走査、ヒット行を
  `{ sessionId, project, role, snippet(前後文脈), index }` で返す。大文字小文字無視。

## 5. フロント（1 ページ・3 ペイン）

- 左ペイン: 上に検索ボックス。下に「プロジェクト > セッション（タイトル・日時）」ツリー。
- 右ペイン: 選択セッションの会話。
  - user = 右寄せ吹き出し、assistant = 左寄せ。
  - 既定は **ツール折りたたみ**: `thinking` は淡色で折りたたみ、`tool_use` は「▶ Bash: <説明>」、`tool_result` も折りたたみ。クリックで展開。
  - ヘッダのトグルで **生ログ表示**（system/attachment も含め全ブロックを順に展開表示）。
- 検索: ヒット一覧をクリック → 該当セッションを開き、その位置へスクロール＆ハイライト。
- Markdown は軽く整形（コードブロック・インラインコード・改行程度で十分。凝った描画は不要）。
- 見た目はダーク基調でシンプルに。ライブラリ読み込みなし（CDN 不可・全部インライン）。

## 6. 実装の分割（動く単位でコミット）

1. `viewer.py`: サーバ雛形 + `/api/sessions`（一覧が JSON で返る）
2. `/api/session`: 1 セッションのパースと整形
3. フロント HTML: 一覧表示 + 会話表示（折りたたみ）
4. `/api/search` + 検索 UI + ジャンプ
5. 生ログトグル、起動オプション（--dir/--port/--no-open）、README

## 7. エラー処理・堅牢性

- jsonl の壊れ行は try/except でスキップ。ファイル欠損・空も落ちない。
- 対象ディレクトリが無ければ分かるメッセージを出して終了。
- 大きい jsonl（数十 MB）でも一覧は軽量スキャンで開けること。会話表示は 1 ファイル読み切りで可。

## 8. テスト方針

- 実物のログ（`~/.claude/projects/`）で起動して 3 機能が動くことを目視確認。
- パース関数は代表的な行（user str / user list / assistant blocks / 壊れ行）で単体的に確認。
