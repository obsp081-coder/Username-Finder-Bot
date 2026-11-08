"""Shared light design tokens for the desktop presentation layer."""

LIGHT_THEME = """
QWidget { background: #f5f7fb; color: #24344b; font-family: 'Segoe UI'; font-size: 10.5pt; }
QLabel { background: transparent; }
QGroupBox QWidget { background: #ffffff; }
QMainWindow, QScrollArea, QTabWidget::pane { background: #f5f7fb; }
QScrollArea, QTabWidget::pane { border: none; }
QScrollBar:vertical { background: #edf2f8; width: 9px; margin: 0; border: none; }
QScrollBar:horizontal { background: #edf2f8; height: 9px; margin: 0; border: none; }
QScrollBar::handle:vertical { background: #b6c6dc; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:horizontal { background: #b6c6dc; border-radius: 4px; min-width: 30px; }
QScrollBar::handle:hover { background: #8da9ce; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; border: none; background: transparent; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QWidget#appHeader, QWidget#sidebar { background: #ffffff; border: 1px solid #e1e7f0; border-radius: 10px; }
QLabel#title, QLabel#igTitle { font-size: 21px; font-weight: 600; color: #152d4e; }
QLabel#heading { font-size: 16px; font-weight: 600; color: #245b9e; padding: 5px; }
QLabel#muted, QLabel#igSubtitle, QLabel#igHint { color: #63738a; }
QLabel#brand { color: #245b9e; font-size: 17px; font-weight: 600; }
QGroupBox { background: #ffffff; border: 1px solid #e1e7f0; border-radius: 10px; margin-top: 16px;
    padding: 12px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top right; padding: 0 8px; right: 12px;
    color: #245b9e; font-weight: 600; }
QLineEdit, QAbstractSpinBox, QComboBox, QTextEdit, QPlainTextEdit, QListWidget {
    background: #ffffff; border: 1px solid #cbd5e3; border-radius: 6px; padding: 5px;
    selection-background-color: #dbeafe; selection-color: #183f6d; }
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus { border:
    1px solid #3478cb; }
QComboBox QAbstractItemView { background: #ffffff; color: #24344b; selection-background-color: #2563ad;
    selection-color: #ffffff; border: 1px solid #cbd5e3; padding: 3px; }
QComboBox QAbstractItemView::item { min-height: 28px; padding: 4px 10px; }
QComboBox QAbstractItemView::item:selected { background: #2563ad; color: #ffffff; }
QComboBox QAbstractItemView::item:hover { background: #e7f0fd; color: #183f6d; }
QComboBox QAbstractItemView::item:focus { border: 1px solid #3478cb; }
QComboBox QAbstractItemView::item:disabled { background: #ffffff; color: #63738a; }
QPushButton { background: #ffffff; border: 1px solid #cbd5e3; border-radius: 6px; padding: 6px 11px;
    min-height: 20px; }
QPushButton:hover { background: #edf4ff; border-color: #9cbce3; }
QPushButton:pressed, QPushButton:checked { background: #dfebfc; }
QPushButton:focus { border: 2px solid #3478cb; padding: 5px 10px; }
QPushButton#primary { background: #2563ad; color: #ffffff; border-color: #2563ad; }
QPushButton#primary:hover { background: #1d5293; }
QPushButton#primary:disabled { background: #eef2f7; color: #8490a1; border-color: #dce3ec; }
QPushButton:disabled, QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled { color: #8490a1;
    background: #eef2f7; border-color: #dce3ec; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; background: white; border: 1px solid #879ab4;
    border-radius: 3px; }
QCheckBox::indicator:checked { background: #2563ad; border-color: #2563ad; }
QCheckBox::indicator:disabled { background: #e6ebf2; }
QTabBar::tab { background: #eaf0f8; padding: 8px 12px; margin: 2px; border-radius: 5px; }
QTabBar::tab:selected { background: #ffffff; color: #245b9e; border-bottom: 2px solid #3478cb; }
QTabBar::tab:hover { background: #e1ebfa; }
QTabBar::tab:focus { border: 1px solid #3478cb; }
QListWidget#navigation { background: transparent; border: none; padding: 0; outline: none; }
QListWidget#navigation::item { padding: 11px 8px; margin: 3px 0; border-radius: 7px; color: #4b5e77; }
QListWidget#navigation::item:selected { background: #e7f0fd; color: #1c559b; font-weight: 600; }
QListWidget#navigation::item:hover { background: #f0f5fc; }
QListWidget#navigation::item:focus { border: 1px solid #3478cb; }
QTableView { background: #ffffff; alternate-background-color: #f7f9fd; gridline-color: #e9eef5; border:
    1px solid #e1e7f0; border-radius: 6px; selection-background-color: #dbeafe; selection-color: #183f6d;
    }
QHeaderView::section { background: #edf2f9; color: #415874; padding: 8px; border: none; font-weight: 600; }
QLabel#simulation, QLabel#igStatus { background: #fff6e4; color: #795b27; border: 1px solid #efdfbe;
    padding: 7px; border-radius: 6px; }
QLabel#metric, QLabel#igCounter { background: #ffffff; border: 1px solid #e1e7f0; border-radius: 8px;
    padding: 12px; }
QLabel#igFileEmpty { background: #f5f7fb; color: #63738a; border: 1px dashed #cbd5e3; border-radius: 6px;
    padding: 8px; }
QProgressBar { background: #edf2f9; border: none; border-radius: 5px; text-align: center; min-height: 22px; }
QProgressBar::chunk { background: #a7c7ee; border-radius: 5px; }
QStatusBar { background: #ffffff; color: #63738a; border-top: 1px solid #e1e7f0; }
QToolTip { background: #ffffff; color: #24344b; border: 1px solid #cbd5e3; padding: 5px; }
"""
