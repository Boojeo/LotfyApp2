"""Notification interface plus console and fan-out implementations."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional

from ..i18n import Translator

log = logging.getLogger(__name__)

# A command handler takes the argument string and returns the reply text.
CommandHandler = Callable[[str], str]


class Notifier(ABC):
    @abstractmethod
    def send(self, message: str, *, level: str = "info") -> None:
        ...

    def register(self, command: str, handler: CommandHandler) -> None:
        """Wire up a chat command.  No-op for notifiers without an input side."""

    def set_translator(self, t: Translator) -> None:
        """Adopt the active display language.  Called again after /lang."""

    def start(self) -> None:
        """Begin listening for commands, if the transport supports it."""

    def stop(self) -> None:
        ...


class ConsoleNotifier(Notifier):
    LEVELS = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}

    def send(self, message: str, *, level: str = "info") -> None:
        log.log(self.LEVELS.get(level, logging.INFO), "%s", message)


class NullNotifier(Notifier):
    def __init__(self) -> None:
        self.messages: List[tuple[str, str]] = []

    def send(self, message: str, *, level: str = "info") -> None:
        self.messages.append((level, message))


class MultiNotifier(Notifier):
    """Fan out to several notifiers; one failing transport must not stop the rest."""

    def __init__(self, notifiers: List[Notifier]):
        self.notifiers = notifiers
        self._commands: Dict[str, CommandHandler] = {}

    def send(self, message: str, *, level: str = "info") -> None:
        for notifier in self.notifiers:
            try:
                notifier.send(message, level=level)
            except Exception:
                log.exception("notifier %s failed", type(notifier).__name__)

    def register(self, command: str, handler: CommandHandler) -> None:
        self._commands[command] = handler
        for notifier in self.notifiers:
            notifier.register(command, handler)

    def set_translator(self, t: Translator) -> None:
        for notifier in self.notifiers:
            notifier.set_translator(t)

    def start(self) -> None:
        for notifier in self.notifiers:
            notifier.start()

    def stop(self) -> None:
        for notifier in self.notifiers:
            try:
                notifier.stop()
            except Exception:
                log.exception("failed to stop %s", type(notifier).__name__)
