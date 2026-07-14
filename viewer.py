#!/usr/bin/env python3
"""Claude Code Log Viewer.

~/.claude/projects/ に溜まる Claude Code の会話ログ(.jsonl)を、
ローカルの Web ビューアーで読み返す・検索する・一覧する。

- 依存ゼロ(Python 標準ライブラリのみ、pip install 不要)
- 読み取り専用: ログは絶対に書き換えない
- 使い方: python3 viewer.py [--dir PATH] [--port N] [--no-open]

DESIGN.md が仕様のソースオブトゥルース。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_DIR = Path.home() / ".claude" / "projects"
DEFAULT_PORT = 8765

# セッション一覧の見出しに使う自動生成タイトル以外の「無視」type。
_IGNORED_TYPES = {
    "ai-title",
    "mode",
    "permission-mode",
    "last-prompt",
    "file-history-snapshot",
    "queue-operation",
    "pr-link",
}

# path traversal を防ぐため、セッション id はこの形だけ許可する。
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# --------------------------------------------------------------------------
# ログ走査・パース
# --------------------------------------------------------------------------

def iter_session_files(root: Path):
    """root 直下の各プロジェクトディレクトリにある *.jsonl を列挙する。"""
    if not root.is_dir():
        return
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        for jsonl in sorted(project_dir.glob("*.jsonl")):
            yield jsonl


def _iter_json_lines(path: Path):
    """1 行 1 JSON を yield する。壊れた行はスキップして落ちない。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return


def _decode_dir_name(name: str) -> str:
    """cwd が取れない時のフォールバック。'-Users-me-proj' → '/Users/me/proj'。"""
    return name.replace("-", "/")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat()


def session_meta(path: Path) -> dict:
    """一覧用の軽量メタ情報。title(ai-title) と user/assistant 件数、mtime、project。"""
    title = ""
    message_count = 0
    project = ""
    for obj in _iter_json_lines(path):
        t = obj.get("type")
        if t == "ai-title":
            title = obj.get("aiTitle") or title
        elif t in ("user", "assistant"):
            message_count += 1
        if not project:
            cwd = obj.get("cwd")
            if cwd:
                project = cwd
    if not project:
        project = _decode_dir_name(path.parent.name)
    try:
        mtime = _iso(path.stat().st_mtime)
    except OSError:
        mtime = ""
    return {
        "project": project,
        "sessionId": path.stem,
        "title": title or path.stem,
        "mtime": mtime,
        "messageCount": message_count,
    }


