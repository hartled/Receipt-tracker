"""Outgoing email. No provider is integrated yet, so nothing is ever sent.

A real adapter (Postmark, SendGrid, SES...) would subclass Mailer, set configured = True, and be returned
by build_mailer() when its settings are present. Adding one is a paid/outside service: get D's OK first."""
from __future__ import annotations

from .config import Settings


class Mailer:
    configured = False
    name = "none"

    def send(self, to: str, subject: str, body: str) -> bool:  # pragma: no cover - interface
        return False


def build_mailer(settings: Settings) -> Mailer:
    return Mailer()
