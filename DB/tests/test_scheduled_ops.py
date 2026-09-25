from __future__ import annotations

from pathlib import Path


DB_ROOT = Path(__file__).resolve().parents[1]


def test_cron_template_keeps_canon_refresh_and_freshness_monitor() -> None:
    cron = (DB_ROOT / "cron.example").read_text(encoding="utf-8")

    assert "17 */6 * * *" in cron
    assert "./refresh_publications_canon_web.sh" in cron
    assert "47 * * * *" in cron
    assert "./check_canon_web_freshness.sh" in cron
