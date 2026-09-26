"""Slack から設定を変える操作: 接続先の申し出のボタンと、App Home（docs/architecture.md）。

Assistant に混ぜて使う。self.slack、self.store、self.config、self.submit などは Assistant のもの。
"""

from __future__ import annotations

import logging

from kei_agent import home, settings, themes
from kei_agent.auto_messages import domain_resume_prompt
from kei_agent.config import HHMM
from kei_agent.request import Request
from kei_agent.themes import ChannelKind, Workspace

log = logging.getLogger(__name__)

ALLOW_ACTION = "kei_agent_domain_allow"
DENY_ACTION = "kei_agent_domain_deny"


class SettingsActions:
    # 接続先の申し出

    def new_connect_requests(self, ws: Workspace, text: str,
                             from_tools: list[tuple[str, str]] = ()) -> list[tuple[str, str]]:
        """返答の `🔒 接続:` と、Bash の allowed_domains で頼まれたもののうち、まだ許可していないもの。

        テーマ以外では受け付けない。ボタンで許可できるのは、ぴったりのドメイン名だけ。
        """
        if ws.kind is not ChannelKind.THEME:
            return []
        allowed = set(self.config.allowed_domains) | set(ws.allowed_domains)
        found: dict[str, str] = {}
        for domain, reason in [*settings.parse_connect_requests(text), *from_tools]:
            domain = domain.strip().lower()
            if settings.valid_domain(domain) and domain not in allowed and domain not in found:
                found[domain] = reason
        return list(found.items())

    async def ask_for_domains(self, req: Request, ws: Workspace, requests: list[tuple[str, str]]) -> None:
        for domain, reason in requests:
            req_id = settings.add_request(self.store, req.channel, req.thread_ts, ws.channel_name, domain, reason)
            text = f"🔒 `{domain}` につなぎたいそうです" + (f"。理由: {reason}" if reason else "")
            await self.slack.chat_postMessage(
                channel=req.channel, thread_ts=req.thread_ts, text=text,
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text":
                        f"許可すると、#{ws.channel_name} の作業でだけつながります（チャンネルをアーカイブすると消えます）"}]},
                    {"type": "actions", "elements": [
                        {"type": "button", "action_id": ALLOW_ACTION, "style": "primary",
                         "text": {"type": "plain_text", "text": "許可する"}, "value": str(req_id)},
                        {"type": "button", "action_id": DENY_ACTION,
                         "text": {"type": "plain_text", "text": "断る"}, "value": str(req_id)},
                    ]},
                ],
            )

    async def on_domain_action(self, body: dict) -> None:
        """[許可する] [断る] が押された。押せるのは依頼者だけ。"""
        if not self.is_allowed(body.get("user", {}).get("id")):
            return
        action = (body.get("actions") or [{}])[0]
        allowed = action.get("action_id") == ALLOW_ACTION
        try:
            req_id = int(action.get("value", ""))
        except ValueError:
            return
        if not settings.resolve_request(self.store, req_id, "allowed" if allowed else "denied"):
            return  # もう決まっている（2度押し）
        row = settings.get_request(self.store, req_id)
        if allowed:
            settings.allow_domain(self.store, row["theme"], row["domain"], row["reason"] or "")
        done = f"🔒 `{row['domain']}` への接続を" + ("許可しました" if allowed else "断りました")
        await self.replace_buttons(body, done, row["channel"])
        if settings.pending_requests(self.store, row["channel"], row["thread_ts"]):
            return  # ほかの申し出に答えてから、まとめて再開する
        decisions = settings.take_decisions(self.store, row["channel"], row["thread_ts"])
        if decisions:
            await self.submit(Request(
                channel=row["channel"], channel_name=row["theme"], thread_ts=row["thread_ts"],
                message_ts=None, text=domain_resume_prompt(decisions), trigger="domain",
            ))

    async def replace_buttons(self, body: dict, text: str, fallback_channel: str = "") -> None:
        """押されたボタンのメッセージを、決まった内容の1行に書き換える（2度押しできないようにする）。"""
        container = body.get("container", {})
        try:
            await self.slack.chat_update(
                channel=container.get("channel_id") or fallback_channel, ts=container.get("message_ts"),
                text=text, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
        except Exception:
            log.warning("ボタンのメッセージを書き換えられません", exc_info=True)

    # App Home（設定画面）

    def _theme_names(self) -> list[str]:
        return [p.name for p in themes.theme_dirs(self.config)]

    async def publish_home(self, user_id: str) -> None:
        view = home.build_home(self.config, self.store, self._theme_names(), is_owner=self.is_allowed(user_id))
        await self.slack.views_publish(user_id=user_id, view=view)

    async def on_home_opened(self, event: dict) -> None:
        if event.get("tab", "home") == "home" and event.get("user"):
            await self.publish_home(event["user"])

    async def on_home_action(self, body: dict) -> None:
        """App Home のボタンと時刻の選択。変えられるのは依頼者だけ。"""
        user = body.get("user", {}).get("id")
        if not self.is_allowed(user):
            return
        action = (body.get("actions") or [{}])[0]
        kind, _, name = action.get("action_id", "").partition(":")
        if kind == home.REFRESH_ACTION:
            pass  # 表示を作り直すだけ
        elif kind == home.REMOVE_DOMAIN_ACTION:
            # 選ぶ形（いま）とボタン（前に出した画面）の両方を読む
            value = (action.get("selected_option") or {}).get("value") or action.get("value", "")
            theme, _, domain = value.partition("\t")
            settings.remove_domain(self.store, theme, domain)
        elif kind == home.ADD_DOMAIN_ACTION:
            await self.slack.views_open(trigger_id=body.get("trigger_id"),
                                        view=home.build_add_domain_modal(self._theme_names()))
            return
        elif kind == home.TIME_ACTION and name in settings.schedule_names(self.config):
            # 時刻を消されたら変えずに、表示だけ元の時刻に戻す
            selected = action.get("selected_time") or ""
            if HHMM.match(selected):
                _, enabled = settings.schedule_setting(self.config, self.store, name)
                settings.set_schedule(self.store, name, selected, enabled)
        elif kind == home.SCHEDULES_ACTION:
            # チェックの付いたものだけを動かす（付け外ししたものだけ書き換える）
            chosen = {option.get("value") for option in action.get("selected_options") or []}
            for schedule in settings.schedule_names(self.config):
                hhmm, enabled = settings.schedule_setting(self.config, self.store, schedule)
                if (schedule in chosen) != enabled:
                    settings.set_schedule(self.store, schedule, hhmm, schedule in chosen)
        elif kind == home.VOICE_ACTION:
            chosen = {option.get("value") for option in action.get("selected_options") or []}
            settings.set_voice(self.store, "voice" in chosen)
            listen = "listen" in chosen
            if listen != settings.listening_enabled(self.store):
                settings.set_listening(self.store, listen)
                self.notify_listening(listen)
        elif kind == home.PROVIDER_ACTION and name in home.agent_labels(self.config):
            provider = ((action.get("selected_option") or {}).get("value") or "")
            if provider:
                settings.set_agent_provider(self.store, name, provider)
        else:
            return
        await self.publish_home(user)

    async def on_add_domain(self, body: dict) -> dict | None:
        """「接続先を足す」の送信。入力がおかしければ、欄ごとの説明を返す（モーダルに出す）。"""
        user = body.get("user", {}).get("id")
        if not self.is_allowed(user):
            return {"domain": "依頼者だけが変えられます"}
        theme, domain = home.read_add_domain(body.get("view", {}))
        if theme not in self._theme_names():
            return {"theme": "テーマを選んでください"}
        if not settings.valid_domain(domain, allow_wildcard=True):
            return {"domain": "zenodo.org や *.example.com のように、ドメイン名だけを書いてください"}
        settings.allow_domain(self.store, theme, domain, "App Home から追加")
        await self.publish_home(user)
        return None
