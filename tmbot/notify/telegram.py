"""Telegram notifier with a long-polling command loop.

Outbound messages are best-effort -- a Telegram outage must never stop the bot
from managing a live position, so send failures are logged, not raised.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

import requests

from ..config import TelegramConfig
from ..store import Store
from .base import CommandHandler, Notifier

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram_update_offset"
MAX_MESSAGE = 3900  # Telegram's limit is 4096; leave room for the prefix


class TelegramNotifier(Notifier):
    PREFIX = {"info": "", "warn": "[warn] ", "error": "[ERROR] "}

    def __init__(self, config: TelegramConfig, store: Optional[Store] = None):
        self.config = config
        self.store = store
        self._base = f"https://api.telegram.org/bot{config.bot_token}"
        self._http = requests.Session()
        self._commands: Dict[str, CommandHandler] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ outbound

    def send(self, message: str, *, level: str = "info") -> None:
        text = f"{self.PREFIX.get(level, '')}{message}"
        for chunk in _chunks(text, MAX_MESSAGE):
            try:
                response = self._http.post(
                    f"{self._base}/sendMessage",
                    json={
                        "chat_id": self.config.chat_id,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    },
                    timeout=self.config.timeout,
                )
                if response.status_code >= 400:
                    log.warning("telegram sendMessage failed: %s", response.text[:200])
            except requests.RequestException as exc:
                log.warning("telegram unreachable: %s", exc)

    # ------------------------------------------------------------------ commands

    def register(self, command: str, handler: CommandHandler) -> None:
        self._commands[command.lower()] = handler

    def start(self) -> None:
        if self._thread is not None or not self._commands:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="telegram-commands", daemon=True
        )
        self._thread.start()
        log.info("telegram command listener started (%d commands)", len(self._commands))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _offset(self) -> int:
        if not self.store:
            return 0
        raw = self.store.get(OFFSET_KEY, "0")
        try:
            return int(raw or 0)
        except ValueError:
            return 0

    def _save_offset(self, offset: int) -> None:
        if self.store:
            self.store.set(OFFSET_KEY, str(offset))

    def _poll_loop(self) -> None:
        offset = self._offset()
        while not self._stop.is_set():
            try:
                response = self._http.get(
                    f"{self._base}/getUpdates",
                    params={"offset": offset + 1, "timeout": 25},
                    timeout=self.config.timeout + 25,
                )
                if response.status_code >= 400:
                    log.warning("telegram getUpdates failed: %s", response.text[:200])
                    self._stop.wait(5)
                    continue
                for update in response.json().get("result", []):
                    offset = max(offset, update.get("update_id", offset))
                    self._save_offset(offset)
                    self._handle(update)
            except requests.RequestException as exc:
                log.debug("telegram poll error: %s", exc)
                self._stop.wait(self.config.poll_seconds)
            except Exception:
                log.exception("telegram poll loop error")
                self._stop.wait(self.config.poll_seconds)

    def _handle(self, update: Dict) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        text = (message.get("text") or "").strip()
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if not text.startswith("/"):
            return
        if chat_id and chat_id != str(self.config.chat_id):
            # Only the configured chat may drive a live trading account.
            log.warning("ignoring command from unauthorised chat %s", chat_id)
            return
        command, _, argument = text[1:].partition(" ")
        command = command.split("@", 1)[0].lower()
        handler = self._commands.get(command)
        if handler is None:
            self.send(f"Unknown command /{command}. Try /help.")
            return
        try:
            reply = handler(argument.strip())
        except Exception as exc:
            log.exception("command /%s failed", command)
            reply = f"/{command} failed: {exc}"
        if reply:
            self.send(reply)


def _chunks(text: str, size: int):
    if len(text) <= size:
        yield text
        return
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start:
                end = newline
        yield text[start:end]
        start = end
