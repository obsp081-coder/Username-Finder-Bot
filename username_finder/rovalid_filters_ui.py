"""Compact RoValid-specific filters; no provider or persistence changes."""

from dataclasses import asdict

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGridLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .rovalid_filters import ResultFilters


class ResultFilterPanel(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        basic = QGridLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("نص البحث")
        self.search.setMaxLength(256)
        self.text_mode = self.choice([("يحتوي", "contains"), ("يبدأ", "starts"), ("ينتهي", "ends")])
        self.classification = self.choice(
            [("كل النتائج المحفوظة", ""), ("متاح أوليًا", "Available"), ("غير محسوم", "Unknown")]
        )
        self.apply = QPushButton("بحث / تطبيق")
        self.reset = QPushButton("إعادة ضبط المرشحات")
        for i, control in enumerate(
            (self.search, self.text_mode, self.classification, self.apply, self.reset)
        ):
            basic.addWidget(control, i // 3, i % 3)
        layout.addLayout(basic)
        self.toggle = QPushButton("مرشحات متقدمة ▾")
        self.toggle.setCheckable(True)
        layout.addWidget(self.toggle)
        self.advanced = QScrollArea()
        self.advanced.setWidgetResizable(True)
        self.advanced.setMaximumHeight(190)
        self.advanced.setMinimumHeight(130)
        container = QWidget()
        form = QFormLayout(container)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.fields = {}
        choices = {
            "length": (
                "طول الاسم",
                [
                    ("كل الأطوال", "all"),
                    *[(str(n), str(n)) for n in (3, 4, 5, 6)],
                    ("7 أو أكثر", "7+"),
                    ("طول مخصص", "exact"),
                ],
            ),
            "underscore": ("الشرطة السفلية", [("الكل", "all"), ("يحتوي _", "yes"), ("دون _", "no")]),
            "digits": (
                "الأرقام",
                [("الكل", "all"), ("دون أرقام", "none"), ("يحتوي أرقامًا", "some"), ("أرقام فقط", "only")],
            ),
            "digit_count": (
                "عدد الأرقام",
                [("الكل", "all"), ("0", "0"), ("1", "1"), ("2", "2"), ("3 أو أكثر", "3+")],
            ),
            "repetition": (
                "تكرار المحارف",
                [
                    ("الكل", "all"),
                    ("دون تكرار", "none"),
                    ("تكرار في أي موضع", "any"),
                    ("تكرار متجاور", "adjacent"),
                    ("نمط ABAB بأربعة محارف مختلفة الزوجين", "abab"),
                    ("جميع المحارف متطابقة", "identical"),
                ],
            ),
            "start": ("أول محرف", [("الكل", "all"), ("حرف", "letter"), ("رقم", "digit")]),
            "end": ("آخر محرف", [("الكل", "all"), ("حرف", "letter"), ("رقم", "digit")]),
        }
        for key, (label, values) in choices.items():
            self.fields[key] = self.choice(values)
            form.addRow(label, self.fields[key])
        self.exact_length = QSpinBox()
        self.exact_length.setRange(1, 20)
        self.exact_length.setValue(4)
        self.exact_length.setEnabled(False)
        form.addRow("الطول المخصص", self.exact_length)
        self.fields["length"].currentIndexChanged.connect(
            lambda: self.exact_length.setEnabled(self.fields["length"].currentData() == "exact")
        )
        for key, label in (("prefix", "بادئة نصية"), ("suffix", "لاحقة نصية")):
            self.fields[key] = QLineEdit()
            self.fields[key].setMaxLength(256)
            form.addRow(label, self.fields[key])
        self.advanced.setWidget(container)
        self.advanced.hide()
        self.toggle.toggled.connect(self.advanced.setVisible)
        layout.addWidget(self.advanced)
        self.reset.clicked.connect(self.clear)

    @staticmethod
    def choice(values):
        control = QComboBox()
        for label, data in values:
            control.addItem(label, data)
        return control

    def values(self):
        values = {
            key: control.currentData() if isinstance(control, QComboBox) else control.text()
            for key, control in self.fields.items()
        }
        return asdict(
            ResultFilters(
                **values,
                exact_length=self.exact_length.value(),
                text=self.search.text(),
                text_mode=self.text_mode.currentData(),
                classification=self.classification.currentData(),
            )
        )

    def clear(self):
        for control in (*self.fields.values(), self.text_mode, self.classification):
            if isinstance(control, QComboBox):
                control.setCurrentIndex(0)
            else:
                control.clear()
        self.search.clear()
        self.exact_length.setValue(4)
