#!/usr/bin/env bash
# in-use.sh のテスト。GitHub の代わりにローカルの bare リポジトリを使い、
# 2つの clone を Mac と rz6 に見立てる。実行: .claude/hooks/test-in-use.sh
set -u

SCRIPT="$(cd "$(dirname "$0")" && pwd)/in-use.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export GIT_CONFIG_GLOBAL="$TMP/gitconfig"
git config --global user.name test
git config --global user.email test@example.com
git config --global init.defaultBranch master

pass=0
fail=0
ok() { pass=$((pass + 1)); }
ng() {
  fail=$((fail + 1))
  echo "NG: $1"
  [ -n "${2:-}" ] && echo "    $2"
}
expect_contains() { case "$3" in *"$2"*) ok ;; *) ng "$1" "「$2」が無い: $3" ;; esac; }
expect_absent() { case "$3" in *"$2"*) ng "$1" "「$2」が出ている: $3" ;; *) ok ;; esac; }
expect_eq() { if [ "$2" = "$3" ]; then ok; else ng "$1" "期待=$2 実際=$3"; fi; }

# 新しい origin と2台分の clone を作る
setup() {
  rm -rf "$TMP/w"
  mkdir -p "$TMP/w"
  git init -q --bare "$TMP/w/origin.git"
  git clone -q "$TMP/w/origin.git" "$TMP/w/mac" 2>/dev/null
  (cd "$TMP/w/mac" && echo a >a && git add a && git commit -qm init && git push -q origin master)
  git clone -q "$TMP/w/origin.git" "$TMP/w/rz6" 2>/dev/null
}

# run <machine> <command>: OUT に stdout+stderr、RC に終了コードが入る
run() {
  OUT=$(IN_USE_HOST="$1" CLAUDE_PROJECT_DIR="$TMP/w/$1" "$SCRIPT" "$2" 2>&1 </dev/null)
  RC=$?
}

flag_subject() {
  git -C "$TMP/w/origin.git" log -1 --format=%s refs/heads/in-use 2>/dev/null || echo none
}

# ---- 取得 ----
setup
run mac start
expect_eq "空いていれば start は成功する" 0 "$RC"
expect_contains "空いていれば flag に自分の名前が入る" mac "$(flag_subject)"

setup
(cd "$TMP/w/mac" && echo b >b && git add b && git commit -qm "new-from-mac" && git push -q origin master)
run rz6 start
expect_contains "start は GitHub の新しいコミットを pull する" new-from-mac "$(git -C "$TMP/w/rz6" log -1 --format=%s)"
expect_contains "start は取り込んだコミットを Claude に伝える" new-from-mac "$OUT"

# ---- 相手が使用中 ----
setup
run mac start
before=$(flag_subject)
run rz6 start
expect_eq "相手が使用中でも start 自体は止まらない" 0 "$RC"
expect_contains "start は使用中の相手を伝える" mac "$OUT"
expect_eq "相手の flag は書き換えない" "$before" "$(flag_subject)"

run rz6 prompt
expect_eq "相手が使用中なら prompt はブロックする" 2 "$RC"
expect_contains "ブロック時に相手の名前を出す" mac "$OUT"
expect_contains "ブロック時に強制解除の方法を出す" takeover "$OUT"

run mac prompt
expect_eq "自分の flag なら prompt は通る" 0 "$RC"

# ---- 同時に取りに行った場合（確認と push の間に相手が先に取る）----
setup
mkdir -p "$TMP/shim"
real_git=$(command -v git)
cat >"$TMP/shim/git" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  if [ "\$a" = push ] && [ ! -e "$TMP/shim/done" ]; then
    touch "$TMP/shim/done"
    IN_USE_HOST=mac CLAUDE_PROJECT_DIR="$TMP/w/mac" PATH="$(dirname "$real_git"):\$PATH" "$SCRIPT" start >/dev/null 2>&1
    break
  fi
done
exec "$real_git" "\$@"
EOF
chmod +x "$TMP/shim/git"
rm -f "$TMP/shim/done"
OUT=$(PATH="$TMP/shim:$PATH" IN_USE_HOST=rz6 CLAUDE_PROJECT_DIR="$TMP/w/rz6" "$SCRIPT" prompt 2>&1 </dev/null)
RC=$?
expect_eq "同時に取りに行って負けたらブロックする" 2 "$RC"
expect_contains "負けたとき flag は勝った側のまま" mac "$(flag_subject)"

# ---- 空いたら取れる ----
setup
run mac start
run rz6 prompt
run mac end
(cd "$TMP/w/origin.git" && git log -1 --format=%s master >/dev/null)
run rz6 prompt
expect_eq "相手が終われば prompt は通る" 0 "$RC"
expect_contains "相手が終われば flag を取れる" rz6 "$(flag_subject)"

