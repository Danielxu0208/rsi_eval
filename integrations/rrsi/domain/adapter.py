"""Tracked upstream domain entry; implementation lives in the toolkit."""

from pathlib import Path

from integrations.rrsi.domain_bridge import CodexDomain

DOMAIN = CodexDomain(Path(__file__).resolve().parent)

