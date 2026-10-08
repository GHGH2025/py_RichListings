"""Composition root: inject individual sender strategies into one registry."""
from .navigation import ButtonPagesHandler
from .senders.ethan import EthanHandler
from .senders.michelle import MichelleHandler
from .senders.john import JohnHandler
from .senders.ivan import IvanHandler
from .senders.alam import AlamHandler


class HandlerRegistry:
    def __init__(self, handlers=()):
        self._handlers = {}
        for handler in handlers:
            self.register(handler.handler_key, handler)

    def register(self, key, handler):
        if key in self._handlers:
            raise ValueError(f"Handler already registered: {key}")
        self._handlers[key] = handler

    def get(self, key):
        if key not in self._handlers:
            raise ValueError(f"Unknown email handler: {key}")
        return self._handlers[key]

    def keys(self):
        return sorted(self._handlers)

    def defaults(self):
        return [handler.default_config() for handler in self._handlers.values()
                if getattr(handler, "sender_email", None)]


registry = HandlerRegistry([EthanHandler(), MichelleHandler(), JohnHandler(), IvanHandler(), AlamHandler()])
registry.register("button_pages_v1", ButtonPagesHandler())
