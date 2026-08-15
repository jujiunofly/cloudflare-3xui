"""Telegram interactive controls: nodes + notify switches."""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from api_client import load_config
from node_state import (
    MODE_AUTO,
    MODE_LOCKED,
    MODE_PAUSE,
    NOTIFY_DEFAULTS,
    NOTIFY_LABELS,
    clear_schedule_runtime_overrides,
    effective_config,
    effective_telegram,
    get_policy,
    load_node_state,
    mode_label,
    notify_flag_label,
    parse_hhmm,
    set_notify_flag,
    set_policy,
    set_runtime_override,
    set_schedule_override,
)
from notifier import answer, edit, get_updates, send, setup_bot, telegram_enabled
from panel_client import PanelClient, matching_inbounds

LOGGER = logging.getLogger(__name__)
CMD_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s|$)")

KEYBOARD = {
    "keyboard": [
        [{"text": "节点列表"}, {"text": "通知设置"}],
        [{"text": "运行设置"}, {"text": "运行状态"}],
    ],
    "resize_keyboard": True,
}

PENDING_PROMPTS = {
    "start": "请发送开始时间，格式 HH:MM\n例如：08:00",
    "end": "请发送结束时间，格式 HH:MM\n例如：23:30",
    "interval": "请发送同步间隔（分钟）\n例如：10   范围 1～1440",
    "jitter": "请发送随机抖动（秒）\n例如：45   范围 0～3600",
}


