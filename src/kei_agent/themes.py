"""チャンネルとテーマ、作業用ディレクトリの対応。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kei_agent.config import Config

THEME_SUBDIRS = ("inputs", "outputs", "logs")

CLAUDE_MD_TEMPLATE = """# テーマ: {name}

Slack の #{name} チャンネルに対応する作業用ディレクトリ。Kei Agent（Slack Bot）がここで作業する。

## 研究の前提

<!-- 研究分野、目的、いま検証していること、使っているデータやモデルを書く -->

## 検索キーワード

<!-- 毎朝 07:00 に、ここに書いたキーワードで arXiv の新着を探す（知識の担当が、この前提と見比べて選ぶ）。1行に1つ、英語で書く（例: - vision language model counting） -->

## ディレクトリ

- `inputs/`: Slack で渡されたファイル（CSV など）
- `outputs/`: 図や集計結果。ここに新しくできたファイルは Slack のスレッドに添付される
- `logs/`: ジョブのログ
- 論文は、研究ホームの「先行研究」DB に残す（このディレクトリには置かない）

## ジョブにする基準

- 数分以上かかりそうな処理は、その場で実行せず `kei-agent-research:running-jobs` skill でジョブにする
- それより短い処理は、その場で実行してよい
"""

COURSE_CLAUDE_MD = """# 授業（大学エージェントの作業場）

大学エージェントの claude がここで動く。授業と課題の資料は Box に置いたまま読むので、
このディレクトリには置かない（作った表やメモだけを置く）。

## 履修している科目

<!-- Notion の「授業」が正。ここには、学期ごとの補足（教室、担当、試験の形式など）を書く -->

## 覚えておいてほしいこと

<!-- 例: 「レポートは PDF で出す」「過去問は Box の Personal/過去問/ の下」 -->
"""

OVERVIEW_CLAUDE_MD = """# 研究全体・中長期の方針