setup
run mac start
run rz6 prompt
(cd "$TMP/w/mac" && echo c >c && git add c && git commit -qm "made-while-blocked")
run mac end
run rz6 prompt
expect_contains "ブロック後に取れたときも pull する" made-while-blocked "$(git -C "$TMP/w/rz6" log -1 --format=%s)"
expect_contains "ブロック後に取れたときは取り込みを伝える" made-while-blocked "$OUT"

# ---- 終了 ----
setup
run mac start
run mac end
expect_eq "変更が無ければ end で flag を消す" none "$(flag_subject)"

setup
run mac start
(cd "$TMP/w/mac" && echo d >d && git add d && git commit -qm "unpushed-work")
run mac end
expect_eq "コミット済みなら end で push する" unpushed-work "$(git -C "$TMP/w/origin.git" log -1 --format=%s master)"
expect_eq "push できたら flag を消す" none "$(flag_subject)"

setup
run mac start
echo dirty >"$TMP/w/mac/dirty"
run mac end
expect_contains "未コミットがあれば flag を残す" mac "$(flag_subject)"
expect_contains "未コミットで残したことを flag に書く" 未コミット "$(flag_subject)"
expect_eq "勝手にコミットしない" init "$(git -C "$TMP/w/mac" log -1 --format=%s)"
run rz6 prompt
expect_contains "相手には未コミットが残っていると伝わる" 未コミット "$OUT"

setup
run mac start
(cd "$TMP/w/mac" && echo e >e && git add e && git commit -qm "local-only")
(cd "$TMP/w/rz6" && echo f >f && git add f && git commit -qm "other" && git push -q origin master)
run mac end
expect_contains "push できなければ flag を残す" mac "$(flag_subject)"
expect_contains "push できなかったことを flag に書く" 未push "$(flag_subject)"

setup
run mac start
run rz6 end
expect_contains "持っていない側の end は flag を消さない" mac "$(flag_subject)"

setup
run mac start
(cd "$TMP/w/rz6" && echo g >g && git add g && git commit -qm "blocked-side-commit")
run rz6 end
expect_eq "持っていない側の end は push しない" init "$(git -C "$TMP/w/origin.git" log -1 --format=%s master)"

# ---- 再開・異常終了 ----
setup
run mac start
rm -f "$(git -C "$TMP/w/mac" rev-parse --absolute-git-dir)/in-use-held"
run mac start
expect_contains "異常終了後は同じマシンで引き継げる" mac "$(flag_subject)"
run mac end
expect_eq "引き継いだ flag も end で消せる" none "$(flag_subject)"

setup
run mac start
run mac end
run mac prompt
expect_contains "同じマシンの別セッションは次の入力で取り直す" mac "$(flag_subject)"

# ---- 強制解除 ----
setup
run mac start
run rz6 takeover
expect_contains "takeover で flag を奪える" rz6 "$(flag_subject)"
run rz6 prompt
expect_eq "takeover 後は prompt が通る" 0 "$RC"

run mac end
expect_contains "奪われた側の end は相手の flag を消さない" rz6 "$(flag_subject)"

setup
run mac start
run rz6 takeover
run mac prompt
expect_eq "印が新しいうちは確認を省くので通る" 0 "$RC"
touch -t 200001010000 "$(git -C "$TMP/w/mac" rev-parse --absolute-git-dir)/in-use-held"
run mac prompt
expect_eq "印が古くなったら確認し、奪われていればブロックする" 2 "$RC"

# ---- オフライン ----
setup
git -C "$TMP/w/rz6" remote set-url origin "$TMP/w/no-such-repo.git"
run rz6 start
expect_eq "オフラインでも start は止めない" 0 "$RC"
expect_contains "オフラインだと伝える" GitHub "$OUT"
run rz6 prompt
expect_eq "オフラインでも prompt は止めない" 0 "$RC"
expect_absent "オフライン通知は毎回は出さない" GitHub "$OUT"

# ---- pull 失敗 ----
setup
(cd "$TMP/w/mac" && echo x >x && git add x && git commit -qm x && git push -q origin master)
echo conflict >"$TMP/w/rz6/x"
run rz6 start
expect_contains "pull できなければ伝える" "pull できません" "$OUT"
expect_contains "pull できなくても flag は取る" rz6 "$(flag_subject)"

# ---- 申し送り ----
setup
(cd "$TMP/w/mac" && printf '# 申し送り\n- [ ] 確認してほしいこと\n- [x] 済んだこと\n' >handoff.md && git add handoff.md && git commit -qm h && git push -q origin master)
run rz6 start
expect_contains "start は未対応の申し送りを出す" 確認してほしいこと "$OUT"
expect_absent "start は済んだ申し送りを出さない" 済んだこと "$OUT"

# ---- 状態表示 ----
setup
run mac status
expect_contains "空いていれば status は空きと出す" 空き "$OUT"
run mac start
run rz6 status
expect_contains "status は使用中の相手を出す" mac "$OUT"

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
