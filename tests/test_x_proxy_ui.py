import threading

from test_x_ui import wait_done
from test_x_ui import window as window

from username_finder import x_connection as connection
from username_finder import x_ui


def test_direct_default_proxy_validation_and_explicit_save(window, qtbot):
    panel = window.x_panel
    assert panel.connection_mode.currentData() == "direct"
    assert not panel.proxy_url.isEnabled()
    assert panel.repo.preferred_connection() == connection.DIRECT
    panel.connection_mode.setCurrentIndex(panel.connection_mode.findData("proxy"))
    assert panel.proxy_url.isEnabled()
    panel.proxy_url.setText("http://user:password@localhost:8080")
    panel.save_connection()
    assert "password" not in str(panel.repo.control())
    assert panel.repo.preferred_connection() == connection.DIRECT
    assert "بيانات دخول" in panel.connection_status.text()
    panel.proxy_url.setText("https://localhost:8080")
    panel.start()
    assert not panel.repo.runs() and "احفظ" in panel.connection_status.text()
    panel.save_connection()
    panel.quantity.setText("1")
    panel.preview()
    wait_done(qtbot, panel)
    identity = panel.current_run
    saved = connection.configuration("proxy", "https://localhost:8080")
    assert panel.repo.connection_for_run(identity) == saved
    panel.connection_mode.setCurrentIndex(panel.connection_mode.findData("direct"))
    panel.save_connection()
    panel.resume()
    wait_done(qtbot, panel)
    assert panel.repo.connection_for_run(identity) == saved
    assert panel.repo.preferred_connection() == connection.DIRECT
    assert "https://localhost:8080" in panel.status.text()


def test_neutral_test_uses_entered_proxy_without_creating_jobs_or_saving_route(window, qtbot, monkeypatch):
    panel = window.x_panel
    calls = []

    def neutral(route, **kwargs):
        calls.append((route, kwargs["compare"]))
        return dict(success=True, used_proxy=True, latency_seconds=0.125, exit_changed=True)

    monkeypatch.setattr(connection, "test_connection", neutral)
    panel.connection_mode.setCurrentIndex(panel.connection_mode.findData("proxy"))
    panel.proxy_url.setText("http://localhost:8080")
    panel.compare_direct.setChecked(True)
    panel.test_connection_button.click()
    wait_done(qtbot, panel)
    assert calls == [(connection.configuration("proxy", "http://localhost:8080"), True)]
    assert panel.repo.preferred_connection() == connection.DIRECT
    assert not panel.repo.runs() and not panel.test_clock.calls
    assert "0.125" in panel.connection_status.text() and "مختلف" in panel.connection_status.text()


def test_connection_test_cancellation_is_responsive_and_controls_locked(window, qtbot, monkeypatch):
    panel = window.x_panel
    entered = threading.Event()

    def neutral(route, *, cancel, **kwargs):
        entered.set()
        assert cancel.wait(3)
        return dict(success=False, used_proxy=False, exit_changed=None, latency_seconds=0.1, cancelled=True)

    monkeypatch.setattr(connection, "test_connection", neutral)
    panel.test_connection()
    qtbot.waitUntil(entered.is_set)
    assert not panel.connection_mode.isEnabled() and not panel.save_connection_button.isEnabled()
    panel.stop()
    wait_done(qtbot, panel)
    assert "أُلغي" in panel.connection_status.text()
    assert panel.connection_mode.isEnabled()
    assert not panel.test_clock.calls and not panel.repo.runs()


def test_saved_connection_restores_in_panel_without_automatic_requests(window, qtbot):
    panel = window.x_panel
    saved = connection.configuration("proxy", "http://localhost:8080")
    panel.repo.set_connection(saved)
    recreated = x_ui.XPanel(
        window.store,
        window.make_table,
        window.fill_table,
        fetcher=lambda *args: (_ for _ in ()).throw(AssertionError("No requests")),
    )
    qtbot.addWidget(recreated)
    try:
        assert recreated.connection_mode.currentData() == "proxy"
        assert recreated.proxy_url.text() == saved["proxy_url"]
        assert recreated.proxy_url.isEnabled()
    finally:
        recreated.shutdown()


def test_neutral_success_or_config_change_never_unblocks_restricted_x(window, qtbot, monkeypatch):
    panel = window.x_panel
    with window.store.transaction() as db:
        panel.repo._control(db, blocked="unexpected_http_status:429", not_before="2099-01-01T00:00:00+00:00")
    panel.refresh()
    assert not panel.start_button.isEnabled() and panel.test_connection_button.isEnabled()
    monkeypatch.setattr(
        connection,
        "test_connection",
        lambda *a, **kw: dict(success=True, used_proxy=False, exit_changed=None, latency_seconds=0.1),
    )
    panel.test_connection()
    wait_done(qtbot, panel)
    panel.connection_mode.setCurrentIndex(panel.connection_mode.findData("proxy"))
    panel.proxy_url.setText("http://localhost:8080")
    panel.save_connection()
    assert panel.repo.control()["blocked"] == "unexpected_http_status:429"
    assert panel.repo.control()["not_before"] == "2099-01-01T00:00:00+00:00"
    assert not panel.start_button.isEnabled() and not panel.test_clock.calls
