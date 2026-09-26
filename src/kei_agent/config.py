"""設定の読み込み。

秘密情報（Slackのトークンなど）は環境変数から、それ以外は config.toml から読む。
config.toml は利用者のフォルダ（既定は ~/.config/kei-agent/。環境変数 KEI_AGENT_HOME で変えられる）に置き、
リポジトリには例（config.example.toml）だけを置く。プロフィールと指示書の差し替えも、同じフォルダから読む
（docs/extensibility.md）。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from kei_agent import modules
from kei_agent.guard import DEFAULT_DENY_READ

REPO_ROOT = Path(__file__).resolve().parents[2]
# 利用者のもの（設定・プロフィール・指示書の差し替え・秘密情報）を置く場所
DEFAULT_HOME = "~/.config/kei-agent"
CONFIG_FILE = "config.toml"
PROFILE_FILE = "profile.md"
EXAMPLE_CONFIG = REPO_ROOT / "config.example.toml"

# 決まった時刻の処理の時刻。空文字は「その処理を行わない」（settings.schedule_time と同じ形）
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# skill を持つエージェント（`plugin/<agent>/`）。声やルーターには skill を渡さない
AGENT_PLUGINS = frozenset({"research", "course", "work"})
# 本体が持つ実行役。router は Daily/Retro の横断的な計画も担う。モジュールの実行役は module.toml の [actor] から足す
CORE_ACTORS = AGENT_PLUGINS | frozenset({"router", "self_fix"})


def model_actors() -> frozenset[str]:
    """provider を選ぶ実行役。コアのものと、知っているモジュール（組み込みと利用者のもの）のもの。"""
    return CORE_ACTORS | frozenset(name for name, spec in modules.known().items() if spec.actor)


@dataclass(frozen=True)
class AgentProfile:
    """agent が使う provider。どこまで触れるか（道具・連携）は制限の表（agent_policy.py）が決める。

    skill は手順、profile は実行器を決める。skill の中にモデル名を埋め込まないため、
    Claude と Codex を同じ agent から切り替えられる。
    """

    # provider は App Home で明示選択する。空文字は「まだ選んでいない」。
    provider: str = ""


def _default_agent_profiles() -> dict[str, AgentProfile]:
    return {agent: AgentProfile() for agent in model_actors()}


def _default_modules() -> tuple[str, ...]:
    """設定に modules を書かなければ、組み込みのモジュールを全部使う。"""
    return tuple(modules.builtin())


def _default_module_channels() -> dict[str, tuple[str, ...]]:
    return {kind: names for spec in modules.builtin().values() for kind, names in spec.channels.items()}


def _default_module_times() -> dict[str, str]:
    return {s.name: s.default for spec in modules.builtin().values() for s in spec.schedules}


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def path_without_venv(path: str, repo_root: Path) -> str:
    """PATH から Kei Agent 自身の `.venv/bin` を外す。

    `uv run` が PATH の先頭に足すので、そのまま渡すと、テーマの中で `python3` と打ったときに
    研究用ではなく Kei Agent の Python が当たってしまう。
    """
    venv_bin = str(repo_root / ".venv" / "bin")
    return os.pathsep.join(p for p in path.split(os.pathsep) if p and p != venv_bin)




@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool = True
    # "HH:MM"（ローカル時刻）。空文字にするとその処理を行わない
    daily: str = "08:00"
    review: str = "21:00"
    night: str = "00:00"
    # Mac のスリープなどで逃した処理を、何時間後まで実行するか
    catch_up_hours: float = 3
    night_max_tasks: int = 5
    stall_days: int = 3
    unanswered_hours: int = 24
    # モジュールの定期処理の時刻（名前 → HH:MM。書かなければ module.toml の既定）
    module_times: dict[str, str] = field(default_factory=_default_module_times)


@dataclass(frozen=True)
class MaintenanceConfig:
    enabled: bool = True
    # 毎晩の保守（古いファイルの整理とバックアップ）を行う時刻。振り返りのあとにする
    time: str = "22:00"
    # ~/research を Git でコミットして push する（deploy/backup-init.sh で準備する）
    backup: bool = True
    # テーマのディレクトリで動かした Claude のセッションの記録を残す日数
    session_retention_days: int = 90
    # スレッドのログ（.kei-agent/threads/*.md）を残す日数
    thread_log_retention_days: int = 180


def notion_id(value: str) -> str:
    """Notion の ID を比べられる形にする（ハイフンを外して小文字）。"""
    return str(value or "").replace("-", "").strip().lower()


@dataclass(frozen=True)
class NotionConfig:
    """Notion のホームのページ ID（`[notion]`）。ゲートウェイはこの下だけを通す。"""
    # 共通ホーム（本体だけ）
    hub_home: str = ""
    # 研究ホーム（kei-agent-notion-setup の notion.json と同じ）
    research_home: str = ""
    # 授業ホーム（kei-agent-course-setup の notion-course.json と同じ）
    course_home: str = ""


def _notion(data: dict) -> NotionConfig:
    _check_keys(data, {f.name for f in fields(NotionConfig)}, "[notion]")
    return NotionConfig(**{key: notion_id(str(value)) for key, value in data.items()})


@dataclass(frozen=True)
class A2AConfig:
    """ほかのエージェントの住所（docs/architecture.md の「振り分けと A2A」）。

    `[a2a.agents]` に「名前 = 住所」を並べる。名前は launchd・ログ・秘密情報ファイル・ポートの
    呼び名と同じにする。書かなければ、そのエージェントは使わない（研究を書かなければ本体の中で動かす）。
    """
    agents: dict[str, str] = field(default_factory=dict)
    # 相手を待つ時間（秒）。AI を動かす仕事には、AI の上限時間を足して待つ
    timeout_seconds: float = 300
    # 本体の A2A の口（声のレイヤからの問い合わせを受ける）。担当を呼べるのは本体だけ
    orchestrator: str = ""

    def url(self, name: str) -> str:
        return self.agents.get(name, "")


@dataclass(frozen=True)
class Config:
    # 研究テーマだけを置く場所（研究エージェントの領分）
    research_root: Path
    # Kei Agent 自身のもの（研究全体の作業場と、状態の書き出し）。研究テーマと混ぜない
    agent_root: Path
    # 授業の作業場（大学エージェントの claude が動くところ。資料は置かない）
    course_root: Path
    state_dir: Path
    repo_root: Path
    allowed_user_id: str
    overview_channels: tuple[str, ...] = ("overview", "research-overview")
    # `#00_kei-agent`。先頭の番号は外して照合する（themes.theme_name）
    improve_channels: tuple[str, ...] = ("kei-agent",)
    # 大学エージェントに取り次ぐチャンネル（claude -p は動かさない）
    course_channels: tuple[str, ...] = ("course",)
    # 仕事エージェントに取り次ぐチャンネル
    work_channels: tuple[str, ...] = ("work",)
    # 使うモジュール（設定の modules）と、そのチャンネル（種類 → 番号を外した名前）
    modules: tuple[str, ...] = field(default_factory=_default_modules)
    module_channels: dict[str, tuple[str, ...]] = field(default_factory=_default_module_channels)
    max_concurrent_runs: int = 2
    run_timeout_minutes: int = 30
    job_poll_seconds: int = 60
    job_parallel: int = 1
    agent_profiles: dict[str, AgentProfile] = field(default_factory=_default_agent_profiles)
    # 依頼者の依頼がこの回数たまったスレッドでは、新しいスレッドに区切るボタンを出す。0 なら出さない
    handoff_after_turns: int = 8
    allowed_domains: tuple[str, ...] = ()
    allow_write: tuple[Path, ...] = ()
    deny_read: tuple[Path, ...] = ()
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    pueue_bin: str = "pueue"
    schedule: ScheduleConfig = field(default_factory=lambda: ScheduleConfig())
    maintenance: MaintenanceConfig = field(default_factory=lambda: MaintenanceConfig())
    a2a: A2AConfig = field(default_factory=lambda: A2AConfig())
    notion: NotionConfig = field(default_factory=lambda: NotionConfig())
    # エージェント同士の合言葉（環境変数 KEI_AGENT_A2A_TOKEN）
    a2a_token: str = ""
    # 利用者のフォルダ（~/.config/kei-agent/）。None なら、プロフィールも指示書の差し替えも使わない（テスト）
    user_dir: Path | None = None
    # 秘密情報の置き場所（[paths] secrets。既定は利用者のフォルダの secrets/）。いつも AI に読ませない
    secrets_dir: Path | None = None

    def prompt_file(self, name: str) -> Path:
        """指示書。利用者のフォルダの prompts/ に同じ名前のファイルがあれば、そちらを使う（丸ごと差し替え）。"""
        if self.user_dir is not None and (own := self.user_dir / "prompts" / name).is_file():
            return own
        return self.repo_root / "prompts" / name

    @property
    def profile_text(self) -> str:
        """利用者のプロフィール（話し方、所属、興味など）。会話する担当の指示書に差し込む。無ければ空。

        先頭の題（`# プロフィール`）と `<!-- -->` のコメント（書き方の説明）は外す。
        """
        path = self.user_dir / PROFILE_FILE if self.user_dir is not None else None
        if path is None or not path.is_file():
            return ""
        text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        return re.sub(r"\A\s*# [^\n]*\n", "", text).strip()

    @property
    def db_path(self) -> Path:
        return self.state_dir / "kei-agent.db"

    @property
    def hub_state_path(self) -> Path:
        """共通ホームの ID 控え。研究ホームの notion.json とは独立させる。"""
        return self.state_dir / "hub.json"

    def agent_plugin_dir(self, agent: str) -> Path:
        """そのエージェントの skill の置き場（`plugin/<agent>/`）。

        `--plugin-dir` に `plugin/` そのものを渡すと、Claude Code は中の plugin を全部読む。
        担当外の skill を同じ claude に見せないため、必ずエージェント1つぶんを名指しする。
        """
        if agent not in AGENT_PLUGINS:
            raise ValueError(f"未知のagent: {agent}（使えるのは {', '.join(sorted(AGENT_PLUGINS))}）")
        return self.repo_root / "plugin" / agent

    @property
    def notion_gateway_url(self) -> str:
        """Notion ゲートウェイの MCP の口（src/kei_agent_notion_gateway）。"""
        return "http://127.0.0.1:8791/mcp"

    @property
    def notion_gateway_api(self) -> str:
        """同じゲートウェイの、決まった処理用の Notion API の口（`/notion/v1`）。"""
        return gateway_endpoint(self.notion_gateway_url, "notion/v1")

    @property
    def overview_dir(self) -> Path:
        """研究全体・中長期の方針のチャンネルが書く場所。

        Daily・振り返り・声の記録は、研究だけでなく授業と仕事の内容も含むので、
        研究テーマの隣ではなく Kei Agent 側に置く（docs/architecture.md）。
        """
        return self.agent_root / "overview"


def gateway_endpoint(mcp_url: str, path: str) -> str:
    """Notion gateway の MCP の URL（…/mcp）から、同じサーバーの別の口を作る。"""
    return f"{mcp_url.rstrip('/').removesuffix('/mcp')}/{path.lstrip('/')}"


class ConfigError(ValueError):
    pass


# 書き間違いが黙って無視されないよう、使えるキーをすべて書き出しておく
TOP_LEVEL_KEYS = {
    "research_root", "agent_root", "course_root", "state_dir", "max_concurrent_runs", "run_timeout_minutes",
    "job_poll_seconds", "job_parallel", "agents", "handoff_after_turns", "channels", "sandbox",
    "schedule", "maintenance", "a2a", "notion", "paths", "modules",
}
PATHS_KEYS = {"secrets"}
AGENT_PROFILE_KEYS = {"provider"}
# 本体が持つチャンネルの種類（モジュールの種類は module.toml の [channels] から足す）
CHANNELS_KEYS = {"overview", "improve", "course", "work"}
# [schedule] のうち、時刻（HH:MM）を書くキー（モジュールの定期処理は module.toml の [schedules] から足す）
SCHEDULE_TIME_KEYS = ("daily", "review", "night")
SANDBOX_KEYS = {"allowed_domains", "allow_write", "deny_read"}


def _check_keys(data: dict, known: set[str], where: str) -> None:
    """知らないキーがあれば、どれが違うかを示して止める。"""
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"config.toml の {where} に知らないキーがあります: {', '.join(unknown)}"
            f"（使えるキー: {', '.join(sorted(known))}）"
        )


def _section(cls, data: dict, name: str):
    """config.toml の [name] を cls にする。"""
    _check_keys(data, {f.name for f in fields(cls)}, f"[{name}]")
    return cls(**data)


def _schedule(data: dict, module_schedules: list[modules.ScheduleSpec]) -> ScheduleConfig:
    """[schedule] を読む。モジュールの定期処理の時刻は、書かなければ module.toml の既定。"""
    names = {s.name for s in module_schedules}
    core = {k: v for k, v in data.items() if k not in names}
    _check_keys(core, {f.name for f in fields(ScheduleConfig)} - {"module_times"}, "[schedule]")
    return ScheduleConfig(**core, module_times={s.name: str(data.get(s.name, s.default)) for s in module_schedules})


def _check_times(schedule: dict, maintenance: dict, extra: tuple[str, ...] = ()) -> None:
    """決まった時刻の書き間違いを、黙って「行わない」にしない。

    `daily = "8:00"` のように書くと、時刻として読めないので処理が動かなくなる。空文字だけが
    「行わない」の意味なので、それ以外の読めない形は、起動のときに断る。extra はモジュールの定期処理の名前。
    """
    times = [(f"[schedule] {name}", schedule[name]) for name in (*SCHEDULE_TIME_KEYS, *extra) if name in schedule]
    if "time" in maintenance:
        times.append(("[maintenance] time", maintenance["time"]))
    for where, value in times:
        if value != "" and not HHMM.match(str(value)):
            raise ConfigError(
                f"config.toml の {where} は HH:MM か、空文字（行わない）にしてください: {value!r}")


def _a2a(data: dict, enabled: list[modules.ModuleSpec]) -> A2AConfig:
    """[a2a] と、その下の [a2a.agents]（名前 = 住所）を読む。担当プロセスを持つモジュールは、書かなければ
    module.toml の番地（127.0.0.1）を使う。"""
    _check_keys(data, {"agents", "timeout_seconds", "orchestrator"}, "[a2a]")
    agents = data.get("agents", {})
    if not isinstance(agents, dict) or any(not isinstance(v, str) for v in agents.values()):
        raise ConfigError("config.toml の [a2a.agents] は「名前 = \"住所\"」の形で書いてください")
    orchestrator = data.get("orchestrator", "")
    if not isinstance(orchestrator, str):
        raise ConfigError("config.toml の [a2a] orchestrator は住所の文字列で書いてください")
    defaults = {spec.name: f"http://127.0.0.1:{spec.port}" for spec in enabled if spec.port}
    return A2AConfig(agents={**defaults, **agents}, timeout_seconds=float(data.get("timeout_seconds", 300)),
                     orchestrator=orchestrator)


def _enabled_modules(data: dict, home: Path) -> list[modules.ModuleSpec]:
    """設定の modules（書かなければ組み込み全部）を、知っているモジュールから選ぶ。利用者のモジュールもここで読む。"""
    try:
        modules.register_user_modules(home / "modules")
    except modules.ModuleError as e:
        raise ConfigError(str(e)) from None
    known = modules.known()
    names = data.get("modules", list(modules.builtin()))
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ConfigError("config.toml の modules は、モジュールの名前の配列にしてください（例: modules = [\"knowledge\"]）")
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ConfigError(f"config.toml の modules に知らないモジュールがあります: {', '.join(unknown)}"
                          f"（知っているもの: {', '.join(sorted(known)) or 'なし'}）")
    enabled = [known[n] for n in dict.fromkeys(names)]
    for spec in enabled:
        missing = [r for r in spec.requires if r not in names]
        if missing:
            raise ConfigError(f"モジュール「{spec.name}」には {', '.join(missing)} が要ります（config.toml の modules に足してください）")
    # 使ってよいモデルの一覧はコアにある（model_policy）。読み込みの順番のため、ここで読む
    from kei_agent.model_policy import check_module_recipes
    for spec in enabled:
        check_module_recipes(spec)
    return enabled


def _agent_profiles(data: dict) -> dict[str, AgentProfile]:
    """[agents.<name>] を読み、未指定の actor は provider 未選択にする。"""
    if not isinstance(data, dict):
        raise ConfigError("config.toml の [agents] はテーブルにしてください")
    unknown = sorted(set(data) - model_actors())
    if unknown:
        raise ConfigError(f"config.toml の [agents] に知らないagentがあります: {', '.join(unknown)}")
    profiles = _default_agent_profiles()
    for name, raw in data.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"config.toml の [agents.{name}] はテーブルにしてください")
        _check_keys(raw, AGENT_PROFILE_KEYS, f"[agents.{name}]")
        provider = str(raw.get("provider", ""))
        if provider not in {"", "claude", "codex"}:
            raise ConfigError(f"config.toml の [agents.{name}].provider は claude、codex、または空文字にしてください")
        profiles[name] = AgentProfile(provider=provider)
    return profiles


def user_home(env: dict[str, str] | None = None) -> Path:
    """利用者のフォルダ（環境変数 KEI_AGENT_HOME、なければ ~/.config/kei-agent）。"""
    env = dict(os.environ) if env is None else env
    return _expand(env.get("KEI_AGENT_HOME") or DEFAULT_HOME)


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    """設定を読む。場所は path、環境変数 KEI_AGENT_CONFIG、利用者のフォルダの config.toml の順に探す。

    利用者のフォルダは KEI_AGENT_HOME か、設定ファイルのあるフォルダ（path を渡したとき）か、~/.config/kei-agent。
    """
    env = dict(os.environ) if env is None else env
    if path is None and env.get("KEI_AGENT_CONFIG"):
        path = _expand(env["KEI_AGENT_CONFIG"])
    home = user_home(env) if env.get("KEI_AGENT_HOME") or path is None else path.parent
    path = path or home / CONFIG_FILE
    if not path.is_file():
        raise ConfigError(f"設定ファイルがありません: {path}。{EXAMPLE_CONFIG.name} を写して書き換えてください"
                          f"（例: cp {EXAMPLE_CONFIG} {path}）")
    with path.open("rb") as f:
        data = tomllib.load(f)

    schedule = data.get("schedule", {})
    channels = data.get("channels", {})
    sandbox = data.get("sandbox", {})
    _check_keys(data, TOP_LEVEL_KEYS, "一番外側")
    enabled = _enabled_modules(data, home)
    _check_keys(channels, CHANNELS_KEYS | {kind for spec in enabled for kind in spec.channels}, "[channels]")
    _check_keys(sandbox, SANDBOX_KEYS, "[sandbox]")
    paths = data.get("paths", {})
    _check_keys(paths, PATHS_KEYS, "[paths]")
    module_schedules = [s for spec in enabled for s in spec.schedules]
    _check_times(schedule, data.get("maintenance", {}), tuple(s.name for s in module_schedules))
    state_dir = _expand(data.get("state_dir", "~/.local/state/kei-agent"))
    secrets_dir = _expand(paths.get("secrets", str(home / "secrets")))
    # 既定の読ませない場所には、使うたびに更新するトークン（Box など）の置き場も足す。
    # 秘密情報の置き場所は、deny_read を書き換えていても必ず足す
    deny_read = [*sandbox.get("deny_read", (*DEFAULT_DENY_READ, str(state_dir / "secrets")))]
    if str(secrets_dir) not in {str(_expand(p)) for p in deny_read}:
        deny_read.append(str(secrets_dir))
    return Config(
        research_root=_expand(data.get("research_root", "~/research")),
        agent_root=_expand(data.get("agent_root", "~/kei-agent")),
        course_root=_expand(data.get("course_root", "~/course")),
        state_dir=state_dir,
        repo_root=REPO_ROOT,
        allowed_user_id=env.get("KEI_AGENT_ALLOWED_USER_ID", ""),
        overview_channels=tuple(channels.get("overview", Config.overview_channels)),
        improve_channels=tuple(channels.get("improve", Config.improve_channels)),
        course_channels=tuple(channels.get("course", Config.course_channels)),
        work_channels=tuple(channels.get("work", Config.work_channels)),
        modules=tuple(spec.name for spec in enabled),
        module_channels={kind: tuple(channels.get(kind, names)) for spec in enabled
                         for kind, names in spec.channels.items()},
        max_concurrent_runs=int(data.get("max_concurrent_runs", 2)),
        run_timeout_minutes=int(data.get("run_timeout_minutes", 30)),
        job_poll_seconds=int(data.get("job_poll_seconds", 60)),
        job_parallel=int(data.get("job_parallel", 1)),
        agent_profiles=_agent_profiles(data.get("agents", {})),
        handoff_after_turns=int(data.get("handoff_after_turns", 8)),
        allowed_domains=tuple(sandbox.get("allowed_domains", ())),
        allow_write=tuple(_expand(p) for p in sandbox.get("allow_write", ())),
        deny_read=tuple(_expand(p) for p in deny_read),
        claude_bin=env.get("KEI_AGENT_CLAUDE_BIN", "claude"),
        codex_bin=env.get("KEI_AGENT_CODEX_BIN", "codex"),
        pueue_bin=env.get("KEI_AGENT_PUEUE_BIN", "pueue"),
        schedule=_schedule(schedule, module_schedules),
        maintenance=_section(MaintenanceConfig, data.get("maintenance", {}), "maintenance"),
        a2a=_a2a(data.get("a2a", {}), enabled),
        notion=_notion(data.get("notion", {})),
        a2a_token=env.get("KEI_AGENT_A2A_TOKEN", ""),
        user_dir=home,
        secrets_dir=secrets_dir,
    )