class TelegramBot:
    def __init__(
        self,
        config_path: Path,
        state_path: Path,
        panel_factory: Callable[[dict[str, Any]], PanelClient],
        status_provider: Callable[[], str] | None = None,
    ) -> None:
        self.config_path = config_path
        self.state_path = state_path
        self.panel_factory = panel_factory
        self.status_provider = status_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset: int | None = None
        self._ready = False
        # chat_id -> {"kind": "lock"|"setting", ...}
        self._pending: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="telegram-bot", daemon=True)
        self._thread.start()
        LOGGER.info("Telegram bot thread started")

    def stop(self) -> None:
        self._stop.set()

    def _raw_cfg(self) -> dict[str, Any]:
        return load_config(self.config_path)

    def _cfg(self) -> dict[str, Any]:
        return effective_config(self._raw_cfg(), self.state_path)

    def _tg(self) -> dict[str, Any]:
        return effective_telegram(self._raw_cfg(), self.state_path)

    def _token(self) -> str:
        return str(self._raw_cfg().get("telegram", {}).get("bot_token", "")).strip()

    def _allowed(self, chat_id: Any) -> bool:
        expected = str(self._raw_cfg().get("telegram", {}).get("chat_id", "")).strip()
        return str(chat_id).strip() == expected

    def _panel(self) -> PanelClient:
        return self.panel_factory(self._cfg())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                tg = self._tg()
                if not telegram_enabled(tg):
                    time.sleep(5)
                    continue
                token = self._token()
                if not self._ready:
                    setup_bot(token)
                    self._ready = True
                    LOGGER.info("Telegram bot ready (long polling)")
                for upd in get_updates(token, self._offset):
                    self._offset = int(upd["update_id"]) + 1
                    try:
                        self._dispatch(token, upd)
                    except Exception:
                        LOGGER.exception("handle update failed")
            except Exception as exc:
                msg = str(exc)
                if "Conflict" in msg and "getUpdates" in msg:
                    LOGGER.error(
                        "Telegram Conflict：同一个 bot token 只能有一个进程轮询。"
                        "请 docker compose down 后只启动一个容器。%s",
                        msg,
                    )
                    time.sleep(30)
                else:
                    LOGGER.warning("bot loop: %s", msg)
                    time.sleep(3)

    def _dispatch(self, token: str, upd: dict[str, Any]) -> None:
        if "callback_query" in upd:
            self._on_callback(token, upd["callback_query"])
            return
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = str(msg.get("text") or "").strip()
        if chat_id is None or not self._allowed(chat_id) or not text:
            return

        key = str(chat_id)
        intent = self._intent(text)

        # Menu commands cancel pending free-text input (except cancel itself).
        if intent and intent != "unknown":
            if intent != "cancel":
                self._pending.pop(key, None)
            self._run_intent(token, chat_id, intent)
            return

        pending = self._pending.get(key)
        if pending:
            self._finish_pending(token, chat_id, pending, text)
            return

        # Ignore free chat; no spam.

    def _intent(self, text: str) -> str | None:
        mapping = {
            "节点列表": "nodes",
            "节点": "nodes",
            "通知设置": "notify",
            "通知": "notify",
            "运行设置": "schedule",
            "计划设置": "schedule",
            "运行状态": "status",
            "状态": "status",
            "菜单": "start",
            "取消": "cancel",
        }
        if text in mapping:
            return mapping[text]
        m = CMD_RE.match(text)
        if not m:
            return None
        name = m.group(1).lower()
        if name in {"start", "help", "menu"}:
            return "start"
        if name in {"nodes", "node", "list"}:
            return "nodes"
        if name in {"notify"}:
            return "notify"
        if name in {"schedule", "runtime", "settings"}:
            return "schedule"
        if name in {"status"}:
            return "status"
        if name == "cancel":
            return "cancel"
        return "unknown"

    def _run_intent(self, token: str, chat_id: Any, intent: str) -> None:
        if intent == "start":
            send(
                token,
                chat_id,
                "👋 Cloudflare → 3x-ui 控制台\n"
                "━━━━━━━━━━━━━━━━\n"
                "• 节点列表：状态 / 参与更新 / 锁定 IP\n"
                "• 通知设置：成功·失败·开始·休息\n"
                "• 运行设置：间隔 / 开始·结束时间\n"
                "• 运行状态：当前是否在工作时段\n"
                "点下方按钮即可。",
                reply_markup=KEYBOARD,
            )
        elif intent == "nodes":
            self._send_nodes(token, chat_id)
        elif intent == "notify":
            self._send_notify(token, chat_id)
        elif intent == "schedule":
            self._send_schedule(token, chat_id)
        elif intent == "status":
            text = self.status_provider() if self.status_provider else "暂无状态"
            send(token, chat_id, text, reply_markup=KEYBOARD)
        elif intent == "cancel":
            self._pending.pop(str(chat_id), None)
            send(token, chat_id, "已取消。", reply_markup=KEYBOARD)
        elif intent == "unknown":
            send(
                token,
                chat_id,
                "不支持该命令。可用：/start /nodes /notify /schedule /status",
                reply_markup=KEYBOARD,
            )

    def _collect_nodes(self) -> list[dict[str, Any]]:
        state = load_node_state(self.state_path)
        nodes: list[dict[str, Any]] = []
        for inbound, line in matching_inbounds(self._panel().list_inbounds()):
            iid = int(inbound["id"])
            pol = get_policy(state, iid)
            nodes.append({
                "id": iid,
                "remark": str(inbound.get("remark") or ""),
                "line": line,
                "addr": str(inbound.get("shareAddr") or ""),
                "mode": pol["mode"],
                "locked": pol.get("locked_address"),
                "enable": bool(inbound.get("enable", True)),
            })
        nodes.sort(key=lambda n: n["id"])
        return nodes

    @staticmethod
    def _mode_badge(mode: str) -> str:
        return {
            MODE_AUTO: "自动",
            MODE_PAUSE: "暂停",
            MODE_LOCKED: "锁定",
        }.get(mode, mode)

    @staticmethod
    def _short_remark(remark: str, limit: int = 18) -> str:
        text = (remark or "无备注").strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _nodes_view(self) -> tuple[str, dict[str, Any]]:
        nodes = self._collect_nodes()
        if not nodes:
            return (
                "📭 没有匹配节点\nremark 需包含 cucc / cmcc / ctcc / mix",
                {"inline_keyboard": [[{"text": "刷新", "callback_data": "nodes"}]]},
            )

        auto_n = sum(1 for n in nodes if n["mode"] == MODE_AUTO)
        pause_n = sum(1 for n in nodes if n["mode"] == MODE_PAUSE)
        lock_n = sum(1 for n in nodes if n["mode"] == MODE_LOCKED)

        # Compact, scannable cards — one visual block per node.
        lines = [
            "📋  节点一览",
            f"共 {len(nodes)} 条  ·  自动 {auto_n}  ·  暂停 {pause_n}  ·  锁定 {lock_n}",
            "────────────────",
        ]
        for n in nodes:
            status = self._mode_badge(n["mode"])
            if n["mode"] == MODE_LOCKED and n.get("locked"):
                status = f"锁定 {n['locked']}"
            elif n["mode"] == MODE_PAUSE:
                status = "暂停"
            if not n["enable"]:
                status = f"面板关闭 · {status}"
            addr = n["addr"] or "—"
            remark = self._short_remark(n["remark"] or "", 20)
            lines.append(f"#{n['id']:<3} {remark}")
            lines.append(f"     {n['line']}  ·  {addr}  ·  {status}")
            lines.append("")

        while lines and lines[-1] == "":
            lines.pop()
        lines.append("────────────────")
        lines.append("👇 点下面按钮管理对应节点")

        # Two buttons per row.
        rows: list[list[dict[str, str]]] = []
        pair: list[dict[str, str]] = []
        for n in nodes:
            raw = re.sub(r"[\U0001F1E6-\U0001F1FF]+", "", n["remark"] or "")
            raw = re.sub(r"\s+", "", raw) or n["line"]
            label = f"#{n['id']} {raw[:10]}"
            if n["mode"] == MODE_PAUSE:
                label = f"⏸{label}"
            elif n["mode"] == MODE_LOCKED:
                label = f"🔒{label}"
            pair.append({"text": label[:64], "callback_data": f"node:{n['id']}"})
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        rows.append([{"text": "🔄 刷新列表", "callback_data": "nodes"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def _node_view(self, iid: int) -> tuple[str, dict[str, Any]]:
        node = next((n for n in self._collect_nodes() if n["id"] == iid), None)
        if not node:
            return (
                f"❌ 未找到节点 #{iid}",
                {"inline_keyboard": [[{"text": "« 返回列表", "callback_data": "nodes"}]]},
            )
        mode = mode_label(node["mode"])
        if node["mode"] == MODE_LOCKED and node.get("locked"):
            mode = f"{mode}\n锁定 IP：{node['locked']}"
        text = (
            f"🎯  节点 #{node['id']}\n"
            f"────────────────\n"
            f"名称    {node['remark'] or '无'}\n"
            f"线路    {node['line']}\n"
            f"地址    {node['addr'] or '—'}\n"
            f"状态    {mode}\n"
            f"面板    {'启用' if node['enable'] else '禁用'}\n"
            f"────────────────\n"
            f"选择操作 ↓"
        )
        rows: list[list[dict[str, str]]] = []
        if node["mode"] != MODE_AUTO:
            rows.append([{"text": "✅ 参与自动更新", "callback_data": f"act:auto:{iid}"}])
        if node["mode"] != MODE_PAUSE:
            rows.append([{"text": "⏸ 不参与更新", "callback_data": f"act:pause:{iid}"}])
        rows.append([{"text": "🔒 锁定为固定 IP", "callback_data": f"act:lock:{iid}"}])
        if node["mode"] == MODE_LOCKED:
            rows.append([{"text": "🔓 解除锁定", "callback_data": f"act:unlock:{iid}"}])
        rows.append([{"text": "« 返回列表", "callback_data": "nodes"}])
        return text, {"inline_keyboard": rows}

    def _notify_view(self) -> tuple[str, dict[str, Any]]:
        tg = effective_telegram(self._cfg(), self.state_path)
        lines = [
            "🔔 通知设置",
            "━━━━━━━━━━━━━━━━",
            "点按钮切换（写入 node_state.json）：",
            "",
        ]
        rows: list[list[dict[str, str]]] = []
        for key, label in NOTIFY_LABELS.items():
            on = bool(tg.get(key, NOTIFY_DEFAULTS[key]))
            lines.append(notify_flag_label(key, on))
            rows.append([{
                "text": f"{'关闭' if on else '开启'}{label}",
                "callback_data": f"tg:{key}",
            }])
        rows.append([{"text": "刷新", "callback_data": "notify"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def _send_nodes(self, token: str, chat_id: Any) -> None:
        try:
            text, markup = self._nodes_view()
            send(token, chat_id, text, reply_markup=markup)
        except Exception as exc:
            LOGGER.exception("list nodes")
            send(token, chat_id, f"❌ 读取节点失败：{exc}", reply_markup=KEYBOARD)

    def _send_notify(self, token: str, chat_id: Any) -> None:
        text, markup = self._notify_view()
        send(token, chat_id, text, reply_markup=markup)

    def _schedule_view(self) -> tuple[str, dict[str, Any]]:
        cfg = self._cfg()
        base = self._raw_cfg()
        state = load_node_state(self.state_path)
        schedule = cfg.get("schedule", {})
        runtime = cfg.get("runtime", {})
        enabled = bool(schedule.get("enabled", False))
        start = schedule.get("start", "08:00")
        end = schedule.get("end", "23:30")
        interval = runtime.get("interval_minutes", 10)
        jitter = runtime.get("jitter_seconds", 45)
        ov_s = state.get("schedule") or {}
        ov_r = state.get("runtime") or {}
        tag = "（含机器人覆盖）" if ov_s or ov_r else "（config 默认）"
        text = (
            f"⚙️  运行设置 {tag}\n"
            f"────────────────\n"
            f"时间窗    {'开启' if enabled else '关闭（全天跑）'}\n"
            f"开始      {start}\n"
            f"结束      {end}\n"
            f"同步间隔  {interval} 分钟\n"
            f"随机抖动  {jitter} 秒\n"
            f"────────────────\n"
            f"config 原始：窗={'开' if base.get('schedule', {}).get('enabled') else '关'} "
            f"{base.get('schedule', {}).get('start', '?')}→{base.get('schedule', {}).get('end', '?')}  "
            f"间隔 {base.get('runtime', {}).get('interval_minutes', '?')}m\n"
            f"点按钮修改 ↓"
        )
        rows = [
            [{"text": f"{'关闭' if enabled else '开启'}时间窗", "callback_data": "sch:toggle"}],
            [
                {"text": f"开始 {start}", "callback_data": "sch:ask:start"},
                {"text": f"结束 {end}", "callback_data": "sch:ask:end"},
            ],
            [
                {"text": f"间隔 {interval}m", "callback_data": "sch:ask:interval"},
                {"text": f"抖动 {jitter}s", "callback_data": "sch:ask:jitter"},
            ],
            [
                {"text": "10分钟", "callback_data": "sch:set:interval:10"},
                {"text": "15分钟", "callback_data": "sch:set:interval:15"},
                {"text": "30分钟", "callback_data": "sch:set:interval:30"},
            ],
            [{"text": "恢复 config 默认", "callback_data": "sch:reset"}],
            [{"text": "🔄 刷新", "callback_data": "schedule"}],
        ]
        return text, {"inline_keyboard": rows}

    def _send_schedule(self, token: str, chat_id: Any) -> None:
        text, markup = self._schedule_view()
        send(token, chat_id, text, reply_markup=markup)

    def _show(self, token: str, chat_id: Any, mid: int | None, text: str, markup: dict[str, Any]) -> None:
        if mid is not None:
            edit(token, chat_id, int(mid), text, markup)
        else:
            send(token, chat_id, text, reply_markup=markup)

    def _on_callback(self, token: str, q: dict[str, Any]) -> None:
        data = str(q.get("data") or "")
        callback_id = str(q.get("id") or "")
        msg = q.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id")
        if chat_id is None or not self._allowed(chat_id):
            answer(token, callback_id, "未授权")
            return

        # Acknowledge immediately — panel API can be slow.
        answer(token, callback_id)

        try:
            if data == "nodes":
                text, markup = self._nodes_view()
                self._show(token, chat_id, mid, text, markup)
                return
            if data == "notify":
                text, markup = self._notify_view()
                self._show(token, chat_id, mid, text, markup)
                return
            if data == "schedule":
                text, markup = self._schedule_view()
                self._show(token, chat_id, mid, text, markup)
                return
            if data.startswith("node:"):
                iid = int(data.split(":", 1)[1])
                text, markup = self._node_view(iid)
                self._show(token, chat_id, mid, text, markup)
                return
            if data.startswith("act:"):
                _, action, sid = data.split(":", 2)
                iid = int(sid)
                if action == "auto":
                    set_policy(self.state_path, iid, mode=MODE_AUTO, locked_address=None)
                elif action == "pause":
                    set_policy(self.state_path, iid, mode=MODE_PAUSE, locked_address=None)
                elif action == "unlock":
                    set_policy(self.state_path, iid, mode=MODE_AUTO, locked_address=None)
                elif action == "lock":
                    self._pending[str(chat_id)] = {"kind": "lock", "inbound_id": iid}
                    send(
                        token,
                        chat_id,
                        f"🔒 请发送节点 #{iid} 要锁定的 IP/域名\n"
                        "发送后立刻写入 3x-ui，并不再自动更新。\n"
                        "发「取消」或 /cancel 取消。",
                        reply_markup=KEYBOARD,
                    )
                    return
                text, markup = self._node_view(iid)
                self._show(token, chat_id, mid, text, markup)
                return
            if data.startswith("tg:"):
                key = data[3:]
                if key not in NOTIFY_DEFAULTS:
                    return
                tg = effective_telegram(self._raw_cfg(), self.state_path)
                cur = bool(tg.get(key, NOTIFY_DEFAULTS[key]))
                set_notify_flag(self.state_path, key, not cur)
                text, markup = self._notify_view()
                self._show(token, chat_id, mid, text, markup)
                return
            if data.startswith("sch:"):
                self._on_schedule_callback(token, chat_id, mid, data)
        except Exception as exc:
            LOGGER.exception("callback %s", data)
            send(token, chat_id, f"❌ 操作失败：{exc}", reply_markup=KEYBOARD)

    def _on_schedule_callback(self, token: str, chat_id: Any, mid: int | None, data: str) -> None:
        if data == "sch:toggle":
            cfg = self._cfg()
            cur = bool(cfg.get("schedule", {}).get("enabled", False))
            set_schedule_override(self.state_path, enabled=not cur)
        elif data == "sch:reset":
            clear_schedule_runtime_overrides(self.state_path)
            send(token, chat_id, "✅ 已恢复为 config.json 中的默认运行参数", reply_markup=KEYBOARD)
        elif data.startswith("sch:ask:"):
            field = data.split(":", 2)[2]
            if field not in PENDING_PROMPTS:
                return
            self._pending[str(chat_id)] = {"kind": "setting", "field": field}
            send(token, chat_id, PENDING_PROMPTS[field] + "\n发「取消」可放弃。", reply_markup=KEYBOARD)
            return
        elif data.startswith("sch:set:interval:"):
            minutes = float(data.rsplit(":", 1)[1])
            set_runtime_override(self.state_path, interval_minutes=minutes)
        text, markup = self._schedule_view()
        self._show(token, chat_id, mid, text, markup)

    def _finish_pending(self, token: str, chat_id: Any, pending: dict[str, Any], text: str) -> None:
        kind = pending.get("kind")
        if kind == "lock":
            self._finish_lock(token, chat_id, int(pending["inbound_id"]), text)
            return
        if kind == "setting":
            self._finish_setting(token, chat_id, str(pending.get("field")), text)
            return
        self._pending.pop(str(chat_id), None)

    def _finish_setting(self, token: str, chat_id: Any, field: str, text: str) -> None:
        try:
            if field == "start":
                set_schedule_override(self.state_path, start=parse_hhmm(text))
            elif field == "end":
                set_schedule_override(self.state_path, end=parse_hhmm(text))
            elif field == "interval":
                set_runtime_override(self.state_path, interval_minutes=float(text.strip()))
            elif field == "jitter":
                set_runtime_override(self.state_path, jitter_seconds=float(text.strip()))
            else:
                raise ValueError(f"未知字段 {field}")
            self._pending.pop(str(chat_id), None)
            send(token, chat_id, "✅ 已保存", reply_markup=KEYBOARD)
            self._send_schedule(token, chat_id)
        except Exception as exc:
            send(token, chat_id, f"❌ {exc}\n请重发，或「取消」")

    def _finish_lock(self, token: str, chat_id: Any, iid: int, address: str) -> None:
        address = address.strip()
        if not address or len(address) > 253:
            send(token, chat_id, "地址无效，请重发或「取消」")
            return
        try:
            client = self._panel()
            inbound = client.get_inbound(iid)
            client.update_share_address(inbound, address)
            set_policy(self.state_path, iid, mode=MODE_LOCKED, locked_address=address)
            self._pending.pop(str(chat_id), None)
            send(token, chat_id, f"✅ #{iid} 已锁定为 {address}", reply_markup=KEYBOARD)
            self._send_nodes(token, chat_id)
        except Exception as exc:
            LOGGER.exception("lock failed")
            send(token, chat_id, f"❌ 锁定失败：{exc}\n可重发 IP 或「取消」")
