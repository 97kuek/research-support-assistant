# 仕組み

Kei Agent のいまの作り。使い方は [`using.md`](using.md)、入れ方は [`../deploy/README.md`](../deploy/README.md)。

## 1. プロセス

すべて同じ Mac の launchd で常駐し、`127.0.0.1` だけで話す。plist はどれも `deploy/com.kei-agent.plist.template` から作り、起動スクリプト（本体 `run.sh`、担当 `run-agent.sh <名前>`、ゲートウェイ `run-notion-gateway.sh`）は、`uv sync` で依存をそろえてから仮想環境の Python を直に起動する（`uv run` のように uv を親として常駐させない）。取り込んだ main の反映は `deploy/update.sh`（`deploy/README.md`）。

| プロセス | コマンド | ポート | パッケージ | 役目 |
|---|---|---|---|---|
| 本体（オーケストレーター） | `kei-agent` | 8786 | `src/kei_agent/` | Slack の受け口、振り分け、柵、Notion、定期実行、自己改善。8786 は声からの問い合わせ口（12章） |
| 大学エージェント | `kei-agent-course` | 8787 | `src/kei_agent_course/` | Moodle・Box・授業ホーム・Toggl |
| 研究エージェント | `kei-agent-research` | 8788 | `src/kei_agent_research/` | 作業場での CLI 実行と pueue ジョブ |
| 仕事エージェント | `kei-agent-work` | 8789 | `src/kei_agent_work/` | Microsoft 365 を読む |
| 知識エージェント | `kei-agent-knowledge` | 8792 | `src/kei_agent_knowledge/` | 読みもの・論文の新着を集めて絞り、要約する。記事や論文の質問に答える（Notion・Slack は持たない） |
| 声のレイヤ | `kei-agent-voice` | 8790 | `src/kei_agent_voice/` | Realtime API、マイク、スピーカー |
| Notion ゲートウェイ | `kei-agent-notion-gateway` | 8791 | `src/kei_agent_notion_gateway/` | Notion を触る唯一の口。利用者ごとに届くホームを決める（10章） |

`src/kei_agent_a2a/` は A2A サーバーの共通部分（`server.py`）、返事の封筒（`envelope.py`）、受け付けの土台と共通の `ask`（`executor.py`）、provider を1回動かす処理（`run.py`）。

`config.toml` の `[a2a.agents]` から `research` の行を消すと、研究の実行は本体のプロセスの中で動く。

## 2. 置き場所

```text
~/research/<テーマ>/      研究テーマの作業場（1テーマ = 1チャンネル = 1ディレクトリ = Notion の「テーマ」1行）
├── CLAUDE.md             前提、分野、検索キーワード、ジョブにする基準
├── inputs/ outputs/      添付されたファイル／見せたい図や集計（新しいものをスレッドに添付）
├── logs/                 ジョブのログ（論文は研究ホームの先行研究 DB に残し、ここには置かない）
└── .kei-agent/           スレッドのログ、ジョブの状態（Kei Agent が書く）
~/course/                 大学エージェントの作業場（資料は Box に置いたまま）
~/kei-agent/              Kei Agent 自身のもの（agent_root）
├── overview/             #01_overview などの作業場。Daily・レトプラ・時間はここに置かず、Notion の共通ホームに残す
└── state/                毎晩の保守が書き出す SQLite の中身と Notion の ID
~/.local/state/kei-agent/ 状態（kei-agent.db、notion.json、asks/、worktrees/ など）
~/.config/kei-agent/      利用者のもの（KEI_AGENT_HOME で変えられる。docs/extensibility.md）
├── config.toml           設定の本体（リポジトリには config.example.toml だけ）
├── profile.md            話し方・所属・興味。会話する担当の指示書の最後に差し込む（例は profile.example.md）
├── prompts/              指示書の差し替え（prompts/ と同じ名前なら、そちらを使う）
├── modules/              自分のモジュール（<名前>/module.toml。組み込みのリポジトリ直下の modules/ と同じ形）
└── secrets/              秘密情報（[paths] secrets で変えられる。AI には読ませない）
```

## 3. Slack の受け口（本体）

- Socket Mode。指示できるのは `KEI_AGENT_ALLOWED_USER_ID` の1人だけ
- チャンネル名は先頭の番号（`00_` など）を外して `config.toml` の `[channels]` と照合する。`overview` / `improve` / `course` / `work` / `knowledge` に当たらないものは研究テーマ
- 返事は `chat.startStream` で流し、経過は `assistant.threads.setStatus` の1行、スレッドの状態は `agents.sessions.setStatus` で出す
- 違うスレッドは最大2件まで並行（`max_concurrent_runs`）、同じスレッドの中は順番
- 1スレッド = 1会話。研究・大学・仕事・知識・自己改善のどれも同じ扱いで、provider と指示書・skill の版が一致する session ID だけで再開し、合わないか失われたら Slack の履歴から新しい会話を始める（`Assistant._converse`）。Kei Agent の投稿（朝の読みもの、論文の新着、朝の一覧など）から始まったスレッドへの最初の返信にも、元の投稿を渡す（「2番を詳しく」に答えるため）
- どの担当に頼むときも、依頼の先頭に今日の日付と曜日を付ける
- 処理中の依頼は控えを残し、再起動で止まったものは起動時にやり直す（大学・仕事の質問も同じ）
- provider の利用上限に当たったら、明ける時刻をスレッドに書き、明けてから自動でやり直す（時刻が分からないときは30分後）
- 外から来た文字（予定の件名など）は Slack に出す前に `<` `>` `&` を逃がす（`slack_text.escape`）

