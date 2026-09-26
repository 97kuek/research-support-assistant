from __future__ import annotations

import os
from pathlib import Path

import pytest
from fakes import FakeGitHub

from kei_agent import improve, issues, model_classifier, modules, research
from kei_agent.config import REPO_ROOT, AgentProfile, Config, model_actors
from kei_agent.store import Store

# 開発機のシェルには本物の秘密情報が入っている。テストから Toggl・Notion・Slack などに届かないよう、
# 各テストの前に消す（使うテストは monkeypatch.setenv で入れ直す）
_SECRET_PREFIXES = ("TOGGL_", "NOTION_", "SLACK_", "KEI_AGENT_", "BOX_", "WANDB_", "OPENAI_")


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch):
    for name in list(os.environ):
        if name.startswith(_SECRET_PREFIXES):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def no_real_restarts(monkeypatch):
    """本物の launchd の担当を起動し直さない。

    自己改善の取り込みや古い担当の入れ替えのテストは、本物の com.kei-agent.* に kickstart をかけてしまう
    （2026-09-26、テストを回すたびに本番の担当が起動し直されていた）。確かめたいテストは、自分で差し替える。
    """
    restarted: list[str] = []
    monkeypatch.setattr(improve, "restart_service", lambda name: restarted.append(name) or True)
    return restarted


@pytest.fixture(autouse=True)
def no_user_modules(monkeypatch):
    """利用者のモジュールは、テストごとに空から始める（読んだものがほかのテストに残らない）。"""
    monkeypatch.setattr(modules, "_user", {})


@pytest.fixture(autouse=True)
def kei_agent_home(no_real_secrets, tmp_path_factory, monkeypatch):
    """利用者のフォルダ（~/.config/kei-agent）は、テストごとに空の設定だけのものにする（開発機の本物を読まない）。"""
    home = tmp_path_factory.mktemp("kei-agent-home")
    (home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("KEI_AGENT_HOME", str(home))
    return home


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        research_root=tmp_path / "research",
        agent_root=tmp_path / "kei-agent",
        course_root=tmp_path / "course",
        state_dir=tmp_path / "state",
        repo_root=REPO_ROOT,
        allowed_user_id="UME",
        allowed_domains=("export.arxiv.org",),
        allow_write=(tmp_path / "cache",),
        deny_read=(tmp_path / "secrets",),
        # 個別の unit test は既存経路の振る舞いを検証する。製品の config.toml は未選択で始まる。
        agent_profiles={name: AgentProfile(provider="claude") for name in model_actors()},
    )


@pytest.fixture
def store(config: Config) -> Store:
    return Store(config.db_path)


@pytest.fixture(autouse=True)
def fake_model_classifier(monkeypatch):
    """通常の unit test は本物の CLI を起動せず、既存の用途判定だけを再現する。"""
    async def classify(_config, _store, prompt: str, **_kwargs):
        return research.use_case_for_prompt(prompt)[0]

    monkeypatch.setattr(model_classifier, "classify_research", classify)

    async def course(_config, _store, _prompt: str):
        from kei_agent.model_policy import UseCase
        return UseCase.COURSE_EXPLAIN

    async def work(_config, _store, _prompt: str):
        from kei_agent.model_policy import UseCase
        return UseCase.WORK_SINGLE_SOURCE

    monkeypatch.setattr(model_classifier, "classify_course", course)
    monkeypatch.setattr(model_classifier, "classify_work", work)


@pytest.fixture(autouse=True)
def fake_github(monkeypatch):
    """要望の issue 化で、本物の gh（公開リポジトリ）と要約のモデルを動かさない。

    確かめたいテストは、引数に `fake_github` を書いて偽物を受け取る。
    """
    github = FakeGitHub()
    monkeypatch.setattr(issues, "gh", github)

    async def summarize(_config, _store, _text: str):
        return issues.Summary("Kei Agent への要望", "- 要望の要約")

    monkeypatch.setattr(issues, "summarize", summarize)
    return github


@pytest.fixture(autouse=True)
def no_date_line(monkeypatch):
    """担当への依頼の先頭に付く今日の日付を、ふだんのテストでは空にする（依頼の本文だけを確かめられるように）。

    日付が付くことは test_assistant.py の専用のテストで確かめる。
    """
    from kei_agent import assistant

    monkeypatch.setattr(assistant, "today_line", lambda now=None: "")

