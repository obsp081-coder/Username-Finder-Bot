import pytest
from osintgram_fixture import PROFILE
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
from test_osintgram_ui import choose, discover, run
from test_osintgram_ui import ui as ui


def check_rect(tree, item):
    option = QStyleOptionViewItem()
    option.initFrom(tree)
    option.widget = tree
    option.rect = tree.visualItemRect(item)
    tree.itemDelegate().initStyleOption(option, tree.indexFromItem(item))
    return tree.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, tree)


def test_row_checkbox_keyboard_and_inspection_are_separate(ui, qtbot):
    window, panel, backend = ui
    discover(panel, qtbot)
    item = choose(panel, "get_user_info", False)
    tree = panel.commands
    tree.scrollToItem(item)
    center = tree.mapTo(window.instagram.widget(), tree.rect().center())
    window.instagram.ensureVisible(center.x(), center.y(), 0, 20)
    qtbot.wait(10)
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=tree.visualItemRect(item).center())
    assert panel.selected() == ["get_user_info"] and panel.current_tool == "get_user_info"
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=check_rect(tree, item).center())
    assert not panel.selected()  # A checkbox click toggles once.
    qtbot.keyClick(tree, Qt.Key.Key_Space)
    assert panel.selected() == ["get_user_info"]
    qtbot.keyClick(tree, Qt.Key.Key_Down)
    assert panel.selected() == ["get_user_info"]  # Inspection never selects.
    other = choose(panel, "get_followers", False)
    assert panel.current_tool == "get_followers" and other.checkState(0) == Qt.CheckState.Unchecked
    qtbot.keyClick(tree, Qt.Key.Key_Space)
    assert len(panel.selected()) == 2 and "2" in panel.selection_count.text()
    assert panel.limit.value() == 10
    assert not any(method == "POST" for method, path, body in backend.calls)
    group = item.parent()
    tree.scrollToItem(group)
    qtbot.wait(5)
    rect = tree.visualItemRect(group)
    arrow = QPoint(
        min(tree.viewport().width() - 5, rect.right() + tree.indentation() // 2), rect.center().y()
    )
    was_expanded = group.isExpanded()
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=arrow)
    assert group.isExpanded() != was_expanded and len(panel.selected()) == 2


def test_all_commands_and_recommendation_never_run_analysis(ui, qtbot):
    from username_finder.osintgram_catalog import CATALOG

    window, panel, backend = ui
    backend.tools = [{"name": name, "params": [], "needs_target": True} for name in CATALOG]
    discover(panel, qtbot)
    assert {tool["name"] for tool in panel.tools} == set(CATALOG)
    panel.manager.backend = "instagrapi"
    panel._update_capabilities()
    panel.recommended_button.click()
    assert panel.selected() == ["get_user_info"] and panel.limit.value() == 10
    unsupported = choose(panel, "get_account_about", False)
    panel.commands.scrollToItem(unsupported)
    qtbot.mouseClick(
        panel.commands.viewport(),
        Qt.MouseButton.LeftButton,
        pos=panel.commands.visualItemRect(unsupported).center(),
    )
    assert panel.selected() == ["get_user_info"]
    panel.clear_selection_button.click()
    assert not panel.selected() and "0" in panel.selection_count.text()
    assert not any(method == "POST" for method, path, body in backend.calls)


@pytest.mark.parametrize(
    "state", ["unchecked", "checked", "hovered", "selected", "focused", "disabled", "checked_disabled"]
)
def test_checkbox_delegate_visibility(ui, qtbot, state):
    window, panel, backend = ui
    discover(panel, qtbot)
    item = choose(panel, "get_user_info", state in ("checked", "checked_disabled"))
    tree = panel.commands
    option = QStyleOptionViewItem()
    option.initFrom(tree)
    option.widget = tree
    option.rect = QRect(0, 0, 360, 46)
    option.state = QStyle.StateFlag.State_Enabled
    if state == "hovered":
        option.state |= QStyle.StateFlag.State_MouseOver
    if state == "selected":
        option.state |= QStyle.StateFlag.State_Selected
    if state == "focused":
        option.state |= QStyle.StateFlag.State_HasFocus
    if state in ("disabled", "checked_disabled"):
        option.state = QStyle.StateFlag.State_None
    index = tree.indexFromItem(item)
    tree.itemDelegate().initStyleOption(option, index)
    box = tree.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, tree)
    image = QImage(360, 46, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    tree.itemDelegate().paint(painter, option, index)
    painter.end()
    pixels = [
        image.pixelColor(x, y).name()
        for x in range(box.left(), box.right() + 1)
        for y in range(box.top(), box.bottom() + 1)
    ]
    assert box.width() >= 20 and len(set(pixels)) > 2
    assert pixels.count("#2463a6") > 100 if state == "checked" else "#2463a6" not in pixels
    assert "#9daaba" in pixels if state in ("disabled", "checked_disabled") else "#365d85" in pixels


def test_profile_and_list_labels_preserve_raw_values(ui, qtbot):
    window, panel, backend = ui
    backend.results["get_user_info"] = {
        **PROFILE,
        "biography": "",
        "follower_count": 1234567,
        "account_type": 2,
        "public_email": "",
        "unfamiliar_field": None,
    }
    discover(panel, qtbot)
    panel.target.setText("fixture")
    choose(panel, "get_user_info")
    choose(panel, "get_followers")
    run(panel, qtbot)
    panel.result_picker.setCurrentIndex(0)
    labels = [panel.table.item(r, 0).text() for r in range(panel.table.rowCount())]
    values = [panel.table.item(r, 1).text() for r in range(panel.table.rowCount())]
    assert "نوع الحساب" in labels and "البريد الإلكتروني المعلن" in labels
    assert "لا توجد نبذة" in values and "1,234,567" in panel.profile_summary.text()
    assert "لا توجد نبذة" in panel.profile_summary.text()
    assert "unfamiliar_field" in " ".join(labels)
    raw = panel.document["results"][0]["result"]
    assert raw["biography"] == "" and raw["follower_count"] == 1234567 and raw["account_type"] == 2
    panel.result_picker.setCurrentIndex(1)
    assert panel.table.horizontalHeaderItem(0).text() == "معرّف الحساب"
    assert panel.table.horizontalHeaderItem(1).text() == "اسم المستخدم"
    assert raw["id"] == PROFILE["id"]
    assert not window.store.jobs() and not window.instagram.start_button.isEnabled()
