"""[계층 1] 터미널 인터페이스 & UI.

Nothing here holds business logic: commands parse options, hand off to
:mod:`codetest.agent`, and render whatever comes back.
"""
from __future__ import annotations

from .cli_parser import app
from .ui_renderer import (interactive, render_report, show_test_code,
                          show_test_result)

__all__ = [
    "app",
    "render_report",
    "show_test_code",
    "show_test_result",
    "interactive",
]
