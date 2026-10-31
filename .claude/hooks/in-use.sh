#!/usr/bin/env bash
# 2台で同じリポジトリを同時に触らないための「使用中フラグ」。
# GitHub のブランチ in-use があれば使用中で、中身は空コミット1個（件名が「端末名 時刻 備考」）。
# 作成は通常の push（既にあれば拒否される）なので、同時に取りに行っても勝つのは1台だけ。
#
#   start    SessionStart: フラグを取って pull し、状況を Claude に伝える
#   prompt   UserPromptSubmit: 相手が使用中なら入力を止める（exit 2）
#   end      SessionEnd: push してフラグを消す。push しきれない作業があればフラグを残す
#   status   今の持ち主を表示する
#   takeover 相手のフラグを奪う（相手が終了できないとき用。手で実行する）
set -u

ROOT=${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}
FLAG=refs/heads/in-use
RECHECK_MIN=5
# パスフレーズ入力やオフラインで hook が止まらないようにする
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh} -o BatchMode=yes -o ConnectTimeout=5"

g() { git -C "$ROOT" "$@"; }

if [ -n "${IN_USE_HOST:-}" ]; then
  HOST=$IN_USE_HOST
elif [ "$(uname)" = Darwin ]; then
  HOST=mac # macOS のホスト名はネットワークで変わることがある
else
  HOST=$(hostname -s)
fi

GIT_DIR_ABS=$(g rev-parse --absolute-git-dir) || exit 0
HELD=$GIT_DIR_ABS/in-use-held       # 自分のフラグの sha。更新時刻が最後に確認した時刻
OFFLINE=$GIT_DIR_ABS/in-use-offline # 更新時刻が最後に接続に失敗した時刻

fresh() { [ -n "$(find "$1" -mmin -"$RECHECK_MIN" 2>/dev/null)" ]; }

new_flag() {
  local note=$1 tree
  tree=$(g mktree </dev/null)
  GIT_AUTHOR_NAME=in-use GIT_AUTHOR_EMAIL=in-use@localhost \
    GIT_COMMITTER_NAME=in-use GIT_COMMITTER_EMAIL=in-use@localhost \
    g commit-tree "$tree" -m "$HOST $(date '+%Y-%m-%d %H:%M')${note:+ $note}"
}

# sha のフラグの件名を返す
flag_subject() {
  g fetch -q origin "+$FLAG:refs/remotes/origin/in-use" 2>/dev/null
  g log -1 --format=%s "$1" 2>/dev/null
}

# フラグを取る。0=自分のもの 1=相手のもの（HOLDER に件名）2=GitHub に繋がらない
# 新しく取ったときだけ CLAIMED=1
CLAIMED=0
HOLDER=
claim() {
  local out sha subject
  out=$(g ls-remote origin "$FLAG" 2>/dev/null) || return 2
  if [ -z "$out" ]; then
    sha=$(new_flag "")
    if g push -q origin "$sha:$FLAG" 2>/dev/null; then
      echo "$sha" >"$HELD"
      CLAIMED=1
      return 0
    fi
    # 確認と push の間に相手が先に取った
    out=$(g ls-remote origin "$FLAG" 2>/dev/null) || return 2
    [ -n "$out" ] || return 2
  fi
  sha=${out%%[[:space:]]*}
  if [ "$sha" = "$(cat "$HELD" 2>/dev/null)" ]; then
    touch "$HELD"
    return 0
  fi
  subject=$(flag_subject "$sha")
  if [ "${subject%% *}" = "$HOST" ]; then
    echo "$sha" >"$HELD" # 異常終了や別セッションの分を引き継ぐ
    return 0
  fi
  rm -f "$HELD"
  HOLDER=$subject
  return 1
}

pull_report() {
  local before after err
  before=$(g rev-parse HEAD)
  if ! err=$(g pull -q --ff-only 2>&1 </dev/null); then
    echo "[使用中フラグ] pull できません。手で確認してください: $err"
    return
  fi
  after=$(g rev-parse HEAD)
  if [ "$before" != "$after" ]; then
    echo "[使用中フラグ] GitHub から取り込んだコミット:"
    g log --oneline "$before..$after"
  fi
}

