import asyncio
from dataclasses import replace

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import home, runner, settings, themes
from kei_agent.assistant import Assistant
from kei_agent.jobs import JobManager


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "research-overview"})
    monkeypatch.setattr(runner, "run_model", FakeClaude())
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    themes.ensure_workspace(themes.resolve(config, "vlm"))
    return assistant, slack


def _texts(view) -> str:
    out = []
    for b in view["blocks"]:
        out.append((b.get("text") or {}).get("text", ""))
        out += [e.get("text", "") for e in b.get("elements", []) if isinstance(e.get("text"), str)]
    return "\n".join(out)


def _published(slack):
    return [kw for name, kw in slack.calls if name == "views_publish"][-1]


def _action(action_id, value="", user="UME", **extra):
    return {"user": {"id": user}, "trigger_id": "trig", "actions": [{"action_id": action_id, "value": value, **extra}]}


def test_home_lists_theme_domains_and_schedule(config, store):
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    view = home.build_home(config, store, ["vlm"], is_owner=True)
    text = _texts(view)
    # テーマ名に `#` は付けない（`10_vlm` のようなチャンネル名とずれて見えるので）
    assert "*vlm*" in text and "#vlm" not in text and "`zenodo.org`" in text
    assert "Daily" in text
    assert "export.arxiv.org" not in text  # 基本の接続先は出さない
    pickers = [b["accessory"] for b in view["blocks"] if b.get("accessory", {}).get("type") == "timepicker"]
    assert len(pickers) == len(settings.schedule_names(config))


def test_home_shows_agent_provider_controls(config, store):
    from kei_agent.config import AgentProfile, model_actors

    config = replace(config, agent_profiles={name: AgentProfile() for name in model_actors()})
    view = home.build_home(config, store, [], is_owner=True)
    course, = [b["accessory"] for b in view["blocks"]
               if b.get("accessory", {}).get("action_id") == "kei_agent_home_provider:course"]
    # 選んでいなければ、欄に「未選択」と出るだけ（説明文は置かない）
    assert "initial_option" not in course and course["placeholder"]["text"] == "未選択"
    controls = [block.get("accessory", {}) for block in view["blocks"]]
    assert {element.get("action_id") for element in controls} >= {
        "kei_agent_home_provider:research", "kei_agent_home_provider:course", "kei_agent_home_provider:work",
        "kei_agent_home_provider:router", "kei_agent_home_provider:self_fix",
        "kei_agent_home_provider:knowledge",
    }


def test_home_exposes_no_model_or_effort_override(config, store):
    view = home.build_home(config, store, [], is_owner=True)
    action_ids = {element.get("action_id") for block in view["blocks"]
                  for element in [block.get("accessory", {}), *block.get("elements", [])]}
    assert not any(action_id.startswith("kei_agent_home_model:") for action_id in action_ids if action_id)
    assert not any(action_id.startswith("kei_agent_home_effort:") for action_id in action_ids if action_id)


def test_home_for_someone_else_changes_nothing(config, store):
    view = home.build_home(config, store, ["vlm"], is_owner=False)
    assert "依頼者だけ" in _texts(view)
    assert not any(b.get("accessory") or b["type"] == "actions" for b in view["blocks"])


async def test_opening_home_publishes_it(env):
    assistant, slack = env
    await assistant.on_home_opened({"type": "app_home_opened", "user": "UME", "tab": "home"})
    assert _published(slack)["user_id"] == "UME"


async def test_remove_domain_from_home(env, store):
    assistant, slack = env
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    await assistant.on_home_action(_action("kei_agent_home_remove_domain", "vlm\tzenodo.org"))
    assert settings.theme_domains(store, "vlm") == []
    assert "zenodo.org" not in _texts(_published(slack)["view"])


async def test_remove_domain_with_the_select(env, store):
    assistant, slack = env
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    view = home.build_home(assistant.config, store, ["vlm"], is_owner=True)
    remove, = [b["accessory"] for b in view["blocks"]
               if b.get("accessory", {}).get("action_id") == home.REMOVE_DOMAIN_ACTION]
    await assistant.on_home_action(_action(home.REMOVE_DOMAIN_ACTION, selected_option=remove["options"][0]))
    assert settings.theme_domains(store, "vlm") == []


async def test_someone_else_cannot_change_settings(env, store):
    assistant, slack = env
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    await assistant.on_home_action(_action("kei_agent_home_remove_domain", "vlm\tzenodo.org", user="USOMEONE"))
    assert settings.theme_domains(store, "vlm") == ["zenodo.org"]


def _checked(*values):
    return [{"value": value} for value in values]


async def test_change_time_and_turn_schedules_on_and_off_from_home(env, config, store):
    assistant, slack = env
    others = [name for name in settings.schedule_names(config) if name != "daily"]
    await assistant.on_home_action(_action("kei_agent_home_time:daily", selected_time="07:30"))
    assert settings.schedule_time(config, store, "daily") == "07:30"
    await assistant.on_home_action(_action(home.SCHEDULES_ACTION, selected_options=_checked(*others)))
    assert settings.schedule_time(config, store, "daily") == ""
    assert all(settings.schedule_time(config, store, name) for name in others)
    await assistant.on_home_action(_action(home.SCHEDULES_ACTION, selected_options=_checked(*settings.schedule_names(config))))
    assert settings.schedule_time(config, store, "daily") == "07:30"
    boxes, = [e for b in _published(slack)["view"]["blocks"] for e in b.get("elements", [])
              if e.get("action_id") == home.SCHEDULES_ACTION]
    assert {o["value"] for o in boxes["initial_options"]} == set(settings.schedule_names(config))


