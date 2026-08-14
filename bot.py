"""Interactive Telegram bot for node list / pause / lock / unlock."""
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
from notifier import (
    answer_callback,
    edit_telegram,
    get_updates,
    send_telegram,
    set_bot_commands,
    telegram_enabled,
)
from panel_client import PanelClient, PanelError, matching_inbounds

LOGGER = logging.getLogger(__name__)
IP_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$"
    r"|^(?:[A-Fa-f0-9:]+:+)+[A-Fa-f0-9]+$"
)
# /start@MyBot extra args → start
COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s|$)")

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "节点列表"}, {"text": "运行状态"}],
        [{"text": "通知设置"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

BOT_COMMANDS = [
    {"command": "start", "description": "打开菜单"},
    {"command": "nodes", "description": "查看节点列表"},
    {"command": "status", "description": "查看运行状态"},
    {"command": "notify", "description": "通知开关设置"},
    {"command": "cancel", "description": "取消当前输入"},
]

# Exact keyboard / alias text → intent
MENU_INTENTS: dict[str, str] = {
    "节点列表": "nodes",
    "节点": "nodes",
    "运行状态": "status",
    "状态": "status",
    "通知设置": "notify",
    "通知": "notify",
    "菜单": "start",
    "start": "start",
    "nodes": "nodes",
    "status": "status",
    "notify": "notify",
    "cancel": "cancel",
    "取消": "cancel",
}


def resolve_intent(text: str) -> str | None:
    """Map user text to a known intent; None means ignore (not 'unknown command')."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw in MENU_INTENTS:
        return MENU_INTENTS[raw]
    lowered = raw.lower()
    if lowered in MENU_INTENTS:
        return MENU_INTENTS[lowered]
    match = COMMAND_RE.match(raw)
    if match:
        name = match.group(1).lower()
        if name in {"start", "nodes", "status", "notify", "cancel", "help", "menu"}:
            return "start" if name in {"help", "menu"} else name
        # Slash command we do not implement — caller may soft-reply once.
        return f"unknown:{name}"
    return None


class TelegramBot:
    def __init__(
        self,
        config_path: Path,
        node_state_path: Path,
        panel_client_factory: Callable[[dict[str, Any]], PanelClient],
        status_provider: Callable[[], str] | None = None,
    ) -> None:
        self.config_path = config_path
        self.node_state_path = node_state_path
        self.panel_client_factory = panel_client_factory
        self.status_provider = status_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset: int | None = None
        self._commands_registered = False
        # chat_id -> {"action": "lock", "inbound_id": int}
        self._pending: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-bot", daemon=True)
        self._thread.start()
        LOGGER.info("Telegram bot thread started")

    def stop(self) -> None:
        self._stop.set()

    def _cfg(self) -> dict[str, Any]:
        return load_config(self.config_path)

    def _tg(self) -> dict[str, Any]:
        return self._cfg().get("telegram", {})

    def _authorized(self, chat_id: str | int) -> bool:
        expected = str(self._tg().get("chat_id", "")).strip()
        return bool(expected) and str(chat_id) == expected

    def _token(self) -> str:
        return str(self._tg().get("bot_token", "")).strip()

    def _ensure_commands(self, token: str) -> None:
        if self._commands_registered:
            return
        set_bot_commands(token, BOT_COMMANDS)
        self._commands_registered = True
        LOGGER.info("Telegram bot commands registered")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                tg = self._tg()
                if not telegram_enabled(tg):
                    time.sleep(5)
                    continue
                token = self._token()
                self._ensure_commands(token)
                updates = get_updates(token, self._offset, timeout=25, request_timeout=35)
                for update in updates:
                    update_id = int(update.get("update_id", 0))
                    self._offset = update_id + 1
                    try:
                        self._handle_update(token, update)
                    except Exception:
                        LOGGER.exception("Failed to handle Telegram update %s", update_id)
            except Exception as exc:
                LOGGER.warning("Telegram bot loop error: %s", exc)
                time.sleep(3)

    def _handle_update(self, token: str, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(token, update["callback_query"])
            return
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        # Ignore service messages, stickers, photos without caption, etc.
        if message.get("from", {}).get("is_bot"):
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or not self._authorized(chat_id):
            return
        text = str(message.get("text") or message.get("caption") or "").strip()
        if not text:
            return

        intent = resolve_intent(text)
        pending = self._pending.get(str(chat_id))

        # Menu / slash commands always win over pending lock input.
        if intent and not intent.startswith("unknown:"):
            if pending and intent != "cancel":
                self._pending.pop(str(chat_id), None)
            self._dispatch_intent(token, chat_id, intent)
            return

        if pending:
            self._handle_pending_text(token, chat_id, text, pending)
            return

        if intent and intent.startswith("unknown:"):
            # Only reply for real /slash junk — do not spam on free text.
            send_telegram(
                token,
                chat_id,
                "不支持该命令。可用：/start /nodes /status /notify，或点下方按钮。",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        # Free-form chat: ignore silently (no more “未知指令”刷屏).
        LOGGER.debug("Ignoring non-command text from %s: %s", chat_id, text[:80])

    def _dispatch_intent(self, token: str, chat_id: str | int, intent: str) -> None:
        if intent == "start":
            self._send_welcome(token, chat_id)
        elif intent == "nodes":
            self._send_node_list(token, chat_id)
        elif intent == "status":
            self._send_status(token, chat_id)
        elif intent == "notify":
            self._send_notify_settings(token, chat_id)
        elif intent == "cancel":
            self._pending.pop(str(chat_id), None)
            send_telegram(token, chat_id, "已取消。", reply_markup=MAIN_KEYBOARD)

    def _send_welcome(self, token: str, chat_id: str | int) -> None:
        send_telegram(
            token,
            chat_id,
            "👋 Cloudflare → 3x-ui 控制台已就绪\n"
            "• 节点列表：查看当前节点与更新状态\n"
            "• 可暂停参与、锁定固定 IP、解除锁定\n"
            "• 通知设置：成功/失败/开始/休息消息开关\n"
            "• 运行状态：查看休息/工作窗口\n\n"
            "请用下方按钮或菜单命令操作。",
            reply_markup=MAIN_KEYBOARD,
        )

    def _send_status(self, token: str, chat_id: str | int) -> None:
        if self.status_provider:
            text = self.status_provider()
        else:
            text = "暂无状态信息。"
        send_telegram(token, chat_id, text, reply_markup=MAIN_KEYBOARD)

    def _effective_tg(self) -> dict[str, Any]:
        return effective_telegram(self._cfg(), self.node_state_path)

    def _notify_text_and_markup(self) -> tuple[str, dict[str, Any]]:
        tg = self._effective_tg()
        lines = [
            "🔔 通知设置",
            "━━━━━━━━━━━━━━━━",
            "点按钮可即时开关（写入 node_state.json，无需改 config）：",
            "",
        ]
        rows: list[list[dict[str, str]]] = []
        for key in NOTIFY_DEFAULTS:
            enabled = bool(tg.get(key, NOTIFY_DEFAULTS[key]))
            lines.append(notify_flag_label(key, enabled))
            action = "关" if enabled else "开"
            label = NOTIFY_LABELS[key]
            rows.append([{
                "text": f"{'🔕' if enabled else '🔔'} {action} {label}",
                "callback_data": f"notify:toggle:{key}",
            }])
        rows.append([{"text": "🔄 刷新", "callback_data": "notify:refresh"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def _send_notify_settings(self, token: str, chat_id: str | int) -> None:
        text, markup = self._notify_text_and_markup()
        send_telegram(token, chat_id, text, reply_markup=markup)

    def _panel(self) -> PanelClient:
        return self.panel_client_factory(self._cfg())

    def _collect_nodes(self) -> list[dict[str, Any]]:
        client = self._panel()
        state = load_node_state(self.node_state_path)
        nodes: list[dict[str, Any]] = []
        for inbound, line in matching_inbounds(client.list_inbounds()):
            inbound_id = int(inbound["id"])
            policy = get_policy(state, inbound_id)
            nodes.append({
                "id": inbound_id,
                "remark": str(inbound.get("remark") or ""),
                "line": line,
                "share_addr": str(inbound.get("shareAddr") or ""),
                "strategy": str(inbound.get("shareAddrStrategy") or ""),
                "enable": bool(inbound.get("enable", True)),
                "mode": policy["mode"],
                "locked_address": policy.get("locked_address"),
            })
        nodes.sort(key=lambda item: item["id"])
        return nodes

    def _list_text_and_markup(self) -> tuple[str, dict[str, Any]]:
        nodes = self._collect_nodes()
        if not nodes:
            return (
                "📭 没有找到 remark 含 cucc / cmcc / ctcc / mix 的入站。",
                {"inline_keyboard": [[{"text": "🔄 刷新", "callback_data": "nodes:refresh"}]]},
            )
        lines = ["📋 当前节点列表", "━━━━━━━━━━━━━━━━"]
        keyboard: list[list[dict[str, str]]] = []
        for node in nodes:
            status_icon = "✅" if node["enable"] else "⛔"
            addr = node["share_addr"] or "（空）"
            locked_hint = f" → {node['locked_address']}" if node["mode"] == MODE_LOCKED and node.get("locked_address") else ""
            lines.append(
                f"{status_icon} #{node['id']} {node['remark'] or '无备注'}\n"
                f"   线路 {node['line']}｜地址 {addr}\n"
                f"   {mode_label(node['mode'])}{locked_hint}"
            )
            keyboard.append([{
                "text": f"#{node['id']} {node['remark'][:16] or node['line']}",
                "callback_data": f"node:{node['id']}",
            }])
        keyboard.append([{"text": "🔄 刷新", "callback_data": "nodes:refresh"}])
        return "\n".join(lines), {"inline_keyboard": keyboard}

    def _send_node_list(self, token: str, chat_id: str | int) -> None:
        try:
            text, markup = self._list_text_and_markup()
        except Exception as exc:
            LOGGER.exception("List nodes failed")
            send_telegram(token, chat_id, f"❌ 读取节点失败：{exc}", reply_markup=MAIN_KEYBOARD)
            return
        send_telegram(token, chat_id, text, reply_markup=markup)

    def _node_detail(self, inbound_id: int) -> tuple[str, dict[str, Any]]:
        nodes = {node["id"]: node for node in self._collect_nodes()}
        node = nodes.get(inbound_id)
        if not node:
            return (
                f"❌ 未找到节点 #{inbound_id}（可能已删除或 remark 无线路关键字）。",
                {"inline_keyboard": [[{"text": "« 返回列表", "callback_data": "nodes:refresh"}]]},
            )
        addr = node["share_addr"] or "（空）"
        locked = node.get("locked_address") or "—"
        panel_state = "启用" if node["enable"] else "禁用"
        text = (
            f"🎯 节点 #{node['id']}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"备注：{node['remark'] or '无'}\n"
            f"线路：{node['line']}\n"
            f"面板状态：{panel_state}\n"
            f"当前 shareAddr：{addr}\n"
            f"策略：{node['strategy'] or '—'}\n"
            f"更新模式：{mode_label(node['mode'])}\n"
            f"锁定地址：{locked}\n"
            f"\n选择操作："
        )
        rows: list[list[dict[str, str]]] = []
        if node["mode"] != MODE_AUTO:
            rows.append([{"text": "🔄 恢复自动更新", "callback_data": f"act:auto:{inbound_id}"}])
        if node["mode"] != MODE_PAUSE:
            rows.append([{"text": "⏸ 暂停自动更新", "callback_data": f"act:pause:{inbound_id}"}])
        rows.append([{"text": "🔒 锁定为固定 IP", "callback_data": f"act:lockask:{inbound_id}"}])
        if node["mode"] == MODE_LOCKED:
            rows.append([{"text": "🔓 解除锁定", "callback_data": f"act:unlock:{inbound_id}"}])
        rows.append([{"text": "« 返回列表", "callback_data": "nodes:refresh"}])
        return text, {"inline_keyboard": rows}

    def _safe_edit(
        self,
        token: str,
        chat_id: str | int,
        message_id: int | None,
        text: str,
        markup: dict[str, Any] | None = None,
    ) -> None:
        if message_id is None:
            send_telegram(token, chat_id, text, reply_markup=markup)
            return
        try:
            edit_telegram(token, chat_id, int(message_id), text, reply_markup=markup)
        except Exception as exc:
            LOGGER.warning("editMessage failed, fallback send: %s", exc)
            send_telegram(token, chat_id, text, reply_markup=markup)

    def _handle_callback(self, token: str, query: dict[str, Any]) -> None:
        data = str(query.get("data") or "")
        callback_id = str(query.get("id") or "")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if chat_id is None or not self._authorized(chat_id):
            if callback_id:
                answer_callback(token, callback_id, "未授权")
            return
        try:
            if data == "nodes:refresh":
                text, markup = self._list_text_and_markup()
                self._safe_edit(token, chat_id, message_id, text, markup)
                answer_callback(token, callback_id, "已刷新")
                return

            if data.startswith("node:"):
                inbound_id = int(data.split(":", 1)[1])
                text, markup = self._node_detail(inbound_id)
                self._safe_edit(token, chat_id, message_id, text, markup)
                answer_callback(token, callback_id)
                return

            if data.startswith("act:"):
                parts = data.split(":")
                if len(parts) != 3:
                    answer_callback(token, callback_id, "无效操作")
                    return
                _, action, id_text = parts
                inbound_id = int(id_text)
                note = self._apply_action(token, chat_id, action, inbound_id)
                if action != "lockask":
                    text, markup = self._node_detail(inbound_id)
                    self._safe_edit(token, chat_id, message_id, text, markup)
                answer_callback(token, callback_id, note)
                return

            if data == "notify:refresh" or data.startswith("notify:toggle:"):
                if data.startswith("notify:toggle:"):
                    key = data.split(":", 2)[2]
                    if key not in NOTIFY_DEFAULTS:
                        answer_callback(token, callback_id, "未知开关")
                        return
                    current = bool(self._effective_tg().get(key, NOTIFY_DEFAULTS[key]))
                    set_notify_flag(self.node_state_path, key, not current)
                    note = f"{NOTIFY_LABELS[key]} 已{'关闭' if current else '开启'}"
                else:
                    note = "已刷新"
                text, markup = self._notify_text_and_markup()
                self._safe_edit(token, chat_id, message_id, text, markup)
                answer_callback(token, callback_id, note)
                return

            # Stale / foreign inline buttons — answer quietly, no chat spam.
            LOGGER.info("Ignoring unknown callback: %s", data)
            answer_callback(token, callback_id)
        except Exception as exc:
            LOGGER.exception("Callback failed")
            try:
                answer_callback(token, callback_id, f"失败: {exc}"[:180])
            except Exception:
                pass

    def _apply_action(self, token: str, chat_id: str | int, action: str, inbound_id: int) -> str:
        if action == "auto":
            set_policy(self.node_state_path, inbound_id, mode=MODE_AUTO, locked_address=None)
            return "已恢复自动更新"
        if action == "pause":
            set_policy(self.node_state_path, inbound_id, mode=MODE_PAUSE, locked_address=None)
            return "已暂停自动更新"
        if action == "unlock":
            set_policy(self.node_state_path, inbound_id, mode=MODE_AUTO, locked_address=None)
            return "已解除锁定"
        if action == "lockask":
            self._pending[str(chat_id)] = {"action": "lock", "inbound_id": inbound_id}
            send_telegram(
                token,
                chat_id,
                f"🔒 请发送节点 #{inbound_id} 要锁定的 IP/域名。\n"
                "发送后会立刻写入 3x-ui，并停止自动更新。\n"
                "发送 /cancel 或点其他菜单可取消。",
                reply_markup=MAIN_KEYBOARD,
            )
            return "等待输入 IP"
        raise ValueError(f"unknown action {action}")

    def _handle_pending_text(
        self,
        token: str,
        chat_id: str | int,
        text: str,
        pending: dict[str, Any],
    ) -> None:
        if pending.get("action") != "lock":
            self._pending.pop(str(chat_id), None)
            return
        address = text.strip()
        if not address or len(address) > 253:
            send_telegram(token, chat_id, "地址无效，请重新发送 IP/域名，或 /cancel 取消。")
            return
        # Allow hostname or IP; lightweight check for pure IP form when it looks like numbers.
        if address.replace(".", "").isdigit() and not IP_RE.match(address):
            send_telegram(token, chat_id, "IP 格式不正确，请重新发送，或 /cancel 取消。")
            return
        inbound_id = int(pending["inbound_id"])
        try:
            client = self._panel()
            inbound = client.get_inbound(inbound_id)
            client.update_share_address(inbound, address)
            set_policy(
                self.node_state_path,
                inbound_id,
                mode=MODE_LOCKED,
                locked_address=address,
            )
            self._pending.pop(str(chat_id), None)
            send_telegram(
                token,
                chat_id,
                f"✅ 节点 #{inbound_id} 已锁定为 {address}，后续自动同步会跳过它。",
                reply_markup=MAIN_KEYBOARD,
            )
            self._send_node_list(token, chat_id)
        except (PanelError, Exception) as exc:
            LOGGER.exception("Lock failed")
            send_telegram(token, chat_id, f"❌ 锁定失败：{exc}\n可重试发送地址，或 /cancel 取消。")