handoff_report() {
  local items
  items=$(grep -F -- '- [ ]' "$ROOT/handoff.md" 2>/dev/null)
  if [ -n "$items" ]; then
    echo "[申し送り] handoff.md の未対応（最初の返答でユーザーに短く伝える）:"
    echo "$items"
  fi
}

offline_report() {
  touch "$OFFLINE"
  echo "[使用中フラグ] GitHub に接続できないので、フラグの確認と pull をしていません。相手の使用中は分かりません。"
}

cmd_start() {
  claim
  case $? in
  0)
    rm -f "$OFFLINE"
    echo "[使用中フラグ] $HOST で使用中にしました。"
    pull_report
    handoff_report
    ;;
  1)
    echo "[使用中フラグ] 相手が使用中です: $HOLDER"
    echo "この端末では作業を止めています。入力はブロックされます。"
    ;;
  2) offline_report ;;
  esac
}

cmd_prompt() {
  fresh "$HELD" && exit 0
  fresh "$OFFLINE" && exit 0
  claim
  case $? in
  0)
    rm -f "$OFFLINE"
    if [ "$CLAIMED" = 1 ]; then
      echo "[使用中フラグ] $HOST で使用中にしました。"
      pull_report
      handoff_report
    fi
    ;;
  1)
    {
      echo "相手が使用中です: $HOLDER"
      echo "相手の端末で Claude を終了してください。相手を使っていないなら、別のターミナルで次を実行します。"
      echo "  $ROOT/.claude/hooks/in-use.sh takeover"
      echo "（相手に残っている push 前の作業は取り込まれません）"
    } >&2
    exit 2
    ;;
  2) offline_report ;;
  esac
}

# フラグを消さずに備考を書き換える
leave_flag() {
  local sha=$1 note=$2 new
  new=$(new_flag "$note")
  g push -q --force-with-lease="$FLAG:$sha" origin "$new:$FLAG" 2>/dev/null
}

cmd_end() {
  local sha upstream
  [ -f "$HELD" ] || exit 0
  sha=$(cat "$HELD")
  rm -f "$HELD"
  if [ -n "$(g status --porcelain)" ]; then
    leave_flag "$sha" "未コミットの変更を残して終了"
    exit 0
  fi
  upstream=$(g rev-parse --abbrev-ref '@{u}' 2>/dev/null) || upstream=
  if [ -z "$upstream" ]; then
    leave_flag "$sha" "未pushのコミットを残して終了"
    exit 0
  fi
  if [ "$(g rev-list --count "$upstream..HEAD")" != 0 ] && ! g push -q 2>/dev/null; then
    leave_flag "$sha" "未pushのコミットを残して終了"
    exit 0
  fi
  # lease 付きなので、奪われた後のフラグは消さない
  g push -q --force-with-lease="$FLAG:$sha" origin ":$FLAG" 2>/dev/null
  exit 0
}

cmd_status() {
  local out
  out=$(g ls-remote origin "$FLAG" 2>/dev/null) || {
    echo "GitHub に接続できません"
    exit 1
  }
  if [ -z "$out" ]; then
    echo "空き"
  else
    flag_subject "${out%%[[:space:]]*}"
  fi
}

cmd_takeover() {
  local sha
  sha=$(new_flag "")
  if ! g push -q --force origin "$sha:$FLAG" 2>/dev/null; then
    echo "GitHub に接続できません"
    exit 1
  fi
  echo "$sha" >"$HELD"
  echo "$HOST で使用中にしました。"
  pull_report
}

case ${1:-} in
start) cmd_start ;;
prompt) cmd_prompt ;;
end) cmd_end ;;
status) cmd_status ;;
takeover) cmd_takeover ;;
*)
  echo "usage: $0 start|prompt|end|status|takeover" >&2
  exit 64
  ;;
esac