async def test_voice_checkboxes_open_and_close_the_microphone(env, store, monkeypatch):
    assistant, slack = env
    heard = []
    monkeypatch.setattr(assistant, "notify_listening", heard.append)

    await assistant.on_home_action(_action(home.VOICE_ACTION, selected_options=_checked("voice", "listen")))
    assert settings.voice_enabled(store) and settings.listening_enabled(store) and heard == [True]
    await assistant.on_home_action(_action(home.VOICE_ACTION, selected_options=_checked("voice")))
    assert settings.voice_enabled(store) and not settings.listening_enabled(store) and heard == [True, False]
    await assistant.on_home_action(_action(home.VOICE_ACTION, selected_options=[]))
    assert not settings.voice_enabled(store) and heard == [True, False]


async def test_home_provider_action_changes_the_next_agent_run(env, store):
    assistant, _ = env
    await assistant.on_home_action(_action("kei_agent_home_provider:course", selected_option={"value": "codex"}))
    assert settings.agent_profile(assistant.config, store, "course").provider == "codex"


async def test_research_provider_selection_uses_the_use_case_recipe(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm"})
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    settings.set_agent_provider(store, "research", "codex")
    seen = {}

    async def fake_run(_config, request, _prompt, *_args, **_kwargs):
        seen.update(model=request.recipe.model, effort=request.recipe.reasoning_effort)
        return runner.RunResult(text="ok")

    monkeypatch.setattr(runner, "run_model", fake_run)
    await assistant.run_agent(themes.resolve(config, "vlm"), "実験計画を設計して")

    assert seen == {"model": "gpt-6-sol", "effort": "xhigh"}


async def test_add_domain_through_modal(env, store):
    assistant, slack = env
    await assistant.on_home_action(_action("kei_agent_home_add_domain", "add"))
    opened, = [kw for name, kw in slack.calls if name == "views_open"]
    assert opened["view"]["callback_id"] == home.ADD_DOMAIN_CALLBACK

    def submitted(theme, domain):
        return {"user": {"id": "UME"}, "view": {"state": {"values": {
            "theme": {"value": {"selected_option": {"value": theme}}},
            "domain": {"value": {"value": domain}}}}}}

    assert "domain" in await assistant.on_add_domain(submitted("vlm", "https://zenodo.org"))
    assert "theme" in await assistant.on_add_domain(submitted("nothere", "zenodo.org"))
    assert await assistant.on_add_domain(submitted("vlm", "*.githubusercontent.com")) is None
    assert settings.theme_domains(store, "vlm") == ["*.githubusercontent.com"]


async def test_archiving_the_channel_drops_theme_domains(env, store):
    assistant, slack = env
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    await assistant.on_channel_archive({"type": "channel_archive", "channel": "C1"})
    await asyncio.sleep(0)
    assert settings.theme_domains(store, "vlm") == []


# いま動いているもの


def test_home_shows_what_is_running(config, store):
    import time

    store.start_run("C1", "10.1", "vlm", "message")
    finished = store.start_run("C1", "10.0", "vlm", "message")
    store.end_run(finished, False, None)
    job = store.add_job("r1", "C1", "10.1", str(config.research_root / "vlm"), "sweep", "scripts/sweep.py",
                        status="queued")
    store.update_job(job.id, pueue_id=3, status="running")
    store.upsert_thread("C5", "20.1", "research-overview", None)
    store.set_awaiting("C5", "20.1", True)

    text = _texts(home.build_home(config, store, ["vlm"], is_owner=True))

    assert "動いているもの" in text
    assert "#vlm" in text and "依頼" in text            # 動いている依頼
    assert "ジョブ 1「sweep」実行中" in text
    assert "#research-overview" in text and "返事待ち" in text
    assert str(int(time.time())) not in text            # 時刻ではなく経過時間で出す


def test_home_says_when_nothing_is_running(config, store):
    blocks = home.build_home(config, store, [], is_owner=True)["blocks"]
    assert blocks[1]["text"]["text"] == "*動いているもの*" and blocks[2]["elements"][0]["text"] == "なし"


def test_home_has_no_explanations(config, store):
    """見出しと操作だけ。説明の文（context）は「なし」などの状態だけにする。"""
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    blocks = home.build_home(config, store, ["vlm"], is_owner=True)["blocks"]
    contexts = [e["text"] for b in blocks if b["type"] == "context" for e in b["elements"]]
    assert contexts == ["なし"]
    assert "config.toml" not in _texts({"blocks": blocks})


async def test_refresh_button_rebuilds_the_home(env):
    assistant, slack = env
    await assistant.on_home_action(_action(home.REFRESH_ACTION, "refresh"))
    assert _published(slack)["user_id"] == "UME"
