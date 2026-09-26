"""エージェントごとの制限の表。Claude と Codex の設定は、ここから作る。"""

import pytest

from kei_agent.agent_policy import NOTION_READ_TOOLS, POLICIES, policy_of
from kei_agent.config import model_actors
from kei_agent.model_policy import UseCase


def test_every_actor_has_one_policy():
    """本体の実行役は表に、モジュールの実行役は module.toml の [actor] から、どれも1つの制限を持つ。"""
    assert set(POLICIES) <= set(model_actors())
    assert all(policy_of(actor).name == actor for actor in model_actors())


def test_course_reads_box_and_writes_notion_only_through_the_gateway():
    course = policy_of("course")
    assert (course.files, course.shell, course.web) == ("none", False, False)
    assert course.notion == "write" and course.notion_tools is None
    assert [c.name for c in course.connectors] == ["box"]
    assert all("upload" not in tool and "move" not in tool for tool in course.connectors[0].claude_tools)
    # Codex の Box も、同じ読む道具だけ
    assert course.codex_apps[0].tools == tuple(f"box.{tool}" for tool in course.connectors[0].claude_tools)


def test_work_has_no_notion_and_reads_outlook_teams_and_sharepoint():
    work = policy_of("work")
    assert work.notion == "none" and work.notion_tools == ()
    assert [c.name for c in work.connectors] == ["outlook", "teams-sharepoint"]
    names = [name for connector in work.connectors for name in connector.claude_names()]
    for tool in ("outlook_calendar_search", "chat_message_search", "teams_list_channel_messages",
                 "sharepoint_search", "read_resource"):
        assert f"mcp__claude_ai_Microsoft_365__{tool}" in names
    writes = ("send", "create", "delete", "reply", "update", "move", "copy", "rename", "forward", "trash")
    assert not any(word in name for name in names for word in writes)
    # Codex はメールと予定の App を読む（Teams・SharePoint の App は、道具の名前を確かめてから足す）
    assert [app.name for app in work.codex_apps] == ["Microsoft Outlook Email", "Microsoft Outlook Calendar"]
    codex_tools = [tool for app in work.codex_apps for tool in app.tools]
    assert not any(word in tool for tool in codex_tools for word in writes + ("draft", "mark_", "cancel"))


def test_read_only_drops_writing_and_commands_but_keeps_reading():
    research = policy_of("research", read_only=True)
    assert (research.files, research.shell, research.notion) == ("read", False, "read")
    assert research.notion_tools == NOTION_READ_TOOLS
    assert research.web is True


def test_routing_use_case_gets_no_tools_whatever_the_actor():
    for actor in ("research", "course", "work"):
        policy = policy_of(actor, UseCase.ROUTING)
        assert policy.name == "router"
        assert (policy.plugin, policy.shell, policy.web, policy.connectors) == (False, False, False, ())
        assert policy.notion_tools == ()


def test_router_is_always_read_only():
    assert policy_of("router").files == "read"


def test_unknown_actor_is_rejected():
    with pytest.raises(ValueError, match="未知"):
        policy_of("hobby")