## 4. 振り分けと A2A

| チャンネル | 行き先 |
|---|---|
| 研究テーマ | 研究エージェントの `ask`（テーマの作業場で provider を1回動かす） |
| 大学 | 大学エージェント。定型に当たれば決まったスキル、当たらなければ `ask` |
| 仕事 | 仕事エージェント。同上 |
| 知識（`#40_knowledge`） | 知識エージェントの `ask`。研究テーマのチャンネルでも、朝の論文の新着のスレッドは知識エージェント |
| overview | 軽いモデル（routing recipe）が名刺のスキル一覧から相手と仕事を選ぶ。選べなければそのドメインの `ask` |
| improve | 本体の自己改善（9章） |

- A2A v1.0。名刺は `/.well-known/agent-card.json`、JSON-RPC の `SendMessage` / `GetTask`、長い仕事は `SendStreamingMessage`（SSE）
- 仕事を頼むには共有の Bearer トークン `KEI_AGENT_A2A_TOKEN` が要る
- 名刺（Agent Card）の `version` は、そのプロセスが起動したときの commit。本体は起動したときに見比べて、古い版のまま動いている担当を起動し直す（`version.py`）
- エージェントを呼べるのは本体だけ。エージェント同士はつながず、声のレイヤも本体の口（`[a2a] orchestrator`）に頼む
- つなぐ前に断られたら3秒待って1回だけやり直す
- サーバー側は `a2a-sdk`、クライアントは aiohttp の薄い実装（`src/kei_agent/a2a.py`）

返事はすべて同じ封筒で返す（`src/kei_agent_a2a/envelope.py`）。

```json
{"ok": true, "text": "人が読む文", "data": {}, "limit_reset_at": null, "cost_usd": 0.02}
```

`text` は raw output で、Slack 向けに整えるのは本体だけ。`data` は締切の一覧などの中身で、見せ方は本体が決める。失敗は A2A のタスクを `failed`、封筒を `ok: false` にする。

### いまあるスキル

| エージェント | スキル |
|---|---|
| 大学 | `sync-assignments` / `list-due` / `list-calendar-assignments` / `list-classes` / `list-current-courses` / `time-report` / `ask` |
| 研究 | `ask` / `submit-job` / `list-jobs` / `cancel-job` / `forget-job` |
| 仕事 | `list-events` / `ask` |
| 知識 | `reading-digest` / `paper-digest` / `ask` |
| 声 | `notify` |
| 本体（声から） | `ask`（`actor`・`question`・研究なら `theme`） |

`ask` はどのエージェントでも同じ形。依頼は JSON（`prompt`、`session_id`、`channel`、`thread_ts`、`read_only`、`use_case`、`provider`。研究はさらに `channel_name`、`allowed_domains`）、返事の `data` は実行結果（`session_id`、`text`、`limit_reset_at` など）。用途が書いていなければ、その担当の軽い分類器で決める（`src/kei_agent_a2a/run.py`）。

エージェントが持たないもの: Slack への投稿、依頼者への約束（上限で待つ・やり直す・知らせる）、スレッドと session の対応、ジョブがどのスレッドのものか、ほかのドメインの秘密情報、Kei Agent 自身を直す作業。

### エージェントごとの中身

