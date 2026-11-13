"""Offscreen filtered browsing, source controls and asynchronous local searches."""

import csv
import hashlib

import pytest
from PySide6.QtCore import QTimer
from test_rovalid_ui import desktop as desktop
from test_rovalid_ui import wait_idle

from username_finder import rovalid, rovalid_review


def test_four_proxy_controls_manual_multiline_file_and_privacy(desktop, tmp_path):
    panel = desktop.roblox
    assert [panel.proxy_mode.itemData(i) for i in range(4)] == ["direct", "file", "manual", "sources"]
    assert panel.secret_options()["proxies"] == []
    assert panel.proxy_entries.isHidden() and panel.proxy_path.isHidden()
    panel.proxy_mode.setCurrentIndex(2)
    assert not panel.proxy_entries.isHidden() and panel.proxy_path.isHidden()
    panel.proxy_entries.setPlainText("http://user:Secret@host:80\nhttp://user:Secret@HOST:80\nhost:81")
    packet = panel.secret_options()
    assert len(packet["proxies"]) == 2 and not packet["scraped"]
    assert "Secret" not in panel.proxy_notice.text() and "مكرر: 1" in panel.proxy_notice.text()
    assert not panel.proxy_entries.revealed
    panel.proxy_reveal.setChecked(True)
    assert panel.proxy_entries.revealed
    panel.proxy_mode.setCurrentIndex(1)
    assert not panel.proxy_entries.revealed and not panel.proxy_path.isHidden()
    source = tmp_path / "proxies.txt"
    source.write_text("host:80\nhost:80\nother:81", encoding="utf-8-sig")
    panel.proxy_path.setText(str(source))
    assert panel.secret_options()["proxies"] == ["host:80", "other:81"]
    source.write_text("http://user:Secret@host:80?bad=1")
    panel.validate_proxy_input()
    assert "Secret" not in panel.proxy_notice.text() and "تعذر" in panel.proxy_notice.text()
    assert "proxy" not in str(panel.options())


def test_fetch_unavailable_and_direct_handler_gated(desktop, monkeypatch):
    panel = desktop.roblox
    monkeypatch.setattr(rovalid_review, "journal", lambda _: [])
    monkeypatch.setattr(panel, "launch", lambda *a: pytest.fail("No child dispatch"))
    panel.proxy_mode.setCurrentIndex(3)
    assert not panel.proxy_fetch.isEnabled()
    assert "429" in panel.proxy_notice.text()
    panel.fetch_proxies()
    assert panel.proxy_pool == []


def populate(desktop, qtbot, count=1300):
    panel = desktop.roblox
    panel.count.setValue(1)
    panel.prepare()
    wait_idle(qtbot, panel)
    directory = rovalid.run_directory(panel.root, panel.current_run)
    results = directory / "results"
    results.mkdir()
    (results / "hits.txt").write_text("".join(f"Name{i:06d}\n" for i in range(count)), encoding="utf8")
    (results / "unresolved.txt").write_text("aBaB\nOther_1\n1234\n", encoding="utf8")
    return panel, results


def test_pagination_filter_reset_and_exact_export(desktop, qtbot, tmp_path, monkeypatch):
    panel, results = populate(desktop, qtbot)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in results.iterdir()}
    panel.refresh_results()
    wait_idle(qtbot, panel)
    assert panel.result_table.rowCount() == 500 and panel.result_total == 1303
    panel.next_results.click()
    wait_idle(qtbot, panel)
    assert panel.result_offset == 500 and "501" in panel.result_count.text()
    assert panel.result_table.item(0, 0).text() == "Name000500"
    panel.search.setText("Name001")
    panel.result_filters.apply.click()
    wait_idle(qtbot, panel)
    assert panel.result_offset == 0 and panel.result_total == 300
    panel.result_filters.reset.click()
    wait_idle(qtbot, panel)
    assert panel.result_total == 1303
    panel.result_filters.fields["repetition"].setCurrentIndex(
        panel.result_filters.fields["repetition"].findData("abab")
    )
    panel.refresh_results()
    wait_idle(qtbot, panel)
    assert panel.result_table.rowCount() == 1 and panel.result_table.item(0, 0).text() == "aBaB"
    out = tmp_path / "filtered.csv"
    monkeypatch.setattr("username_finder.rovalid_ui.QFileDialog.getSaveFileName", lambda *a: (str(out), ""))
    panel.export()
    wait_idle(qtbot, panel)
    with out.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 1 and rows[0]["username"] == "aBaB" and rows[0]["classification"] == "Unknown"
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}


def test_large_search_keeps_event_loop_responsive_and_can_cancel(desktop, qtbot):
    panel, results = populate(desktop, qtbot, count=100000)
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    panel.search.setText("not-found")
    panel.refresh_results()
    assert panel.busy and not panel.result_filters.isEnabled()
    qtbot.waitUntil(lambda: len(ticks) >= 3)
    panel.stop()
    wait_idle(qtbot, panel)
    timer.stop()
    assert len(ticks) >= 3 and panel.result_table.rowCount() == 0
    assert panel.outcome == "cancelled" and "أُلغيت" in panel.activity_status.text()
    assert panel.result_total == 0 and not panel.next_results.isEnabled()
    assert (results / "hits.txt").stat().st_size > 0


@pytest.mark.parametrize("width,height", [(900, 650), (1180, 850)])
def test_advanced_filters_compact_rtl_layout(desktop, qtbot, width, height):
    desktop.resize(width, height)
    desktop.tabs.setCurrentWidget(desktop.roblox)
    panel = desktop.roblox
    panel.workspace.setCurrentIndex(2)
    panel.result_filters.toggle.setChecked(True)
    qtbot.wait(30)
    assert desktop.width() == width and desktop.height() == height
    assert panel.result_filters.advanced.height() <= 190
    assert panel.result_filters.advanced.horizontalScrollBar().maximum() == 0
    assert panel.result_table.height() > 40
    panel.result_filters.fields["length"].setCurrentIndex(6)
    assert panel.result_filters.exact_length.isEnabled()
