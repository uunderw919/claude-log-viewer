# Claude Code Log Viewer

`~/.claude/projects/` に溜まる Claude Code の会話ログ（`.jsonl`）を、
Mac で手軽に **読み返す・検索する・一覧する** ためのローカル Web ビューアー。

- 依存ゼロ（Python 標準ライブラリのみ・`pip install` 不要）
- 読み取り専用（ログは一切書き換えません）
- 単一ファイル `viewer.py` の一発起動

## 機能

- **セッション一覧**: プロジェクト（実際の `cwd`）ごとにセッションをツリー表示。
- **会話表示**: user は右寄せ吹き出し、assistant は左寄せ。
  既定は `thinking` / `tool_use` / `tool_result` を折りたたみ、クリックで展開。
- **生ログ表示トグル**: ヘッダのチェックで `system` / `attachment` も含め全て展開表示。
- **横断検索**: 全セッションを大文字小文字無視で検索。ヒットをクリックすると
  該当セッションを開き、その位置へスクロール＆ハイライト。

## 起動方法

```sh
python3 viewer.py
```

`http://127.0.0.1:8765/` にサーバが立ち、ブラウザが自動で開きます。
停止は `Ctrl+C`。

### オプション

| オプション | 説明 | 既定 |
|---|---|---|
| `--dir PATH` | ログ対象ディレクトリ | `~/.claude/projects/` |
| `--port N` | 待ち受けポート | `8765` |
| `--no-open` | 起動時にブラウザを自動で開かない | （開く） |

例:

```sh
python3 viewer.py --port 9000 --no-open
python3 viewer.py --dir /path/to/other/projects
```

## 動作環境

macOS + Python 3.12 で確認。

## ライセンス

[MIT License](LICENSE)