def scan_sessions(root: Path) -> list[dict]:
    """全プロジェクト×全セッションのメタ情報を mtime 降順で返す。"""
    sessions = [session_meta(p) for p in iter_session_files(root)]
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def _stringify(content) -> str:
    """tool_result などの content(str または list ブロック)を平文にする。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block.get("text") or ""))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _blocks_from_message(msg: dict) -> list[dict]:
    """assistant / user(list) の content を表示用ブロック配列に変換する。"""
    content = msg.get("content")
    blocks: list[dict] = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"kind": "text", "text": content})
        return blocks
    if not isinstance(content, list):
        return blocks
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            blocks.append({"kind": "text", "text": block.get("text") or ""})
        elif bt == "thinking":
            blocks.append({"kind": "thinking", "text": block.get("thinking") or ""})
        elif bt == "tool_use":
            inp = block.get("input") or {}
            desc = inp.get("description") if isinstance(inp, dict) else ""
            blocks.append({
                "kind": "tool_use",
                "name": block.get("name") or "tool",
                "description": desc or "",
                "input": json.dumps(inp, ensure_ascii=False, indent=2),
            })
        elif bt == "tool_result":
            blocks.append({
                "kind": "tool_result",
                "text": _stringify(block.get("content")),
                "isError": bool(block.get("is_error")),
            })
    return blocks


def parse_session(path: Path) -> list[dict]:
    """1 セッションを表示用に整形した配列を順序どおり返す。

    要素: { role, blocks:[{kind, ...}], ts }
    system / attachment は生ログ表示時のみフロントが出す(role で判別)。
    """
    items: list[dict] = []
    for obj in _iter_json_lines(path):
        t = obj.get("type")
        ts = obj.get("timestamp") or ""
        if t == "user":
            msg = obj.get("message") or {}
            blocks = _blocks_from_message(msg)
            if blocks:
                # ツール結果だけの user 行は、モデルへの応答であって
                # ユーザー発言ではないので assistant 側に寄せて描画する。
                role = "tool_result" if all(b["kind"] == "tool_result" for b in blocks) else "user"
                items.append({"role": role, "blocks": blocks, "ts": ts})
        elif t == "assistant":
            msg = obj.get("message") or {}
            blocks = _blocks_from_message(msg)
            if blocks:
                items.append({"role": "assistant", "blocks": blocks, "ts": ts})
        elif t == "system":
            text = _stringify(obj.get("content")) or obj.get("subtype") or ""
            items.append({
                "role": "system",
                "blocks": [{"kind": "system", "text": str(text)}],
                "ts": ts,
            })
        elif t == "attachment":
            att = obj.get("attachment")
            text = json.dumps(att, ensure_ascii=False, indent=2) if att is not None else ""
            items.append({
                "role": "attachment",
                "blocks": [{"kind": "attachment", "text": text}],
                "ts": ts,
            })
        # ai-title やメタ type、未知 type、壊れ行はスキップ。
    return items


def _block_text(block: dict) -> str:
    """検索・スニペット用にブロックから平文を取り出す。"""
    kind = block.get("kind")
    if kind == "tool_use":
        return f"{block.get('name', '')} {block.get('description', '')}\n{block.get('input', '')}"
    return block.get("text") or ""


def search_all(root: Path, query: str) -> list[dict]:
    """全セッションを走査し、ヒットしたメッセージを返す(大文字小文字無視)。"""
    q = query.strip().lower()
    if not q:
        return []
    results: list[dict] = []
    for path in iter_session_files(root):
        meta = session_meta(path)
        items = parse_session(path)
        for index, item in enumerate(items):
            joined = "\n".join(_block_text(b) for b in item["blocks"])
            lower = joined.lower()
            pos = lower.find(q)
            if pos == -1:
                continue
            start = max(0, pos - 60)
            end = min(len(joined), pos + len(q) + 60)
            snippet = joined[start:end].replace("\n", " ")
            if start > 0:
                snippet = "…" + snippet
            if end < len(joined):
                snippet = snippet + "…"
            results.append({
                "sessionId": meta["sessionId"],
                "project": meta["project"],
                "title": meta["title"],
                "role": item["role"],
                "snippet": snippet,
                "index": index,
            })
    return results


# --------------------------------------------------------------------------
# HTTP サーバ
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    root: Path = DEFAULT_DIR

    def log_message(self, *args):  # 標準の逐一ログを抑制
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _find_session_file(self, session_id: str) -> Path | None:
        if not session_id or not _ID_RE.match(session_id):
            return None
        for path in iter_session_files(self.root):
            if path.stem == session_id:
                return path
        return None

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)
        try:
            if route == "/":
                self._send_html(INDEX_HTML)
            elif route == "/api/sessions":
                self._send_json(scan_sessions(self.root))
            elif route == "/api/session":
                sid = (params.get("id") or [""])[0]
                path = self._find_session_file(sid)
                if path is None:
                    self._send_json({"error": "not found"}, status=404)
                else:
                    self._send_json(parse_session(path))
            elif route == "/api/search":
                q = (params.get("q") or [""])[0]
                self._send_json(search_all(self.root, q))
            else:
                self._send_json({"error": "not found"}, status=404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # 何があっても 500 を返して落ちない
            try:
                self._send_json({"error": str(exc)}, status=500)
            except OSError:
                pass


# --------------------------------------------------------------------------
# フロント(1 ページ・CSS/JS インライン)
# --------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code Log Viewer</title>
<style>
  :root {
    --bg: #1a1b1e; --panel: #232429; --panel2: #2b2d33; --border: #35373d;
    --fg: #e6e7ea; --muted: #9aa0aa; --accent: #7aa2f7; --user: #2d4a6b;
    --thinking: #7d818c; --hl: #b58900;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    background: var(--bg); color: var(--fg); font: 14px/1.6 -apple-system,
    "Hiragino Sans", "Yu Gothic", system-ui, sans-serif; display: flex;
    flex-direction: column; height: 100vh; overflow: hidden;
  }
  header {
    display: flex; align-items: center; gap: 12px; padding: 8px 14px;
    background: var(--panel); border-bottom: 1px solid var(--border); flex: 0 0 auto;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; }
  header .spacer { flex: 1; }
  header label { color: var(--muted); font-size: 13px; cursor: pointer; user-select: none; }
  .main { display: flex; flex: 1; min-height: 0; }
  .left {
    width: 320px; flex: 0 0 320px; background: var(--panel);
    border-right: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0;
  }
  .search { padding: 10px; border-bottom: 1px solid var(--border); }
  .search input {
    width: 100%; padding: 7px 9px; background: var(--panel2); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px; font-size: 13px;
  }
  .list { overflow-y: auto; flex: 1; min-height: 0; }
  .right { flex: 1; overflow-y: auto; padding: 18px 22px; min-width: 0; }
  .project { margin-top: 6px; }
  .project > .phead {
    padding: 6px 12px; color: var(--muted); font-size: 12px; font-weight: 600;
    letter-spacing: .02em; position: sticky; top: 0; background: var(--panel);
    cursor: pointer; user-select: none; word-break: break-all;
  }
  .session {
    padding: 7px 12px 7px 20px; cursor: pointer; border-left: 3px solid transparent;
  }
  .session:hover { background: var(--panel2); }
  .session.active { background: var(--panel2); border-left-color: var(--accent); }
  .session .stitle { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .session .smeta { font-size: 11px; color: var(--muted); }
  .searchres { display: none; }
  .searchres.on { display: block; }
  .tree.hidden { display: none; }
  .hit { padding: 8px 12px; cursor: pointer; border-bottom: 1px solid var(--border); }
  .hit:hover { background: var(--panel2); }
  .hit .htitle { font-size: 12px; color: var(--accent); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hit .hsnip { font-size: 12px; color: var(--muted); }
  .hit mark { background: var(--hl); color: #000; }

  .msg { margin: 14px 0; display: flex; }
  .msg.user { justify-content: flex-end; }
  .bubble { max-width: 78%; padding: 4px 14px; border-radius: 10px; }
  .msg.user .bubble { background: var(--user); }
  .msg.assistant .bubble, .msg.tool_result .bubble { background: var(--panel); border: 1px solid var(--border); }
  .msg.system .bubble, .msg.attachment .bubble {
    background: transparent; border: 1px dashed var(--border); color: var(--muted);
    max-width: 100%; font-size: 12px;
  }
  .role { font-size: 11px; color: var(--muted); margin: 0 6px; }
  .msg.user .role { text-align: right; }
  .txt p { margin: 8px 0; }
  .txt pre {
    background: #14151a; border: 1px solid var(--border); border-radius: 6px;
    padding: 10px 12px; overflow-x: auto; font-size: 12.5px; line-height: 1.45;
  }
  .txt code {
    background: #14151a; border: 1px solid var(--border); border-radius: 4px;
    padding: 1px 5px; font-size: 12.5px;
  }
  .txt pre code { background: none; border: none; padding: 0; }
  details { margin: 6px 0; }
  details > summary {
    cursor: pointer; color: var(--muted); font-size: 12.5px; list-style: none;
    user-select: none; padding: 2px 0;
  }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before { content: "▶ "; }
  details[open] > summary::before { content: "▼ "; }
  details.thinking > summary { color: var(--thinking); font-style: italic; }
  details.tool > summary { color: var(--accent); }
  details .body {
    margin: 4px 0 4px 14px; padding: 8px 10px; background: #14151a;
    border: 1px solid var(--border); border-radius: 6px; white-space: pre-wrap;
    word-break: break-word; font-size: 12.5px; overflow-x: auto; font-family: ui-monospace, monospace;
  }
  details.thinking .body { color: var(--thinking); font-style: italic; font-family: inherit; white-space: pre-wrap; }
  .body.error { border-color: #b04a4a; }
  .placeholder { color: var(--muted); text-align: center; margin-top: 40px; }
  mark.jump { background: var(--hl); color: #000; }
  .msg.flash { animation: flash 1.6s ease-out; }
  @keyframes flash { 0% { background: rgba(181,137,0,.28); } 100% { background: transparent; } }
</style>
</head>
<body>
<header>
  <h1>Claude Code Log Viewer</h1>
  <span class="spacer"></span>
  <label><input type="checkbox" id="rawToggle"> 生ログ表示</label>
</header>
<div class="main">
  <div class="left">
    <div class="search"><input id="q" type="search" placeholder="全セッション横断検索…"></div>
    <div class="list">
      <div id="tree" class="tree"></div>
      <div id="searchres" class="searchres"></div>
    </div>
  </div>
  <div id="right" class="right">
    <div class="placeholder">左のセッションを選ぶか、上で検索してください。</div>
  </div>
</div>
<script>
const $ = (s, el=document) => el.querySelector(s);
const state = { sessions: [], current: null, items: [], raw: false };

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

// 軽い Markdown: escape → フェンス/インラインコード/改行。
function md(src) {
  const parts = String(src).split(/```/);
  let out = '';
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {
      let body = parts[i].replace(/^[a-zA-Z0-9_-]*\n/, '');
      out += '<pre><code>' + esc(body) + '</code></pre>';
    } else {
      let t = esc(parts[i]);
      t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
      t = t.replace(/\n/g, '<br>');
      out += t;
    }
  }
  return out;
}

function fmtTime(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleString(); } catch(e) { return iso; }
}

async function loadSessions() {
  const res = await fetch('/api/sessions');
  state.sessions = await res.json();
  renderTree();
}

function renderTree() {
  const groups = {};
  for (const s of state.sessions) (groups[s.project] = groups[s.project] || []).push(s);
  const tree = $('#tree');
  tree.innerHTML = '';
  for (const project of Object.keys(groups)) {
    const box = document.createElement('div');
    box.className = 'project';
    const head = document.createElement('div');
    head.className = 'phead';
    head.textContent = project;
    box.appendChild(head);
    for (const s of groups[project]) {
      const el = document.createElement('div');
      el.className = 'session';
      el.dataset.id = s.sessionId;
      el.innerHTML = '<div class="stitle">' + esc(s.title) + '</div>' +
        '<div class="smeta">' + fmtTime(s.mtime) + ' · ' + s.messageCount + ' msgs</div>';
      el.onclick = () => openSession(s.sessionId);
      box.appendChild(el);
    }
    tree.appendChild(box);
  }
}

function markActive(id) {
  for (const el of document.querySelectorAll('.session'))
    el.classList.toggle('active', el.dataset.id === id);
}

async function openSession(id, jumpIndex=null) {
  const res = await fetch('/api/session?id=' + encodeURIComponent(id));
  if (!res.ok) return;
  state.current = id;
  state.items = await res.json();
  markActive(id);
  renderConversation();
  if (jumpIndex != null) jumpTo(jumpIndex);
}

function renderBlock(b) {
  if (b.kind === 'text') return '<div class="txt">' + md(b.text) + '</div>';
  if (b.kind === 'thinking') {
    const open = state.raw ? ' open' : '';
    return '<details class="thinking"' + open + '><summary>thinking</summary>' +
      '<div class="body">' + esc(b.text) + '</div></details>';
  }
  if (b.kind === 'tool_use') {
    const open = state.raw ? ' open' : '';
    const label = esc(b.name) + (b.description ? ': ' + esc(b.description) : '');
    return '<details class="tool"' + open + '><summary>' + label + '</summary>' +
      '<div class="body">' + esc(b.input) + '</div></details>';
  }
  if (b.kind === 'tool_result') {
    const open = state.raw ? ' open' : '';
    const cls = b.isError ? 'body error' : 'body';
    return '<details class="tool"' + open + '><summary>結果' + (b.isError ? ' (error)' : '') +
      '</summary><div class="' + cls + '">' + esc(b.text) + '</div></details>';
  }
  if (b.kind === 'system' || b.kind === 'attachment')
    return '<div class="body">' + esc(b.text) + '</div>';
  return '';
}

function renderConversation() {
  const right = $('#right');
  right.innerHTML = '';
  if (!state.items.length) {
    right.innerHTML = '<div class="placeholder">(表示できるメッセージがありません)</div>';
    return;
  }
  state.items.forEach((item, i) => {
    // 生ログ表示以外では system / attachment を隠す。
    if (!state.raw && (item.role === 'system' || item.role === 'attachment')) return;
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + item.role;
    wrap.dataset.idx = i;
    const inner = item.blocks.map(renderBlock).join('');
    const label = item.role === 'tool_result' ? 'tool result' : item.role;
    wrap.innerHTML = '<div><div class="role">' + label + '</div>' +
      '<div class="bubble">' + inner + '</div></div>';
    right.appendChild(wrap);
  });
}

function jumpTo(index) {
  const el = document.querySelector('.msg[data-idx="' + index + '"]');
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
}

// --- 検索 ---
let searchTimer = null;
async function runSearch(q) {
  const box = $('#searchres');
  if (!q.trim()) {
    box.classList.remove('on');
    $('#tree').classList.remove('hidden');
    return;
  }
  const res = await fetch('/api/search?q=' + encodeURIComponent(q));
  const hits = await res.json();
  box.innerHTML = '';
  if (!hits.length) {
    box.innerHTML = '<div class="hit"><div class="hsnip">ヒットなし</div></div>';
  } else {
    for (const h of hits) {
      const el = document.createElement('div');
      el.className = 'hit';
      const snip = esc(h.snippet).replace(new RegExp('(' + escapeRe(q) + ')', 'ig'), '<mark>$1</mark>');
      el.innerHTML = '<div class="htitle">' + esc(h.title) + '</div>' +
        '<div class="hsnip">' + h.role + ': ' + snip + '</div>';
      el.onclick = () => openSession(h.sessionId, h.index);
      box.appendChild(el);
    }
  }
  box.classList.add('on');
  $('#tree').classList.add('hidden');
}
function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

$('#q').addEventListener('input', e => {
  clearTimeout(searchTimer);
  const q = e.target.value;
  searchTimer = setTimeout(() => runSearch(q), 220);
});

$('#rawToggle').addEventListener('change', e => {
  state.raw = e.target.checked;
  if (state.current) renderConversation();
});

loadSessions();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 起動
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Claude Code Log Viewer")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                        help=f"ログ対象ディレクトリ (既定: {DEFAULT_DIR})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"待ち受けポート (既定: {DEFAULT_PORT})")
    parser.add_argument("--no-open", action="store_true",
                        help="起動時にブラウザを自動で開かない")
    args = parser.parse_args(argv)

    root = args.dir.expanduser()
    if not root.is_dir():
        print(f"エラー: ログディレクトリが見つかりません: {root}", file=sys.stderr)
        print("--dir でパスを指定してください。", file=sys.stderr)
        return 1

    Handler.root = root
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"エラー: ポート {args.port} を開けません: {exc}", file=sys.stderr)
        print("--port で別のポートを指定してください。", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{args.port}/"
    print(f"Claude Code Log Viewer を起動しました: {url}")
    print(f"対象ディレクトリ: {root}")
    print("停止するには Ctrl+C。")

    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
