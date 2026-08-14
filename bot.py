"""Simple Telegram control: nodes + notify switches."""
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
    set_notify_flag,
    set_policy,
)
from notifier import answer, edit, get_updates, send, setup_bot, telegram_enabled
from panel_client import PanelClient, matching_inbounds

LOGGER = logging.getLogger(__name__)
CMD_RE = re.compile(r"^/([a-zA-Z0-9_]+)(?:@[A-Za-z0-9_]+)?")

KEYBOARD = {
    "keyboard": [[{"text": "节点列表"}, {"text": "通知设置"}]],
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
        self._pending: dict[str, int] = {}  # chat_id -> inbound_id waiting for lock IP

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="telegram-bot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _cfg(self) -> dict[str, Any]:
        return load_config(self.config_path)

    def _tg(self) -> dict[str, Any]:
        return self._cfg().get("telegram", {})

    def _token(self) -> str:
        return str(self._tg().get("bot_token", "")).strip()

    def _ok_chat(self, chat_id: Any) -> bool:
        return str(chat_id) == str(self._tg().get("chat_id", "")).strip()

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
                    LOGGER.info("Telegram bot ready")
                for upd in get_updates(token, self._offset):
                    self._offset = int(upd["update_id"]) + 1
                    self._handle(token, upd)
            except Exception as exc:
                text = str(exc)
                if "Conflict" in text and "getUpdates" in text:
                    LOGGER.error(
                        "Telegram Conflict：同一 bot token 只能有一个实例在跑。"
                        "请 docker compose down 后只启动一个容器。 %s",
                        text,
                    )
                    time.sleep(30)
                else:
                    LOGGER.warning("bot loop: %s", exc)
                    time.sleep(3)

    def _handle(self, token: str, upd: dict[str, Any]) -> None:
        if "callback_query" in upd:
            self._on_callback(token, upd["callback_query"])
            return
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = str(msg.get("text") or "").strip()
        if chat_id is None or not self._ok_chat(chat_id) or not text:
            return

        # pending lock IP input
        key = str(chat_id)
        if key in self._pending and not text.startswith("/"):
            if text in {"节点列表", "通知设置", "取消"}:
                self._pending.pop(key, None)
            else:
                self._lock_with_ip(token, chat_id, self._pending[key], text)
                return

        intent = self._intent(text)
        if intent == "start":
            send(
                token,
                chat_id,
                "菜单：\n• 节点列表 — 查看/暂停/锁定\n• 通知设置 — 成功失败开始休息开关",
                reply_markup=KEYBOARD,
            )
        elif intent == "nodes":
            self._send_nodes(token, chat_id)
        elif intent == "notify":
            self._send_notify(token, chat_id)
        elif intent == "cancel":
            self._pending.pop(key, None)
            send(token, chat_id, "已取消", reply_markup=KEYBOARD)

    def _intent(self, text: str) -> str | None:
        if text in {"节点列表", "节点"}:
            return "nodes"
        if text in {"通知设置", "通知"}:
            return "notify"
        if text in {"取消"}:
            return "cancel"
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
        if name == "cancel":
            return "cancel"
        return None

    def _panel(self) -> PanelClient:
        return self.panel_factory(self._cfg())

    def _nodes(self) -> list[dict[str, Any]]:
        state = load_node_state(self.state_path)
        out = []
        for inbound, line in matching_inbounds(self._panel().list_inbounds()):
            iid = int(inbound["id"])
            pol = get_policy(state, iid)
            out.append({
                "id": iid,
                "remark": str(inbound.get("remark") or ""),
                "line": line,
                "addr": str(inbound.get("shareAddr") or ""),
                "mode": pol["mode"],
                "locked": pol.get("locked_address"),
            })
        out.sort(key=lambda x: x["id"])
        return out

    def _nodes_view(self) -> tuple[str, dict[str, Any]]:
        nodes = self._nodes()
        if not nodes:
            return "没有匹配节点（remark 需含 cucc/cmcc/ctcc/mix）", {
                "inline_keyboard": [[{"text": "刷新", "callback_data": "n:r"}]]
            }
        lines = ["节点列表"]
        rows = []
        for n in nodes:
            extra = f" 锁={n['locked']}" if n["mode"] == MODE_LOCKED and n["locked"] else ""
            lines.append(f"#{n['id']} {n['remark'] or '-'} | {n['line']} | {n['addr'] or '-'} | {mode_label(n['mode'])}{extra}")
            rows.append([{"text": f"#{n['id']} {n['remark'][:12] or n['line']}", "callback_data": f"n:{n['id']}"}])
        rows.append([{"text": "刷新", "callback_data": "n:r"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def _send_nodes(self, token: str, chat_id: Any) -> None:
        try:
            text, markup = self._nodes_view()
            send(token, chat_id, text, reply_markup=markup)
        except Exception as exc:
            send(token, chat_id, f"读取失败: {exc}", reply_markup=KEYBOARD)

    def _node_view(self, iid: int) -> tuple[str, dict[str, Any]]:
        node = next((n for n in self._nodes() if n["id"] == iid), None)
        if not node:
            return f"找不到 #{iid}", {"inline_keyboard": [[{"text": "返回", "callback_data": "n:r"}]]}
        text = (
            f"节点 #{node['id']}\n"
            f"备注: {node['remark'] or '-'}\n"
            f"线路: {node['line']}\n"
            f"地址: {node['addr'] or '-'}\n"
            f"状态: {mode_label(node['mode'])}\n"
            f"锁定IP: {node['locked'] or '-'}"
        )
        rows = []
        if node["mode"] != MODE_AUTO:
            rows.append([{"text": "参与自动更新", "callback_data": f"a:auto:{iid}"}])
        if node["mode"] != MODE_PAUSE:
            rows.append([{"text": "不参与更新", "callback_data": f"a:pause:{iid}"}])
        rows.append([{"text": "锁定为固定IP", "callback_data": f"a:lock:{iid}"}])
        if node["mode"] == MODE_LOCKED:
            rows.append([{"text": "解除锁定", "callback_data": f"a:unlock:{iid}"}])
        rows.append([{"text": "返回列表", "callback_data": "n:r"}])
        return text, {"inline_keyboard": rows}

    def _notify_view(self) -> tuple[str, dict[str, Any]]:
        tg = effective_telegram(self._cfg(), self.state_path)
        lines = ["通知开关（点一下切换）"]
        rows = []
        for key, label in NOTIFY_LABELS.items():
            on = bool(tg.get(key, NOTIFY_DEFAULTS[key]))
            lines.append(f"{'开' if on else '关'} · {label}")
            rows.append([{
                "text": f"{'关闭' if on else '开启'}{label}",
                "callback_data": f"t:{key}",
            }])
        return "\n".join(lines), {"inline_keyboard": rows}

    def _send_notify(self, token: str, chat_id: Any) -> None:
        text, markup = self._notify_view()
        send(token, chat_id, text, reply_markup=markup)

    def _on_callback(self, token: str, q: dict[str, Any]) -> None:
        data = str(q.get("data") or "")
        cid = str(q.get("id") or "")
        msg = q.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id")
        if chat_id is None or not self._ok_chat(chat_id):
            answer(token, cid, "未授权")
            return
        answer(token, cid)  # ack first

        try:
            if data in {"n:r", "n:list"}:
                text, markup = self._nodes_view()
                edit(token, chat_id, int(mid), text, markup) if mid else send(token, chat_id, text, reply_markup=markup)
                return
            if data.startswith("n:") and data[2:].isdigit():
                text, markup = self._node_view(int(data[2:]))
                edit(token, chat_id, int(mid), text, markup) if mid else send(token, chat_id, text, reply_markup=markup)
                return
            if data.startswith("a:"):
                _, action, sid = data.split(":", 2)
                iid = int(sid)
                if action == "auto":
                    set_policy(self.state_path, iid, mode=MODE_AUTO, locked_address=None)
                elif action == "pause":
                    set_policy(self.state_path, iid, mode=MODE_PAUSE, locked_address=None)
                elif action == "unlock":
                    set_policy(self.state_path, iid, mode=MODE_AUTO, locked_address=None)
                elif action == "lock":
                    self._pending[str(chat_id)] = iid
                    send(token, chat_id, f"发送 #{iid} 要锁定的 IP，或发「取消」", reply_markup=KEYBOARD)
                    return
                text, markup = self._node_view(iid)
                edit(token, chat_id, int(mid), text, markup) if mid else send(token, chat_id, text, reply_markup=markup)
                return
            if data.startswith("t:"):
                key = data[2:]
                if key not in NOTIFY_DEFAULTS:
                    return
                tg = effective_telegram(self._cfg(), self.state_path)
                cur = bool(tg.get(key, NOTIFY_DEFAULTS[key]))
                set_notify_flag(self.state_path, key, not cur)
                text, markup = self._notify_view()
                edit(token, chat_id, int(mid), text, markup) if mid else send(token, chat_id, text, reply_markup=markup)
        except Exception as exc:
            LOGGER.exception("callback failed")
            send(token, chat_id, f"失败: {exc}", reply_markup=KEYBOARD)

    def _lock_with_ip(self, token: str, chat_id: Any, iid: int, address: str) -> None:
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
            send(token, chat_id, f"#{iid} 已锁定 {address}", reply_markup=KEYBOARD)
            self._send_nodes(token, chat_id)
        except Exception as exc:
            send(token, chat_id, f"锁定失败: {exc}\n可重发 IP 或「取消」")
