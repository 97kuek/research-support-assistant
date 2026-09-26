"""エージェントごとの制限の正本（docs/architecture.md の「柵」）。

Claude の設定（許可する道具、MCP、sandbox）と Codex の設定（権限 profile、App、MCP、Web 検索）は、
どちらもこの表から作る。provider で効く範囲が変わらないよう、同じ一覧をほかの場所に書かない。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from kei_agent import modules
from kei_agent.model_policy import UseCase

# Notion ゲートウェイの MCP の名前（全エージェント共通）。どのホームに届くかは、合言葉でゲートウェイが決める
NOTION_MCP = "kei-notion"
# 読むだけの実行に渡すゲートウェイの道具
NOTION_READ_TOOLS = ("read", "search", "query")

Access = Literal["none", "read", "write"]


@dataclass(frozen=True)
class CodexApp:
    """Codex App。ID はアカウントごとに違うので、実行のたびに表示名から引く。"""

    name: str
    # 使う道具（`<App の名前空間>.<道具>`）。ここに無い道具は、Codex がモデルに見せない
    tools: tuple[str, ...]


def _codex_tools(namespace: str, tools: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{namespace}.{tool}" for tool in tools)


@dataclass(frozen=True)
class Connector:
    """アカウントの連携（Claude は claude.ai、Codex は Codex App）。どちらも読む道具だけを使う。"""

    name: str
    # Claude: claude.ai の連携の名前（`mcp__<server>__<tool>`）と、許す道具
    claude_server: str
    claude_tools: tuple[str, ...]
    # Codex: 使う App と、その読む道具。無ければ Codex ではこの連携を使わない
    codex_apps: tuple[CodexApp, ...] = ()

    def claude_names(self) -> tuple[str, ...]:
        return tuple(f"mcp__{self.claude_server}__{tool}" for tool in self.claude_tools)


# Box: 探す・中身を読む・ページを画像で見る（手書きやスキャンの過去問）。道具の名前は Claude と Codex で同じ
_BOX_READS = ("search_files_keyword", "search_folders_by_name", "list_folder_content_by_folder_id",
              "get_file_details", "get_file_content", "get_file_preview", "get_preview_page")
BOX = Connector("box", "claude_ai_Box", _BOX_READS, (CodexApp("Box", _codex_tools("box", _BOX_READS)),))
# 会社の Microsoft 365 の Outlook: 予定・メール・人・空き時間を探して読む。送信・作成・変更・削除はしない。
# Codex ではメールと予定が別の App で、道具の名前も違う
OUTLOOK = Connector(
    "outlook", "claude_ai_Microsoft_365",
    ("outlook_calendar_search", "outlook_email_search", "search_people", "find_meeting_availability",
     "read_resource"),
    (CodexApp("Microsoft Outlook Email", _codex_tools("microsoft_outlook_email", (
        "search_messages", "list_messages", "get_recent_emails", "fetch_message", "fetch_messages_batch",
        "list_mail_folders", "find_mail_folder", "list_attachments", "fetch_attachment",
        "search_people", "search_directory_users"))),
     CodexApp("Microsoft Outlook Calendar", _codex_tools("microsoft_outlook_calendar", (
         "search_events", "list_events", "list_event_instances", "list_recurring_series", "fetch_event",
         "fetch_events_batch", "list_calendars", "find_available_slots", "get_schedule",
         "search_people", "search_directory_users")))),
)
# 同じ連携の Teams と SharePoint: メッセージと資料を探して読む。投稿・ファイルの作成・移動・削除はしない。
# Codex の Teams・SharePoint の App は、道具の名前を確かめてから足す（いまの Codex は Outlook だけを読む）
TEAMS_SHAREPOINT = Connector(
    "teams-sharepoint", "claude_ai_Microsoft_365",
    ("chat_message_search", "teams_list_teams", "teams_list_channels", "teams_list_channel_messages",
     "teams_list_chats", "sharepoint_search", "sharepoint_folder_search"),
)


@dataclass(frozen=True)
class AgentPolicy:
    """1つのエージェントが、どこまで触れるか。"""

    name: str
    # 指示書（prompts/ の中）。作業場が別の指示書を持つとき（振り分け・分類）は、そちらを使う
    prompt: str
    # plugin/<name>/ の skill と、二の柵のフック
    plugin: bool
    # 作業場のファイル。none でも自分の作業場（前提のメモと skill）は読める。
    # write でも、書けるのは作業場と [sandbox] allow_write だけ
    files: Access
    # 作業場でのコマンド（sandbox の中）
    shell: bool
    # Web の検索と取得
    web: bool
    # Notion ゲートウェイ（利用者の名前はエージェントの名前）
    notion: Access
    connectors: tuple[Connector, ...] = ()
    # 1回の上限時間（分）。None なら config.toml の run_timeout_minutes
    timeout_minutes: int | None = None

    def narrowed(self, read_only: bool) -> AgentPolicy:
        """読むだけの実行では、書く・動かす手段を外す（連携は、もとから読む道具だけ）。"""
        if not read_only:
            return self
        return replace(self, files=_read(self.files), shell=False, notion=_read(self.notion))

    @property
    def codex_apps(self) -> tuple[CodexApp, ...]:
        return tuple(app for connector in self.connectors for app in connector.codex_apps)

    @property
    def notion_tools(self) -> tuple[str, ...] | None:
        """ゲートウェイで使える道具。None なら全部、空ならゲートウェイを渡さない。"""
        return {"none": (), "read": NOTION_READ_TOOLS, "write": None}[self.notion]


def _read(access: Access) -> Access:
    return "read" if access == "write" else access


POLICIES: dict[str, AgentPolicy] = {
    "research": AgentPolicy("research", "system.md", plugin=True, files="write", shell=True, web=True,
                            notion="write"),
    # Notion は授業ホームの中だけ（ゲートウェイが決める）。手元のファイル・コマンド・Web は使わない
    "course": AgentPolicy("course", "course.md", plugin=True, files="none", shell=False, web=False,
                          notion="write", connectors=(BOX,), timeout_minutes=5),
    "work": AgentPolicy("work", "work.md", plugin=True, files="none", shell=False, web=False,
                        notion="none", connectors=(OUTLOOK, TEAMS_SHAREPOINT), timeout_minutes=5),
    # 振り分け・分類・Daily/レトプラ。材料はプロンプトで渡すので、読むだけで道具も持たない
    "router": AgentPolicy("router", "system.md", plugin=False, files="read", shell=False, web=False,
                          notion="none"),
    # 自己改善。書けるのは一時ディレクトリか worktree の中だけ（作業場で決まる）
    "self_fix": AgentPolicy("self_fix", "system.md", plugin=False, files="write", shell=True, web=True,
                            notion="none"),
}


def module_policy(spec: modules.ModuleSpec) -> AgentPolicy:
    """モジュールの実行役の制限（module.toml の [actor]）。連携の道具は、枠の版 1 ではまだ持てない。"""
    assert spec.actor is not None
    actor = spec.actor
    return AgentPolicy(spec.name, actor.prompt, plugin=actor.plugin, files=actor.files, shell=actor.shell,
                       web=actor.web, notion=actor.notion, timeout_minutes=actor.timeout_minutes)


def is_offline(use_case: UseCase | str | None) -> bool:
    """Web を使わない用途か（module.toml の offline = true）。

    材料をプロンプトで渡す用途では、外の文（記事・論文の要旨）を読むが、外には出られない回にする
    （外の文・個人の情報・外への出口の3つを1つの回に揃えない。docs/architecture.md の「知識」）。
    """
    owner = modules.use_case_owner(str(use_case)) if use_case else None
    return bool(owner and owner.actor and any(u.name == use_case and u.offline for u in owner.actor.use_cases))


def policy_of(actor: str, use_case: UseCase | str | None = None, *, read_only: bool = False) -> AgentPolicy:
    """実行の制限。振り分け・分類の用途は、どの担当のものでも道具を持たない router にする。"""
    name = "router" if use_case is UseCase.ROUTING or actor == "router" else actor
    spec = modules.known().get(name)
    if name in POLICIES:
        policy = POLICIES[name]
    elif spec is not None and spec.actor is not None:
        policy = module_policy(spec)
    else:
        raise ValueError(f"未知のagentです: {actor}")
    policy = policy.narrowed(read_only or name == "router")
    return replace(policy, web=False) if is_offline(use_case) else policy
