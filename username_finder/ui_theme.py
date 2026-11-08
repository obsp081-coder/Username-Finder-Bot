"""Shared light design tokens for the desktop presentation layer."""

from pathlib import Path

LIGHT_THEME = """
QWidget#cardGrid { background: transparent; }
QWidget { background: #f5f7fb; color: #162342; font-family: 'Segoe UI'; font-size: 11pt; }
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
QWidget#appHeader, QWidget#sidebar { background: #ffffff; border: 1px solid #e1e7f0; border-radius: 12px; }
QLabel#title, QLabel#igTitle { font-size: 24px; font-weight: 600; color: #152d4e; }
QLabel#heading { font-size: 16px; font-weight: 600; color: #245b9e; padding: 5px; }
QLabel#muted, QLabel#igSubtitle, QLabel#igHint { color: #63738a; }
QLabel#brand { color: #245b9e; font-size: 17px; font-weight: 600; }
QGroupBox { background: #ffffff; border: 1px solid #e1e7f0; border-radius: 12px; margin-top: 16px;
    padding: 12px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top right; padding: 0 8px; right: 12px;
    color: #245b9e; font-weight: 600; }
QLineEdit, QAbstractSpinBox, QComboBox, QTextEdit, QPlainTextEdit, QListWidget {
    background: #ffffff; border: 1px solid #cbd5e3; border-radius: 6px; padding: 5px;
    selection-background-color: #dbeafe; selection-color: #183f6d; }
QLineEdit, QAbstractSpinBox, QComboBox { min-height: 24px; }
QAbstractSpinBox { padding-right: 22px; padding-left: 6px; }
QSpinBox, QDoubleSpinBox { qproperty-alignment: AlignCenter; }
QAbstractSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right;
    width: 18px; border-left: 1px solid #dce5fa; border-bottom: 1px solid #dce5fa; }
QAbstractSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right;
    width: 18px; border-left: 1px solid #dce5fa; }
QAbstractSpinBox::up-arrow { image: url(__UI_ASSETS__/ui_arrow_up.svg); width: 9px; height: 6px; }
QAbstractSpinBox::down-arrow { image: url(__UI_ASSETS__/ui_arrow_down.svg); width: 9px; height: 6px; }
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background: #e4edff; }
QFrame#surfaceCard QCheckBox { background: transparent; }
QWidget#emptyState { background: #f7f9ff; border-radius: 10px; }
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus { border:
    1px solid #3478cb; }
QComboBox QAbstractItemView { background: #ffffff; color: #162342; selection-background-color: #2563ad;
    selection-color: #ffffff; border: 1px solid #cbd5e3; padding: 3px; }
QComboBox QAbstractItemView::item { min-height: 28px; padding: 4px 10px; }
QComboBox QAbstractItemView::item:selected { background: #2563ad; color: #ffffff; }
QComboBox QAbstractItemView::item:hover { background: #e7f0fd; color: #183f6d; }
QComboBox QAbstractItemView::item:focus { border: 1px solid #3478cb; }
QComboBox QAbstractItemView::item:disabled { background: #ffffff; color: #63738a; }
QPushButton { background: #ffffff; border: 1px solid #cbd5e3; border-radius: 6px; padding: 6px 8px;
    min-height: 24px; }
QPushButton:hover { background: #edf4ff; border-color: #9cbce3; }
QPushButton:pressed, QPushButton:checked { background: #dfebfc; }
QPushButton:focus { border: 2px solid #3478cb; padding: 5px 7px; }
QPushButton#primary { background: #2463f6; color: #ffffff; border-color: #2463f6; }
QPushButton#primary:hover { background: #1d5293; }
QPushButton#primary:disabled { background: #eef2f7; color: #8490a1; border-color: #dce3ec; }
QPushButton:disabled, QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled { color: #8490a1;
    background: #eef2f7; border-color: #dce3ec; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; background: white; border: 1px solid #879ab4;
    border-radius: 3px; }
QCheckBox::indicator:checked { background: #2563ad; border-color: #2563ad; }
QCheckBox::indicator:disabled { background: #e6ebf2; }
QCheckBox::indicator:checked:disabled { background: #9ebafb; border-color: #9ebafb; }
QTabBar::tab { background: #eaf0f8; padding: 8px 12px; margin: 2px; border-radius: 5px; }
QTabBar::tab:selected { background: #ffffff; color: #245b9e; border-bottom: 2px solid #3478cb; }
QTabBar::tab:hover { background: #e1ebfa; }
QTabBar::tab:focus { border: 1px solid #3478cb; }
QListWidget#navigation { background: transparent; border: none; padding: 0; outline: none; }
QListWidget#navigation::item { padding: 11px 8px; margin: 3px 0; border-radius: 7px; color: #4b5e77; }
QListWidget#navigation::item:selected { background: #2463f6; color: #ffffff; font-weight: 600; }
QListWidget#navigation::item:hover { background: #f0f5fc; }
QListWidget#navigation::item:focus { border: 1px solid #3478cb; }
QTableView { background: #ffffff; alternate-background-color: #f7f9fd; gridline-color: #e9eef5; border:
    1px solid #e1e7f0; border-radius: 6px; selection-background-color: #dbeafe; selection-color: #183f6d;
    }
QTableView::item:selected { background: #e4edff; color: #123873; }
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
QToolTip { background: #ffffff; color: #162342; border: 1px solid #cbd5e3; padding: 5px; }

QFrame#surfaceCard { background: #ffffff; border: 1px solid #dce5fa; border-radius: 12px; }
QFrame#statCard { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #ffffff,stop:1 #f2f6ff);
    border: 1px solid #dce5fa; border-radius: 12px; }
QLabel#pageTitle { font-size: 26px; font-weight: 700; color: #101b3e; }
QLabel#pageSubtitle { font-size: 14px; color: #576789; }
QLabel#cardTitle, QLabel#actionTitle { font-size: 16px; font-weight: 600; color: #142247; }
QLabel#metricTitle { font-size: 14px; font-weight: 600; color: #273858; }
QLabel#metricValue { font-size: 29px; font-weight: 700; color: #101b3e; }
QLabel#micro { font-size: 12px; color: #63739a; }
QLabel#bodyText { font-size: 15px; color: #233555; }
QLabel#statusPill { font-size: 13px; color: #315ba0; background: #eaf1ff; border-radius: 8px; padding: 7px
    10px; }
QLabel#selectionDetails { color: #344766; background: #edf3ff; border-radius: 8px; padding: 10px; }
QLabel#emptyTitle { font-size: 17px; color: #566b93; font-weight: 600; }
QLabel#notice { background: #fff7e8; color: #785822; border: 1px solid #eedfbd; border-radius: 8px;
    padding: 9px; }
QLabel#sidebarBrand { background: #ffffff; color: #12346d; border: 1px solid #dce5fa;
    border-radius: 10px; padding: 12px; font-size: 17px; font-weight: 700; }
QPushButton#actionCard { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #ffffff,stop:1 #f5f7ff);
    border: 1px solid #dce5fa; border-radius: 10px; padding: 0; }
QPushButton#actionCard:hover { background: #edf3ff; border-color: #7097fa; }
QPushButton#actionCard:focus { border: 2px solid #2463f6; padding: 0; }
QPushButton#textButton { color: #2463f6; border: none; background: transparent; padding: 2px 5px; }
QPushButton#danger { color: #bb354e; background: #fff8fa; border-color: #f0cdd5; }
QPushButton#danger:disabled { color: #9aa8bd; border-color: #dce3ec; background: #eef2f7; }
QTabBar#modeStrip::tab { background: #edf2fe; border: 1px solid #dce5fa; border-radius: 8px;
    padding: 9px 12px; color: #324c79; }
QTabBar#modeStrip::tab:selected { background: #e0eaff; border: 1px solid #739bff; color: #1753c2; }
""".replace("__UI_ASSETS__", Path(__file__).resolve().parent.as_posix())