- **研究** … テーマの作業場で provider を1回動かして最終結果を返す。長い処理は pueue（グループ `kei-agent`）に入れ、本体が毎分状態を見て、終わったらその会話を再開する。ジョブは作業場の中のスクリプトだけで、`--expect` で宣言したファイルができたかを確かめる。Notion はゲートウェイ経由、W&B は `managing-wandb` skill
- **大学** … Moodle はカレンダーの ics（`MOODLE_ICS_URL`）を読み、「授業」に入れた履修科目の締切だけを「課題」に入れる。Notion への定型の書き込みは Python（ゲートウェイの `course` として）。自由な質問（`ask`）は `course_root` の作業場で動かし、Box は読む道具だけ、Notion はゲートウェイの `course` として授業ホームの中だけを触る。Toggl は読むだけで、プロジェクト＝科目で突き合わせる。提出の代行はしない
- **知識** … 朝の読みもの（`reading-digest`）と、テーマごとの論文の新着（`paper-digest`）を作る。材料（「収集」ページの興味と情報源、テーマの `CLAUDE.md` の検索キーワードと前提、先行研究 DB にある ID）は本体が本文の JSON で渡し、結果は本体が Slack と Notion に出す。RSS・arXiv・記事の本文はプログラムが読み（`feeds.py`。arXiv は混んでいると 406 などでしばらく断るので、5・15・45秒あけてやり直す）、一度候補にしたものは作業場の `seen.json` に90日覚える。読みものには、本体が覚えている最近の 👍（題名・出どころ・興味）も渡り、同じ出どころ・興味の候補に少し点を足し（`LIKE_BOOST`）、選ぶ回に好みの例として見せる。選ぶ・要約するのは Web を使えない回（`OFFLINE_USE_CASES`）、質問に答える `ask` だけが Web を読む
- **仕事** … 会社アカウントに付いた Microsoft 365 の連携（Outlook の予定・メール・人・空き時間、Teams、SharePoint）を、読む道具だけで使う。送信・投稿・予定の作成・変更・削除はしない。Codex では Outlook（メールと予定の App）だけを読む（Teams・SharePoint の App は、道具の名前を確かめてから表に足す）。予定の一覧（`list-events`）も共通の起動口で、読むだけの1回として動かす

## 5. provider とモデル

- 各 actor（`research` / `course` / `work` / `knowledge` / `router` / `self_fix`）ごとに、App Home で Claude か Codex を選ぶ。既定はなく、選ぶまで動かない（`config.toml` の `[agents.<actor>]` は `provider` だけ）
- model と effort は actor・用途（use case）・provider から決める。本体の用途は `src/kei_agent/model_policy.py`、モジュールの用途は `module.toml` の `[use_cases]`（知識は `modules/knowledge/module.toml`）。使ってよいモデルの一覧は `model_policy.py` にだけ置き、モジュールはその中からしか選べない
- 許可する model は Codex が `gpt-6-luna` / `gpt-6-sol` / `gpt-6-astra`、Claude が `claude-haiku-4-5` / `claude-sonnet-5` / `claude-opus-5` / `claude-fable-5` だけ
- 別 provider や上位 model への自動の切り替えはしない。provider 未選択、連携が使えない、上限到達のときは理由を出して止まる

| 用途 | Codex | Claude |
|---|---|---|
| 振り分け・自由文の分類 | luna / low | haiku-4-5 |
| 研究: 抽出 / 選別 | luna low / luna medium | haiku-4-5 |
| 研究: 比較 | sol / medium | sonnet-5 / medium |
| 研究: 実行（既定） | sol / high | sonnet-5 / high |
| 研究: 設計 | sol / xhigh | opus-5 / high |
| 大学: 説明 / 要件 / 比較 / 履修計画 | luna medium / luna high / sol medium / sol xhigh | sonnet-5 medium / high / high / opus-5 high |
| 仕事: 1つの出典 / 横断 / 判断 | luna medium / sol medium / sol high | sonnet-5 medium / high / opus-5 high |
| 知識: 選ぶ / 要約 / 質問 | luna low / luna medium / luna medium | haiku-4-5 / sonnet-5 medium / sonnet-5 medium |
| Daily / Retro & Planning | luna medium / sol high | sonnet-5 medium / opus-5 high |
| 自己改善: 案 / 実装 / 確認 | sol xhigh / high / medium | opus-5 high / sonnet-5 high / high |
| 明示指定だけ | `[[manual-astra]]` → astra / xhigh | `[[manual-fable]]` → fable-5 / high |

- 自由文の用途は選択中 provider の軽量分類器が決める。JSON が壊れている、自信が低い、失敗したときはその actor の通常の用途に落とす
- 研究の依頼の先頭に `[[research-extract]]` `[[research-screen]]` `[[research-compare]]` `[[research-execute]]` `[[research-design]]` を書くと分類より優先し、ラベルは本文から取り除く
- 大学・仕事の定型スキル（締切の取り込み、予定の一覧など）はモデルを呼ばない

### 実行のしかた

AI を起動するのは `src/kei_agent/runner.py` の `run_model` だけ（研究・大学・仕事・知識・振り分け・Daily/レトプラ・自己改善、Claude も Codex も）。1回の実行条件は `ExecutionRequest` と `ExecutionContract`（`execution_contract.py`）にまとめ、どこまで触れるかは制限の表（`agent_policy.py`）が決める。Claude の設定も Codex の設定も、この表から作る。

| 担当 | ファイル | コマンド | Web | Notion（ゲートウェイ） | アカウントの連携 |
|---|---|---|---|---|---|
| 研究 | 作業場を読み書き | ○ | ○ | 研究ホームを読み書き | なし |
| 大学 | 作業場を読むだけ | × | × | 授業ホームを読み書き | Box（読む道具だけ） |
| 仕事 | 作業場を読むだけ | × | × | なし | Microsoft 365（Outlook・Teams・SharePoint を読む道具だけ。Codex は Outlook だけ） |
| 知識 | 作業場を読むだけ | × | 質問に答えるときだけ | なし | なし |
| 振り分け・分類・Daily/レトプラ | 読むだけ | × | × | なし | なし |
| 自己改善 | 作業場を読み書き | ○ | ○ | なし | なし |

