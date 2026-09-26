#!/bin/zsh
# 取り込んだ main を、動いているプロセスに反映する（1コマンドのデプロイ）。
# 使い方: deploy/update.sh          この checkout の main を、そのまま反映する
#         deploy/update.sh --pull   先に origin/main を取り込む（fast-forward できるときだけ）
# やること: 依存をそろえる → plist が変わったものだけ登録し直す → 全部を起動し直す → 新しい版で動いているか確かめる。
# push はしない（push は別に、頼まれたときだけ）
set -eu

REPO="${0:A:h:h}"
source "$REPO/deploy/_common.sh"
cd "$REPO"

# 本番の checkout なので、main 以外や、書きかけのあるときは何もしない
branch=$(git rev-parse --abbrev-ref HEAD)
if [[ "$branch" != main ]]; then
  echo "main ではありません（$branch）。main で実行してください" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "書きかけの変更があります。コミットするか戻してから実行してください" >&2
  exit 1
fi
if [[ "${1:-}" == "--pull" ]]; then
  git pull --ff-only --quiet origin main
fi
commit=$(git rev-parse --short=12 HEAD)
echo "反映する版: $commit $(git log -1 --format=%s)"

# 依存を lock のとおりにそろえる（どのプロセスも起動のときに自分の分をそろえるが、先に全部を確かめておく）
uv sync --frozen --inexact --all-groups --quiet

# plist が変わったものだけ登録し直す（kickstart では前の plist のまま動くため）
for name in assistant $(agent_names) notion-gateway; do
  args=()
  if [[ "$name" != assistant ]]; then
    args=("$name")
  fi
  plist="$HOME/Library/LaunchAgents/com.kei-agent.$name.plist"
  if [[ ! -f "$plist" ]]; then
    echo "登録されていません: $name（使うなら deploy/install.sh ${args[*]}）"
    continue
  fi
  if [[ "$(zsh "$REPO/deploy/install.sh" "${args[@]}" print)" != "$(<"$plist")" ]]; then
    echo "plist が変わったので登録し直します: $name"
    zsh "$REPO/deploy/install.sh" "${args[@]}" > /dev/null
  fi
done

# 全部を起動し直して（ゲートウェイ → 担当 → 本体）、新しい版で動いているかを確かめる
"$REPO/deploy/restart-all.sh"
"$REPO/.venv/bin/python" -m kei_agent.deploy_check "$commit"