Slack の研究全体と中長期の方針のチャンネルに対応する作業用ディレクトリ。
各テーマのディレクトリ（`../<theme>/`）は読むだけにし、書き込みはこのディレクトリの中だけにする。
"""


class ChannelKind(Enum):
    THEME = "theme"
    OVERVIEW = "overview"
    IMPROVE = "improve"
    # 大学（授業と課題）。ここでの依頼は大学エージェントに取り次ぐだけで、ファイルは持たない
    COURSE = "course"
    # 仕事（会社の予定など）。同じく、仕事エージェントに取り次ぐ
    WORK = "work"
    # 知識（読みもの・論文の新着・その質問）。知識エージェントに取り次ぐだけで、ファイルは持たない
    KNOWLEDGE = "knowledge"
    # Kei Agent 自身を直すときの worktree（improve.py）。書き込めるのはその中だけ
    SELF_FIX = "self_fix"


@dataclass(frozen=True)
class Workspace:
    channel_name: str
    kind: ChannelKind
    # claude -p を動かすディレクトリ。IMPROVE では None
    cwd: Path | None
    # config.toml の基本の接続先に足して、このテーマで許可した接続先（Slack で許可したもの。settings.py）
    allowed_domains: tuple[str, ...] = ()
    # ここから下は、エージェントが自分の claude を動かすときの上乗せ（docs/architecture.md の「振り分けと A2A」）
    # そのエージェントの指示書（prompts/<agent>.md）。既定は prompts/system.md
    system_prompt: Path | None = None
    # claude 1回の上限時間（分）。既定は config.run_timeout_minutes
    timeout_minutes: int | None = None


# Slack のチャンネル名は日本語も使えるので、パスとして危ない形だけを弾く
_SAFE_NAME = re.compile(r"^[^./_\\\x00][^/\\\x00]{0,79}$")
# チャンネル名の先頭の番号（`10_amr-query` の `10_`）。並び順のためのもので、名前の一部として扱わない
_NUMBER_PREFIX = re.compile(r"^\d{2,}_")


def theme_name(channel_name: str) -> str:
    """チャンネル名から、テーマの名前（フォルダ名、Notion のテーマ名）を作る。

    Slack では並び順のために `10_amr-query` のような番号を付ける。番号を変えても
    同じテーマを指し続けられるよう、先頭の番号は外して扱う。
    """
    return _NUMBER_PREFIX.sub("", channel_name)


def resolve(config: Config, channel_name: str) -> Workspace:
    """チャンネル名から作業場所を決める。研究全体・改善・大学・仕事以外は、すべて研究テーマとして扱う。"""
    channel_name = theme_name(channel_name)
    if channel_name in config.improve_channels:
        return Workspace(channel_name, ChannelKind.IMPROVE, None)
    if channel_name in config.work_channels:
        return Workspace(channel_name, ChannelKind.WORK, None)
    if channel_name in config.course_channels:
        # 作業場は大学エージェントの claude が使う（本体はここで claude を動かさない）
        return Workspace(channel_name, ChannelKind.COURSE, config.course_root)
    if channel_name in config.module_channels.get("knowledge", ()):
        return Workspace(channel_name, ChannelKind.KNOWLEDGE, None)
    if channel_name in config.overview_channels:
        return Workspace(channel_name, ChannelKind.OVERVIEW, config.overview_dir)
    if not _SAFE_NAME.match(channel_name) or ".." in channel_name:
        raise ValueError(f"テーマ名に使えないチャンネル名です: {channel_name!r}")
    return Workspace(channel_name, ChannelKind.THEME, config.research_root / channel_name)


# チャンネルの種類ごとに、会話を続ける担当（研究テーマと研究全体は研究の担当）
_ACTORS = {ChannelKind.COURSE: "course", ChannelKind.WORK: "work", ChannelKind.IMPROVE: "self_fix",
           ChannelKind.KNOWLEDGE: "knowledge"}


def actor_of(kind: ChannelKind) -> str:
    return _ACTORS.get(kind, "research")


def agent_workspace(config: Config, agent: str) -> Workspace:
    """大学・仕事のエージェントが AI を動かす場所。会話の続きは作業場ごとに残るので、毎回同じ場所にする。

    大学は `course_root`（前提のメモの CLAUDE.md を置く）、仕事は状態の置き場の下。どちらも手元のファイルは
    作業場を読むだけ（制限の表）。
    """
    if agent == "course":
        ws = Workspace(agent, ChannelKind.COURSE, config.course_root)
    elif agent == "work":
        ws = Workspace(agent, ChannelKind.WORK, config.state_dir / "agents" / agent)
    elif agent == "knowledge":
        ws = Workspace(agent, ChannelKind.KNOWLEDGE, config.state_dir / "agents" / agent)
    else:
        raise ValueError(f"作業場を持たないエージェントです: {agent}")
    ensure_workspace(ws)
    return ws


def theme_dirs(config: Config) -> list[Path]:
    """研究テーマの作業用ディレクトリ。

    「どれがテーマか」の判断は resolve() に合わせる（2か所で別々に決めない）。
    """
    root = config.research_root
    if not root.is_dir():
        return []
    dirs = []
    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name.startswith((".", "_")):
            continue
        try:
            if resolve(config, p.name).kind is ChannelKind.THEME:
                dirs.append(p)
        except ValueError:
            continue
    return dirs


def ensure_workspace(ws: Workspace) -> bool:
    """作業用ディレクトリとひな形を作る。新しく作ったら True。"""
    if ws.cwd is None:
        return False
    created = not ws.cwd.exists()
    ws.cwd.mkdir(parents=True, exist_ok=True)
    claude_md = ws.cwd / "CLAUDE.md"
    if ws.kind is ChannelKind.THEME:
        for sub in THEME_SUBDIRS:
            (ws.cwd / sub).mkdir(exist_ok=True)
        if not claude_md.exists():
            claude_md.write_text(CLAUDE_MD_TEMPLATE.format(name=ws.channel_name), encoding="utf-8")
    elif ws.kind is ChannelKind.OVERVIEW:
        (ws.cwd / "outputs").mkdir(exist_ok=True)
        if not claude_md.exists():
            claude_md.write_text(OVERVIEW_CLAUDE_MD, encoding="utf-8")
    elif ws.kind is ChannelKind.COURSE and not claude_md.exists():
        claude_md.write_text(COURSE_CLAUDE_MD, encoding="utf-8")
    return created