- 読むだけの実行（声からの問い合わせ、分類など）は、書く・動かす手段を外す。ゲートウェイは読む道具（`read` `search` `query`）だけ
- 外の文（記事・論文）を材料にする用途（`module.toml` の `offline = true`。知識の選ぶ・要約）は、Web も外す（`agent_policy.is_offline`）。モジュールの実行役の制限は、`module.toml` の `[actor]` から作る（`agent_policy.module_policy`）。外の文・個人の情報・外への出口の3つを1つの回に揃えない（記事に仕込まれた指示で、手元の情報を外へ送られないように）。知識の担当は Notion もテーマのファイルも持たず、テーマの前提は Web を使えない回にだけ渡す
- Claude は `claude -p`（stream-json）。表から `--settings` の許可と拒否を作り、`dontAsk` で表にないものは使わせない。アカウントの連携はその担当のプロファイル（`CLAUDE_CONFIG_DIR`）のユーザー設定から読み、ほかの担当はユーザー設定を持ち込まない（`--setting-sources ""`、`--strict-mcp-config`）
- Codex は `codex exec --json`（一時的な権限 profile `kei_agent_scoped`、`--ignore-user-config`）。アカウントの連携は、表の App の、表に書いた読む道具だけをモデルに見せる（ID は実行のたびに名前から引く）。Web 検索は表で許した担当だけ。Claude の担当が持たない道具（サブエージェント、画像の生成、プラグインの導入）は切り、ファイルを読まない担当からは画像を開く道具も外す。無人で動くので、渡した道具（ゲートウェイと App の読む道具）は呼ぶたびの承認を求めない
- Codex はファイルや skill をコマンドで読むので、コマンドを持たない担当（大学・仕事）でもシェルだけは残す。書き込み・通信・ホームの下（作業場と skill の置き場のほか）は、権限 profile が止める
- ゲートウェイの MCP は全担当で `kei-notion`。合言葉はその担当の名前で作ったもの（研究は `research`、大学は `course`）
- 渡す plugin は担当の1つだけ。skill は依頼に応じて使う手順、フック（`plugin/<agent>/hooks/policy.py`、PreToolUse）は明らかな安全違反だけを断る第二の防御で、Claude と Codex の両方に掛ける。Codex は道具の名前の書き方が違う（`kei-notion` は `kei_notion`、App の道具は `mcp__codex_apps__<App>__<道具>`）ので、フックは両方を読む。振り分け・自己改善には plugin を渡さない
- 作業場の前提は `CLAUDE.md` に置き、どの provider でも AI が読む（自動で読み込む仕組みには頼らない）

## 6. Slack に出す文（出力契約）

モデルの自由回答は、最終回答を `<<kei-agent-final>>` と `<<kei-agent-final-end>>` の間にだけ書く（`prompts/system.md`、`course.md`、`work.md`）。本体が `src/kei_agent/response_output.py` で次のように扱う。

| 種類 | 扱い |
|---|---|
| ふだんの会話 | marker がちょうど1組で、中が空でなく、後ろに何もないことだけを確かめ、中身を採用する。`outputs/fig.png` のような作業場の中の相対パスはそのまま出す。`~/…` や `/…`、`file://` の絶対パスはファイル名だけに置き換える（Web の URL は触らない） |
| Daily | marker の中が4つの太字の見出し（今日のタスク／夜間処理の結果／確認待ち・期日・止まっているテーマ・返事待ち／今日考えるとよい問い）だけで、パスや作業手順を含まないこと |
| Retro & Planning | 「今日の成果」「未完了タスク」と、最後の「夜間に実行したいタスクはありますか？」だけ |
| 定型の A2A の返事 | marker は要らない。作業の経過・例外・パスを含むものは出さない |

契約を満たさないときは、内部の詳細を含まない決まった失敗の文を出す（`safe_failure`）。

