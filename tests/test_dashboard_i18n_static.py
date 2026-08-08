"""Static regression guards for LeapBoard frontend i18n coverage.

There is no JS test runner in this repository yet, so these tests protect the
high-value failure mode directly in the source: dynamic LeapBoard strings must
use t()/tx()/fmt() instead of hardcoded English in render paths.
"""
from __future__ import annotations

from pathlib import Path

_APP_JS = Path(__file__).parents[1] / "src" / "leapflow" / "dashboard" / "static" / "app.js"


def _source() -> str:
    return _APP_JS.read_text(encoding="utf-8")


def test_signal_timeline_dynamic_text_uses_i18n_helpers() -> None:
    src = _source()

    assert 'fmt("seconds ago"' in src
    assert 'fmt("minutes ago"' in src
    assert 'fmt("hours ago"' in src
    assert 't("All")' in src
    assert 'fmt("Showing {shown} of {total} recent events."' in src
    assert 'fmt("Showing {shown} of {total} {family} events."' in src
    assert 'badge.textContent = "\\u26a0 " + t("stale build")' in src
    assert 'badge.textContent = "\\u26a0 stale build"' not in src
    assert '+ "s ago"' not in src
    assert 'footer.textContent = "Showing " +' not in src


def test_tables_and_chart_labels_route_through_translation() -> None:
    src = _source()

    assert 'esc(tx(row.label))' in src
    assert 'esc(tx(v))' in src


def test_connection_status_updates_data_i18n_and_text_together() -> None:
    src = _source()

    assert "function setConnectionStatus(key)" in src
    assert "statusEl.dataset.i18n = key" in src
    assert "statusEl.textContent = t(key)" in src
    assert 'ws.onopen = () => { setConnectionStatus("live"); };' in src
    assert 'ws.onclose = () => { setConnectionStatus("reconnecting…");' in src
    assert 'statusEl.textContent = t("live")' not in src
    assert 'statusEl.textContent = t("reconnecting…")' not in src


def test_i18n_patch_covers_supported_locales_and_signal_keys() -> None:
    src = _source()

    for lang in ("en", "zh", "fr", "es", "ar", "ru"):
        assert f"    {lang}: {{" in src
    for key in (
        "Noise suppressed",
        "Stream events",
        "Active watches",
        "Watch portfolio",
        "Signal health summary",
        "Trigger coverage",
        "stale_build_title",
        "signal.family.clipboard",
        "connected",
        "正在连接",
        "已连接",
        "正在重连",
    ):
        assert key in src
