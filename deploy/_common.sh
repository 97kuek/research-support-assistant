# launchd から起動する run*.sh が読む共通の処理（直接は動かさない）。読む前に REPO を決めておく。

# launchd は ~/.zshrc を読まないので、使うコマンド（uv・claude・codex・pueue など）の場所をここで決める
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# 秘密情報は、どのプロセスも読む共通のもの（kei-agent.zsh）と、そのプロセスだけのもの（kei-agent-<名前>.zsh、任意）に
# 分けてある（docs/architecture.md の「振り分けと A2A」）。こうすると、ほかのプロセスの鍵が載らない。
# 置き場所は config.toml の [paths] secrets（既定は ~/.config/kei-agent/secrets）。本体を起動する前に知りたいので、
# 仮想環境の Python に設定を読ませる。まだ仮想環境が無いときは既定の場所
secrets_dir() {
  local found=""
  if [[ -x "${REPO:-}/.venv/bin/python" ]]; then
    found=$("$REPO/.venv/bin/python" -m kei_agent.paths secrets 2>/dev/null) || found=""
  fi
  print -r -- "${found:-${KEI_AGENT_HOME:-$HOME/.config/kei-agent}/secrets}"
}
# require_secrets が SECRETS_DIR と SECRETS（共通のファイル）を決める。source するのは各 run*.sh の一番外側で
# （関数の中で source すると、変数の扱いが変わることがある）
require_secrets() {
  SECRETS_DIR=$(secrets_dir)
  SECRETS="$SECRETS_DIR/kei-agent.zsh"
  if [[ ! -r "$SECRETS" ]]; then
    echo "秘密情報のファイルがありません: $SECRETS（deploy/README.md を参照）" >&2
    exit 1
  fi
}

# launchd の出力は回らないので、起動のたびに大きすぎるものを捨てる
# （設定を間違えると KeepAlive で 30 秒ごとに再起動し、同じエラーが積もり続ける）
LAUNCHD_LOG_LIMIT=5242880
trim_launchd_log() {
  local log="$HOME/Library/Logs/kei-agent/$1"
  if [[ -f "$log" ]] && (( $(stat -f%z "$log") > LAUNCHD_LOG_LIMIT )); then
    : > "$log"
  fi
}

# Notion の鍵を持つのは Notion ゲートウェイ（run-notion-gateway.sh）だけ。ほかのプロセスは、
# 共通の秘密情報を読んだあとで消す（Notion には client ごとの合言葉でゲートウェイを通して届く）
drop_notion_secrets() {
  unset NOTION_TOKEN NOTION_COURSE_TOKEN
}

# 担当プロセスの名前。本体に組み込みの担当と、担当プロセスを持つモジュール（module.toml に [process] がある。
# 組み込みの modules/ と、利用者のフォルダの modules/ の両方）。起動の前に要るので、Python を使わずに探す
CORE_AGENTS=(research course work voice)
module_processes() {
  local file
  for file in "${REPO:-}"/modules/*/module.toml(N) "${KEI_AGENT_HOME:-$HOME/.config/kei-agent}"/modules/*/module.toml(N); do
    if grep -q '^\[process\]' "$file"; then
      print -r -- "${file:h:t}"
    fi
  done
}
agent_names() {
  print -r -- $CORE_AGENTS $(module_processes)
}

# 仮想環境の Python で直に起動する。`uv run` だと uv が親として残り、プロセスごとに 20MB ほど余分に使う。
# 起動の前に lock のとおりに依存をそろえる（--inexact: ほかのグループのものは消さない。uv run と同じ）
launch() {
  local group="$1" entry="$2"
  local -a groups=()
  if [[ -n "$group" ]]; then
    groups=(--group "$group")
  fi
  cd "$REPO"
  uv sync --frozen --inexact --quiet "${groups[@]}"
  export VIRTUAL_ENV="$REPO/.venv"
  export PATH="$VIRTUAL_ENV/bin:$PATH"
  exec "$VIRTUAL_ENV/bin/$entry"
}
