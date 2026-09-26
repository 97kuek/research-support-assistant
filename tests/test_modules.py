"""モジュールの定義（module.toml）と、設定の modules（docs/extensibility.md）。"""

import pytest

from kei_agent import home, modules, settings
from kei_agent.config import ConfigError, load_config
from kei_agent.schedule import task_names

WEATHER = '''api = 1
name = "weather"
label = "天気"
[schedules.weather]
label = "天気と電車"
default = "06:30"
'''


def _module(root, name, text):
    (root / name).mkdir(parents=True)
    (root / name / "module.toml").write_text(text, encoding="utf-8")
    return root / name


def _config(tmp_path, text=""):
    home_dir = tmp_path / "home"
    home_dir.mkdir(exist_ok=True)
    (home_dir / "config.toml").write_text(text, encoding="utf-8")
    return home_dir


def test_the_knowledge_module_is_described_by_its_definition():
    spec = modules.builtin()["knowledge"]
    assert (spec.label, spec.port, spec.channels) == ("知識", 8792, {"knowledge": ("knowledge",)})
    assert [s.name for s in spec.schedules] == ["literature", "reading"]
    offline = {u.name for u in spec.actor.use_cases if u.offline}
    assert offline == {"knowledge_pick", "knowledge_summary"} and spec.actor.default_use_case == "knowledge_answer"


@pytest.mark.parametrize(("text", "message"), [
    ('api = 2\nname = "x"\n', "枠の版が合いません"),
    ('api = 1\nname = "y"\n', "フォルダの名前"),
    ('api = 1\nname = "x"\ncolor = "red"\n', "知らないキー"),
    ('api = 1\nname = "x"\n[use_cases.a]\nclaude = { model = "m" }\n', "[actor]"),
    ('api = 1\nname = "x"\n[process]\nport = 80\n', "1024"),
    ('api = 1\nname = "x"\n[schedules.a]\ndefault = "7:00"\n', "HH:MM"),
    ('api = 1\nname = "x"\n[actor]\nprompt = "x.md"\n[use_cases.a]\nclaude = { effort = "low" }\n', "model"),
])
def test_a_broken_definition_says_what_is_wrong(tmp_path, text, message):
    with pytest.raises(modules.ModuleError, match=message.replace("[", r"\[").replace("]", r"\]")):
        modules.load_spec(_module(tmp_path, "x", text))


def test_own_modules_come_from_the_user_folder_and_must_not_collide(tmp_path):
    home_dir = _config(tmp_path)
    _module(home_dir / "modules", "weather", WEATHER)
    config = load_config(env={"KEI_AGENT_HOME": str(home_dir)})
    assert "weather" in modules.known() and not modules.known()["weather"].builtin
    assert config.modules == ("knowledge",)            # 知っていても、設定に書くまではオンにしない

    _module(home_dir / "modules", "knowledge", 'api = 1\nname = "knowledge"\n')
    with pytest.raises(ConfigError, match="組み込みのモジュール「knowledge」と同じ名前"):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


def test_two_modules_cannot_share_a_schedule(tmp_path):
    home_dir = _config(tmp_path)
    _module(home_dir / "modules", "news", 'api = 1\nname = "news"\n[schedules.reading]\ndefault = "07:00"\n')
    with pytest.raises(ConfigError, match="定期処理「reading」がぶつかっています"):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


def test_enabled_modules_bring_their_channels_schedules_actors_and_address(tmp_path):
    home_dir = _config(tmp_path, 'modules = ["knowledge", "weather"]\n')
    _module(home_dir / "modules", "weather", WEATHER)
    config = load_config(env={"KEI_AGENT_HOME": str(home_dir)})
    assert config.modules == ("knowledge", "weather")
    assert config.module_channels == {"knowledge": ("knowledge",)}
    assert config.a2a.agents["knowledge"] == "http://127.0.0.1:8792"       # 書かなければ module.toml の番地
    assert task_names(config) == ("night", "literature", "reading", "weather", "daily", "review", "maintenance")
    assert settings.schedule_time(config, _store(config), "weather") == "06:30"
    assert settings.schedule_label(config, "weather") == "天気と電車"
    assert home.agent_labels(config)["knowledge"] == "知識" and "weather" not in home.agent_labels(config)


def test_turning_a_module_off_removes_what_it_brings(tmp_path):
    config = load_config(env={"KEI_AGENT_HOME": str(_config(tmp_path, "modules = []\n"))})
    assert config.modules == () and config.module_channels == {} and "knowledge" not in config.a2a.agents
    assert task_names(config) == ("night", "daily", "review", "maintenance")
    assert "knowledge" not in home.agent_labels(config)


def test_modules_in_the_config_must_exist_and_bring_what_they_require(tmp_path):
    with pytest.raises(ConfigError, match="知らないモジュール"):
        load_config(env={"KEI_AGENT_HOME": str(_config(tmp_path, 'modules = ["nothing"]\n'))})
    home_dir = _config(tmp_path, 'modules = ["digest"]\n')
    _module(home_dir / "modules", "digest", 'api = 1\nname = "digest"\n[depends]\nrequires = ["knowledge"]\n')
    with pytest.raises(ConfigError, match="knowledge が要ります"):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


def test_a_module_can_only_pick_models_from_the_core_list(tmp_path):
    home_dir = _config(tmp_path, 'modules = ["cheap"]\n')
    _module(home_dir / "modules", "cheap", 'api = 1\nname = "cheap"\n[actor]\nprompt = "cheap.md"\n'
                                           '[use_cases.cheap_answer]\nclaude = { model = "claude-2" }\n')
    with pytest.raises(ConfigError, match="claude のモデル claude-2 は使えません"):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


def _store(config):
    from kei_agent.store import Store
    return Store(config.db_path)