返事の最後の合図（`❓ 確認:` `🧵 区切り:` `🔒 接続:` `🛠 着手` `📦 取り込み`）は marker の内側の末尾に書き、本体がそれを見てボタンや状態を作る（[using.md](using.md#返事の最後に付く合図)）。

## 7. 柵

| 項目 | いまの形 |
|---|---|
| 使える道具 | 担当ごとに制限の表（5章）で決め、Claude の許可・拒否と Codex の profile・App・MCP をそこから作る |
| 書き込み | 作業場の中と `[sandbox] allow_write`（uv のキャッシュ）だけ。sandbox を有効にして確認なしで動かす |
| 読ませない場所 | `[sandbox] deny_read`（既定は `guard.py` の `DEFAULT_DENY_READ`: 秘密情報、`~/.ssh`、`~/.claude`、大学・仕事のプロファイルなど）。sandbox（Bash）でも、Claude の Read・Grep・Glob でも塞ぐ |
| 接続先 | 基本は `[sandbox] allowed_domains`。テーマごとの追加は Slack で1つずつ許可し、本体の SQLite に置く（作業場に置くとモデルが自分で足せてしまうため） |
| 環境変数 | 子プロセスに Slack・Notion などの鍵を渡さない。Notion ゲートウェイの親の合言葉も、どの子にも渡さない（`guard.strip_env`） |
| Notion | 鍵を持つのはゲートウェイだけ。ほかはすべて client ごとの合言葉でゲートウェイを通し、届くホームはゲートウェイが決める（下） |
| 柵そのもの | `src/kei_agent/guard.py`、`config.example.toml`、`deploy/` は Kei Agent 自身に直させない（`PROTECTED_PATHS`）。本物の設定はリポジトリの外（`~/.config/kei-agent/`）にあり、自己改善の作業場からは届かない |
| Slack から変えられないもの | 同時に動かす数、上限時間、書き込み先、読ませない場所、基本の接続先 |

**Notion ゲートウェイ**（`127.0.0.1:8791`、`src/kei_agent_notion_gateway/`）

- Notion に届くのはこのプロセスだけで、`NOTION_TOKEN` を持つのもここだけ（ほかの起動スクリプトは読んだあとで消す）
- 合言葉は client ごと。親の合言葉 `KEI_AGENT_NOTION_GATEWAY_TOKEN` で client 名を HMAC-SHA256 したもの（`kei_agent.notion.gateway_client_token`）だけを定数時間で比べて受け付け、親そのものは通さない。`/health` 以外はどれかの合言葉が要る
- 届くホームは `config.toml` の `[notion]` で決まる

| client | 使うところ | 届くホーム | 口 |
|---|---|---|---|
| `kei-agent` | 本体、手で動かす setup・移行の CLI | 共通・研究・授業 | `/mcp`、`/notion/v1` |
| `course` | 大学エージェントの決まった処理（締切・成績・setup）と LLM | 授業 | `/mcp`、`/notion/v1` |
| `research` | 研究の LLM | 研究 | `/mcp` だけ（Bash を持つので、何でも送れる口は渡さない） |

- 要求ごとに、触れる ID をすべて確かめてから送る。パスの ID、クエリの `database_id` / `data_source_id`、本文の親・移動先・テンプレート・リレーション・メンション・ページへのリンク・同期ブロック・位置の指定・ビューの置き場所・Markdown のページ参照。どれも親をたどってホームの子孫なら通す。外・見つからない・ワークスペース直下・循環・深すぎは 403（`restricted_resource`、`Kei Agent gateway: <client> can't reach <ID>`）。親子関係は60秒だけ覚え、ゲートウェイで移したものはすぐ忘れる
- `/notion/v1/…` は Notion の API をそのまま中継し、状態・本文・`Retry-After` をそのまま返す。扱うのはページ（Markdown・移動を含む）・ブロック・データベース・データソース・ビューと検索だけで、`/users` `/comments` `/file_uploads` など分からない形は断る。検索はホームの外の結果を落とす
- `/mcp` の道具はどの client でも同じ12個: `read`（ページはプロパティと Markdown の本文、`cursor` で続き）、`search`、`query`、`create_page`（親はページかデータソース）、`update_page`（`in_trash` も）、`append_blocks`、`replace_content`、`update_block`、`delete_block`、`create_database`、`update_data_source`、`move`。複製は Notion の API に無いので置かない。返事は短くし、長いものは切って続きの読み方を添える
- 研究の LLM には `--mcp-config` / `--strict-mcp-config`（Codex は `mcp_servers` の設定）で `/mcp` だけを渡し、環境には研究用の合言葉のヘッダー（`KEI_AGENT_NOTION_GATEWAY_AUTH`）だけを置く
- 記録は時刻・client・操作名・対象の ID・成否・失敗の種類だけ。本文や値は残さない
- 止まっているときは「つながらない」と返し、別のトークンや連携に乗り換えない

## 8. 定期実行

本体のスケジューラが毎分動く（`src/kei_agent/schedule.py`）。時刻は `config.toml` の `[schedule]` が既定で、App Home で変えたものは SQLite から毎分読み直す。

| 名前 | 既定 | 中身 |
|---|---|---|
| `literature` | 07:00 | テーマの `CLAUDE.md` の検索キーワードと前提を知識エージェントに渡し、arXiv の新着から関係のあるものを最大5本、研究ホームの先行研究 DB（「未読」）とテーマのチャンネルに出す。そのスレッドの続きは知識エージェントが答える |
| `reading` | 07:00 | 共通ホームの「収集」ページの興味と情報源、最近60日に 👍 した記事（最大20件）を知識エージェントに渡し、興味ごとに偏らない5件を要約つきで `#40_knowledge` に1記事ずつ出す（記事は `reading_posts` に控える）。新着がなければ出さない |
| `daily` | 08:00 | 今日の予定を時刻順に1通で出し、そのスレッドに Daily |
| `review` | 21:00 | Retro & Planning。Slack には成果と未完了だけ。直前に Moodle の課題を取り込み、スレッドに明日・明後日の締切を並べる |
| `maintenance` | 22:00 | 古いファイルの整理、Toggl だけで測った時間の取り込み（11章）、バックアップ（`[maintenance]`） |
| `night` | 00:00 | Notion の「今夜やる」Task を1件ずつ、一晩5件まで。テーマのない Task は「確認待ち」にする |

- Daily と Retro & Planning は actor `router` で動く。材料（`digest.py`）はファイルにせずプロンプトに入れ、3万字を超えたら後ろのノートから削る。前日の振り返りと今週の時間は共通ホームから読む
- 返事はそのまま共通ホームの「日別記録」に1日1行で保存し、手元には残さない。保存できなければ改善チャンネルに知らせる（Slack には出ている）。Retro のスレッドに貼った結論は同じ行のレトプラに足す
- Moodle の課題は Daily と Retro の前に授業ホームへ取り込み、新しい課題と締切の変わった課題を `#20_course` に知らせる
- 08:00 以降に1回、授業ホームの課題（これからの全部）を共通ホームの予定カレンダーに写す（AI は動かさない）
- 会議は、Daily のために読んだ7日ぶんを予定カレンダーに足す。AI が読んだ一覧は全部とは言い切れないので、見つからなくなった会議は消さずに「要確認」にする（0件のときは印も付けない）
- 毎分、締切24時間前の知らせを出し、24時間放置された失敗ジョブや確認待ちに一度だけ声をかける。締切まで3日を切っても「未着手」の課題（授業ホームの状態）は、1時間おきに見て一度だけ知らせる
- 締切の「0:00」ちょうどは、前の日の「24:00」として表示し、日付もその日に振り分ける（`deadline.py`。締切の時刻そのものは変えない）
- 朝の予定には、前回の Daily から失敗した定期処理（Notion に残せなかった Daily・Retro を含む）と、前の日に予定カレンダーへ課題を写せなかったことを1行で添える
- スリープで逃した処理は3時間以内（`night` は12時間以内）なら起きたときに動かす。上限中は始めず、明けてから動かす
- `kei-agent-schedule <night|literature|reading|daily|review|maintenance>` で1回だけ動かせる（`--record` を付けなければ今日の記録に残らない）

## 9. 自己改善（`#00_kei-agent`）

- 新しい要望は、`self_fix` で選んだ provider の routing recipe（read-only の一時ディレクトリ）が題（60字以内）と1〜4行の本文に要約し、`gh` で `origin` の公開 issue（ラベル `kei-agent-request`）にする。番号は SQLite の `improvements` に置く（`issues.py`）
- URL・`/Users/`・`~/`・メンション・`#チャンネル`・秘密情報・原文と20字以上同じところを含む要約は捨てる。issue にできなければ、provider 未選択はスレッドに、それ以外は改善チャンネルに知らせる。ファイルには逃がさない
- 取り込んだ版で起動できたら、そのコミットの短い sha を添えて issue を閉じる
- 案を考える回は、リポジトリを読むだけ（書けるのは一時ディレクトリ）
- `🛠 着手` で `<state_dir>/worktrees/` の git worktree を作って直す。書けるのは worktree の中だけ
- `🛠 着手` / `📦 取り込み` は、その回が依頼者の投稿で始まったときだけ効く
- 取り込む前に、変えたファイル、差分（添付）、テストの結果を出す。依存の追加は先頭に出す
- 取り込み: 柵のファイルに触れた差分は捨てる。手元に未コミットの変更があれば止める。main が進んでいれば合わせ直し、テスト・`ruff`・鍵・大きなファイルを確かめてから早送りで取り込んで push する
- 動いている作業がなくなったら、ほかのプロセス（ゲートウェイ・担当・声）を起動し直してから自分で終了し、launchd が新しい版で起動する。`update-pending` を残し、つながらないまま3回起動し直したら `deploy/run.sh` が `git revert` して前の版で起動し、Slack で知らせる
- 同時に直すのは1つだけ

## 10. Notion

Notion への道は、ゲートウェイの1つだけ。鍵は `NOTION_TOKEN`（コネクト「Kei Agent」。3つのホームを共有）で、持つのはゲートウェイだけ。使う側（client）ごとに届くホームを絞る（7章）。

本体・大学エージェントの決まった処理と LLM・setup の CLI・研究の LLM は、どれも client ごとの合言葉でゲートウェイを通す。アカウントに付いた Notion 連携（claude.ai・Codex App）は、どの担当にも使わせない。ホームのページ ID は `config.toml` の `[notion]`。

データベースとプロパティは名前で読むので、Notion の画面で名前や選択肢を変えない（変えるなら `src/kei_agent/notion.py`、`notion_store.py`、`notion_hub.py`、`src/kei_agent_course/notion_setup.py` も直す）。Kei Agent は自分が作ったページと決めたプロパティだけを書き、人が書いた本文は書き換えない。

### 共通ホーム（`Keitaro Ueki`）

本体だけが扱う（`src/kei_agent/notion_hub.py`）。研究ホームと授業ホームはこの下に移さず、リンクで並べる。

- **日別記録** … 1日1行。`日付`、`Daily`、`レトプラ`、`対象日`、それぞれの Slack。本文を手で足すときは「Kei Agent の本文ここまで」の下に書く
- **予定カレンダー** … 既存の「今月の予定」に `出典`（Outlook / 課題 / 手入力）、`出典 ID`、`元 URL`、`最終確認`、`同期状態` を足したもの。手入力の行と、同期に失敗したときの既存の行は消さない。見えなくなった行は `同期状態` を「要確認」にし、次に見えれば「確認済み」に戻す
- **時間記録** … 研究・大学・仕事の時間（11章）
- 研究 Task と授業の課題のリンクドビュー（「今週のタスク」）。締切が今週・来週のものと、期限切れで終わっていないものを、締切の近い順に出す（`task_view_spec`）
- **読みもの** … 朝の読みもので依頼者が 👍 した記事。`名前`、`URL`、`出どころ`、`興味`、`要約`、`日付`、`状態`（気になる / 読んだ）。👍 で本体が「気になる」で入れて 📝 を付け、👍 を外すとゴミ箱に入れる（`KnowledgeChannel.reading_reaction`）。無いときは入れずに、次からの参考にだけ使う
- **収集** … 知識エージェントが毎朝読む設定のページ。「興味」は `名前: キーワード、キーワード`、「情報源」は `zenn: トピック、…`・`qiita: タグ、…`・RSS の URL を1行ずつ。setup が無いときだけ作り、中身は利用者が直す（`parse_collect`）

### 研究ホーム

`kei-agent-notion-setup --apply` が作る（付けなければ、作るもの・足すものを並べるだけ。作った DB の ID は `notion.json`）。ホームには「自分の Task」「進行中のテーマ」「近いマイルストーン」のビューを置く。

| DB | 主なプロパティ |
|---|---|
| テーマ | 名前（チャンネル名と同じ）、状態（進行中 / 保留 / 完了）、目的、Slack、ディレクトリ、Task・ノート・マイルストーンへの relation |
| Task | タイトル、テーマ、状態（未着手 / 今夜やる / 実行中 / 確認待ち / 完了）、担当（自分 / Kei Agent）、優先度（P0〜P2）、期日、Slack、結果 |
| ノート | タイトル、種類（計画 / 考察 / 議論メモ。旧 Daily・振り返りの原本も残る）、テーマ、日付、書いた人、Slack、ファイル |
| マイルストーン | 名前、期日、テーマ、状態（予定 / 準備中 / 済み）、メモ。「中長期の方針」ページの下 |
| 先行研究 | 名前、テーマ（複数）、URL、ID（`arXiv:…` など。同じ論文は1行）、著者、年、会場、要点、この研究との関係、見つけた日、出どころ（毎朝の新着 / 依頼）、状態（未読 / 読んだ / 使う）。各テーマのページに、そのテーマの論文だけの表「先行研究」を置く |

- 🌙 は Task を作る入口。外すと「未着手」に戻す。夜間は「今夜やる」を読み、「状態」と「結果」を書く
- 実験の要約（W&B run の URL・主な指標）は、研究の LLM がゲートウェイ経由でその Task の「結果」かノート（種類「考察」）に書く。スレッドやジョブなど運用の状態は本体の SQLite が正で、Notion には置かない
- テーマの前提と検索キーワードの正は `CLAUDE.md`、論文は先行研究 DB（手元の `papers/` は使わない）。毎朝の新着は本体が書き、頼まれて調べた論文は研究の LLM が `researching-literature` の手順で書く

### 授業ホーム

`kei-agent-course-setup` が5つの DB をそろえる（DB の ID は `notion-course.json`）。

| DB | 中身 |
|---|---|
| 授業 | 科目名、科目コード、学期、曜日、時限、Moodle、状態、科目群・科目区分・必選区分 |
| 課題 | 課題名、締切、状態、授業への relation |
| 📊 成績履歴 | 科目群、科目区分、授業名、成績、GP、単位、取得年度 |
| 🎓 単位要件 | 大区分、要件名、所定・既得・算入・残り単位。`総合計` の行が卒業要件の全体 |
| 📈 GPA推移 | 春学期・秋学期・通算の GPA |

`授業` ← `課題` / `📊 成績履歴`、`📊 成績履歴` ← `🎓 単位要件` / `📈 GPA推移` の relation でつなぐ。成績と単位は大学の成績 HTML をローカルで読んで入れ（`kei-agent-course-academic-import`）、HTML 自体は Notion に置かない。名前が一致しない DB（`Untitled` など）は触らない。

## 11. 時間の記録

- 人の時間は Slack の `/toggl` コマンドか固定した時間記録カード（`src/kei_agent/time_cards.py`、`time_tracking.py`）で測る。1人1本で、別のチャンネルで始めると前の計測は止まる。`/toggl` の返事は ephemeral で、新しいカードは投稿しない（カードがあれば表示を更新する）
- 止めたらまず Toggl（`focus.toggl.com/api`、`toggl_sk_` の鍵）に送る。Toggl の環境変数が無ければ送らず（`not_configured`）Notion にだけ書く。そのあと研究・大学・仕事のどれも、共通ホームの「時間記録」に1件書く（記録 ID で1回だけ、出典 Slack）
- 送れなかったものは SQLite に保留して再送する。共通ホームが使えない間も保留にし、使えるようになってから送る。Toggl に届いたか分からないときだけ手で再送する
- Toggl のアプリで直接測った記録は、毎晩の保守で直近7日ぶんを「時間記録」に取り込む（プロジェクト名が `研究/` `大学/` `仕事/` で始まるものだけ。記録 ID `toggl:<id>`、出典 Toggl）。Slack から送った記録（開始と長さが1分以内で一致）は重ねない
- 週ごとの合計は「時間記録」のグラフのビュー（週ごとの時間）で見る。Kei Agent の稼働は SQLite の `runs` テーブルから数え、今週の合計を Daily の材料に入れる

## 12. 声のレイヤ

| 役割 | 担当 |
|---|---|
| 聞く・喋る・割り込み・ふだんの会話 | OpenAI Realtime API（`gpt-realtime-2.1-mini` 固定、声は `KEI_AGENT_REALTIME_VOICE`、既定 `cedar`） |
| 音の出し入れ | Mac の `ffmpeg`（`audio.py`）。割り込みは鳴らしているプロセスを止めて実現する |
| 予定・締切・様子 | 本体が押しておいた手元のデータ（`get_schedule` / `get_status`、朝に1週間ぶん） |
| 研究・授業・仕事の中身 | 本体の問い合わせ口（`[a2a] orchestrator`、`src/kei_agent/questions.py`）に頼む。本体が読むだけで担当に聞き、Slack と同じ出力の確認を通した答えを返す（`handoff.py`） |
| 作業 | `propose_request` で下書きし、読み上げて確認してから `send_request`。`<state_dir>/asks/` にファイルを置き、本体が拾ってスレッドを立てる（`src/kei_agent/ask.py`） |
| 顔 | `KEI_AGENT_STACKCHAN_URL` があれば Stack-chan に HTTP で表情だけ送る（`face.py`）。ロボットは未購入 |

- 本体 → 声は A2A の `notify` を投げっぱなしで送る（`src/kei_agent/voice.py`）。渡すのは出来事（`schedule` `due` `working` `done` `failed` `limited` `awaiting` `listen`）だけで、言い方と顔は声のレイヤが決める
- 「知らせる」だけのときは通知のたびに短い接続を作って読み上げ、マイクは開かない。「聞く」が入のときだけマイクを開けて会話する。どちらも既定は切で、設定は再起動後も戻る
- `OPENAI_API_KEY` が無ければつながらないが落ちない
- 会話は60分で切れるのでつなぎ直す。会話は残さず、Slack に残るのは依頼だけ
- 声の依頼は `asks/` を信じる（書けるのは自分だけという前提）。話し手の判定はしない

## 13. コードの地図（本体）

| ファイル | 役割 |
|---|---|
| `app.py` | 起動、Slack のイベントとボタンの登録 |
| `assistant.py` | 依頼の受け付けから返信までの本筋 |
| `themes.py` | チャンネル → テーマ → 作業場 |
| `router.py` / `agents.py` / `a2a.py` / `questions.py` | 振り分け、ほかのエージェントに頼む口、声からの問い合わせ口 |
| `model_policy.py` / `model_classifier.py` | recipe と用途の分類 |
| `agent_policy.py` / `execution_contract.py` / `runner.py` / `codex_apps.py` | 制限の表、実行条件、AI の起動口、Codex の App の ID |
| `response_output.py` | 出力契約 |
| `guard.py` | 柵（Kei Agent 自身に直させない） |
| `store.py` | SQLite（スレッド、session、ジョブ、定期処理、実行時間、接続先、時間記録） |
| `schedule.py` / `morning.py` / `digest.py` / `deadline.py` | 定期実行、朝の予定、材料集め、締切の読み方 |
| `course.py` / `work.py` / `knowledge.py` | 大学・仕事・知識のチャンネルと、そのエージェントに頼む口（知識は朝の読みもの・論文の新着の見せ方と、読みものの 👍 も） |
| `version.py` | 動いている版（担当の版ずれを見つける） |
| `notion.py` / `notion_store.py` / `notion_hub.py` | 研究ホームと共通ホーム |
| `home.py` / `settings.py` / `settings_actions.py` | App Home と設定 |
| `improve.py` / `self_fix.py` / `issues.py` | 自己改善、要望の GitHub issue |
| `time_cards.py` / `time_tracking.py` / `timelog.py` | 時間記録と Toggl |
