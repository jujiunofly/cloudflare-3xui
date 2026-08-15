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
    effective_telegram,
    get_policy,
    load_node_state,
    mode_label,
    notify_flag_label,
    set_notify_flag,
    set_policy,
)
from notifier import answer, edit, get_updates, send, setup_bot, telegram_enabled
from panel_client import PanelClient, matching_inbounds

LOGGER = logging.getLogger(__name__)
CMD_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s|$)")

KEYBOARD = {
    "keyboard": [
        [{"text": "节点列表"}, {"text": "通知设置"}],
        [{"text": "运行状态"}],
    ],
    "resize_keyboard": True,
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
        # chat_id -> inbound id waiting for lock IP
        self._pending_lock: dict[str, int] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="telegram-bot", daemon=True)
        self._thread.start()
        LOGGER.info("Telegram bot thread started")

    def stop(self) -> None:
        self._stop.set()

    def _cfg(self) -> dict[str, Any]:
        return load_config(self.config_path)

    def _tg(self) -> dict[str, Any]:
        return self._cfg().get("telegram", {})

    def _token(self) -> str:
        return str(self._tg().get("bot_token", "")).strip()

    def _allowed(self, chat_id: Any) -> bool:
        return str(chat_id).strip() == str(self._tg().get("chat_id", "")).strip()

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

        # Menu always cancels pending lock input.
        if intent and intent != "unknown":
            if intent != "cancel":
                self._pending_lock.pop(key, None)
            self._run_intent(token, chat_id, intent)
            return

        if key in self._pending_lock:
            self._finish_lock(token, chat_id, self._pending_lock[key], text)
            return

        # Ignore free chat; no spam.

    def _intent(self, text: str) -> str | None:
        mapping = {
            "节点列表": "nodes",
            "节点": "nodes",
            "通知设置": "notify",
            "通知": "notify",
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
        if name in {"notify", "settings"}:
            return "notify"
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
                "• 节点列表：查看状态 / 参与更新 / 锁定 IP\n"
                "• 通知设置：成功·失败·开始·休息开关\n"
                "• 运行状态：当前是否在工作时段\n"
                "点下方按钮即可。",
                reply_markup=KEYBOARD,
            )
        elif intent == "nodes":
            self._send_nodes(token, chat_id)
        elif intent == "notify":
            self._send_notify(token, chat_id)
        elif intent == "status":
            text = self.status_provider() if self.status_provider else "暂无状态"
            send(token, chat_id, text, reply_markup=KEYBOARD)
        elif intent == "cancel":
            self._pending_lock.pop(str(chat_id), None)
            send(token, chat_id, "已取消。", reply_markup=KEYBOARD)
        elif intent == "unknown":
            send(
                token,
                chat_id,
                "不支持该命令。可用：/start /nodes /notify /status",
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

    def _nodes_view(self) -> tuple[str, dict[str, Any]]:
        nodes = self._collect_nodes()
        if not nodes:
            return (
                "📭 没有匹配节点\nremark 需包含 cucc / cmcc / ctcc / mix",
                {"inline_keyboard": [[{"text": "刷新", "callback_data": "nodes"}]]},
            )
        lines = ["📋 节点列表", "━━━━━━━━━━━━━━━━"]
        rows: list[list[dict[str, str]]] = []
        for n in nodes:
            lock_hint = f" → {n['locked']}" if n["mode"] == MODE_LOCKED and n.get("locked") else ""
            icon = "✅" if n["enable"] else "⛔"
            lines.append(
                f"{icon} #{n['id']} {n['remark'] or '无备注'}\n"
                f"   {n['line']}｜{n['addr'] or '（空）'}\n"
                f"   {mode_label(n['mode'])}{lock_hint}"
            )
            label = f"#{n['id']} {(n['remark'] or n['line'])[:12]}"
            rows.append([{"text": label[:64], "callback_data": f"node:{n['id']}"}])
        rows.append([{"text": "刷新", "callback_data": "nodes"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def _node_view(self, iid: int) -> tuple[str, dict[str, Any]]:
        node = next((n for n in self._collect_nodes() if n["id"] == iid), None)
        if not node:
            return (
                f"❌ 未找到节点 #{iid}",
                {"inline_keyboard": [[{"text": "返回列表", "callback_data": "nodes"}]]},
            )
        text = (
            f"🎯 节点 #{node['id']}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"备注：{node['remark'] or '无'}\n"
            f"线路：{node['line']}\n"
            f"当前 IP：{node['addr'] or '（空）'}\n"
            f"更新模式：{mode_label(node['mode'])}\n"
            f"锁定地址：{node['locked'] or '—'}\n"
            f"\n选择操作："
        )
        rows: list[list[dict[str, str]]] = []
        if node["mode"] != MODE_AUTO:
            rows.append([{"text": "参与自动更新", "callback_data": f"act:auto:{iid}"}])
        if node["mode"] != MODE_PAUSE:
            rows.append([{"text": "不参与更新", "callback_data": f"act:pause:{iid}"}])
        rows.append([{"text": "锁定为固定 IP", "callback_data": f"act:lock:{iid}"}])
        if node["mode"] == MODE_LOCKED:
            rows.append([{"text": "解除锁定", "callback_data": f"act:unlock:{iid}"}])
        rows.append([{"text": "返回列表", "callback_data": "nodes"}])
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
                    self._pending_lock[str(chat_id)] = iid
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
                tg = effective_telegram(self._cfg(), self.state_path)
                cur = bool(tg.get(key, NOTIFY_DEFAULTS[key]))
                set_notify_flag(self.state_path, key, not cur)
                text, markup = self._notify_view()
                self._show(token, chat_id, mid, text, markup)
        except Exception as exc:
            LOGGER.exception("callback %s", data)
            send(token, chat_id, f"❌ 操作失败：{exc}", reply_markup=KEYBOARD)

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
            self._pending_lock.pop(str(chat_id), None)
            send(token, chat_id, f"✅ #{iid} 已锁定为 {address}", reply_markup=KEYBOARD)
            self._send_nodes(token, chat_id)
        except Exception as exc:
            LOGGER.exception("lock failed")
            send(token, chat_id, f"❌ 锁定失败：{exc}\n可重发 IP 或「取消」")
