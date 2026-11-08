"""Arabic RTL offline file generator, independent of checking jobs and providers."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .file_generator import RECORD_LIMIT, IdentifierSpace, generate_file, read_journal
from .file_generator_output import (
    check_capacity,
    create_output_directory,
    friendly_error,
    proposed_output,
)
from .file_generator_rules import DIGITS, GAMING, LOWER, SOCIAL, UPPER, Grammar, variants, violations
from .ui_helpers import style_combo_popup

CONFIDENCE = {"Verified": "موثق للصيغة المحددة", "Provisional": "مؤقت", "Incomplete": "غير مكتمل"}


def identifier_labels(profile):
    if profile.key == "riot":
        return "Riot ID", "اسم اللاعب (GameName)"
    names = {
        "pubg": "اسم اللاعب",
        "coc": "اسم اللاعب",
        "cr": "اسم اللاعب",
        "genshin": "اسم اللاعب",
        "8ball": "اسم اللاعب",
        "psn": "معرّف PlayStation",
        "xbox": "اسم اللاعب الحديث (Gamertag)",
        "xbox_legacy": "اسم اللاعب القديم (Gamertag)",
        "steam": "اسم العرض في Steam",
        "steam_url": "الرابط المخصص في Steam",
        "epic": "اسم العرض في Epic",
        "minecraft_java": "اسم اللاعب في Minecraft Java",
        "activision": "جزء اسم العرض في Activision",
        "battletag": "جزء اسم العرض في BattleTag",
        "ea": "معرّف حساب EA",
        "rockstar": "اسم حساب Rockstar",
        "ubisoft": "اسم حساب Ubisoft",
    }
    label = names.get(profile.key, "اسم المستخدم")
    return label, label


class RuleEditor(QGroupBox):
    changed = Signal()

    def __init__(self, title):
        super().__init__(title)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.advanced = QGroupBox(title)
        advanced_layout = QVBoxLayout(self.advanced)
        advanced_form = QFormLayout()
        self.minimum, self.maximum = QSpinBox(), QSpinBox()
        for control in (self.minimum, self.maximum):
            control.setRange(1, RECORD_LIMIT)
            control.setMaximumWidth(180)
            control.valueChanged.connect(self.changed)
        length_row = QHBoxLayout()
        length_row.addWidget(QLabel("من"))
        length_row.addWidget(self.minimum)
        length_row.addWidget(QLabel("إلى"))
        length_row.addWidget(self.maximum)
        length_row.addStretch()
        form.addRow("عدد الخانات", length_row)
        length_hint = QLabel("عدد الخانات التي يتكون منها الاسم. لطول ثابت، اختر العدد نفسه في الخانتين.")
        length_hint.setWordWrap(True)
        form.addRow(length_hint)
        self.groups = {}
        for key, label, alphabet in (
            ("lower", "حروف صغيرة (a، b)", LOWER),
            ("upper", "حروف كبيرة (A، B)", UPPER),
            ("digits", "أرقام (0–9)", DIGITS),
        ):
            row = QHBoxLayout()
            check, subset = QCheckBox(label), QLineEdit(alphabet)
            subset.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            check.setToolTip(
                {
                    "lower": "استخدام الحروف الإنجليزية الصغيرة مثل a وb.",
                    "upper": "استخدام الحروف الإنجليزية الكبيرة مثل A وB، إذا سمحت المنصة.",
                    "digits": "السماح بالأرقام من 0 إلى 9.",
                }[key]
            )
            subset.setToolTip(
                "اترك المجموعة كاملة، أو اكتب الحروف أو الأرقام التي تريدها فقط؛ تكرارها لا يكرر النتائج."
            )
            row.addWidget(check)
            row.addWidget(subset)
            check.toggled.connect(self.changed)
            subset.textEdited.connect(self.changed)
            self.groups[key] = check, subset, alphabet
            form.addRow(row)
        alphabet_hint = QLabel("عدّل المجموعة بجانب كل خيار لاستخدام حروف أو أرقام محددة فقط.")
        alphabet_hint.setWordWrap(True)
        form.addRow(alphabet_hint)
        layout.addLayout(form)
        special_row = QHBoxLayout()
        self.specials_label = QLabel("العلامات المسموحة")
        special_row.addWidget(self.specials_label)
        self.extra_specials = QHBoxLayout()
        self.extra_label = QLabel("علامات إضافية خارج إعداد المنصة")
        self.extra_label.setToolTip("يمكن اختيارها للقواعد المخصصة؛ قد ترفض المنصة هذه العلامات.")
        self.extra_specials.addWidget(self.extra_label)
        advanced_layout.addLayout(self.extra_specials)
        self.first, self.last = QComboBox(), QComboBox()
        for control in (self.first, self.last):
            for label, value in (
                ("أي محرف محدد", "any"),
                ("حرف", "letter"),
                ("حرف أو رقم", "alnum"),
                ("رقم", "digit"),
            ):
                control.addItem(label, value)
            style_combo_popup(control)
            control.currentIndexChanged.connect(self.changed)
        self.first.setToolTip("نوع الخانة الأولى في كل اسم؛ يُطبق مع مجموعة الحروف والأرقام التي اخترتها.")
        self.last.setToolTip("نوع الخانة الأخيرة في كل اسم؛ لا يضيف حروفًا جديدة إلى المجموعة.")
        self.prefix, self.suffix = QLineEdit(), QLineEdit()
        for control in (self.prefix, self.suffix):
            control.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            control.textEdited.connect(self.changed)
        self.prefix.setToolTip("حروف أو أرقام ثابتة في بداية كل اسم، وهي جزء من الطول المحدد.")
        self.suffix.setToolTip("حروف أو أرقام ثابتة في نهاية كل اسم، وهي جزء من الطول المحدد.")
        advanced_form.addRow("أول خانة", self.first)
        advanced_form.addRow("آخر خانة", self.last)
        advanced_form.addRow("بداية ثابتة للاسم", self.prefix)
        advanced_form.addRow("نهاية ثابتة للاسم", self.suffix)
        advanced_layout.addLayout(advanced_form)
        grid = QGridLayout()
        for column, label in enumerate(("الرمز", "أقصى تكرار", "بداية", "داخل", "نهاية", "منع التتابع")):
            header = QLabel(label)
            header.setToolTip(
                "أكبر عدد مرات يمكن أن تظهر فيها العلامة، ومواضع السماح بها. "
                "منع التتابع يمنع علامتين متجاورتين."
            )
            grid.addWidget(header, 0, column)
        self.specials = {}
        for row, char in enumerate("_.-", 1):
            enabled = QCheckBox(char)
            special_row.addWidget(enabled)
            count = QSpinBox()
            count.setRange(0, RECORD_LIMIT)
            positions = [QCheckBox() for _ in range(3)]
            adjacent = QCheckBox()
            enabled.toggled.connect(self.changed)
            grid.addWidget(QLabel(char), row, 0)
            controls = [count, *positions, adjacent]
            for column, control in enumerate(controls, 1):
                grid.addWidget(control, row, column)
                if isinstance(control, QSpinBox):
                    control.valueChanged.connect(self.changed)
                else:
                    control.toggled.connect(self.changed)
            count.setToolTip("أكبر عدد مرات يمكن أن تظهر فيها العلامة داخل الاسم؛ صفر يعني منعها.")
            for control, place in zip(positions, ("بداية الاسم", "وسط الاسم", "نهاية الاسم"), strict=True):
                control.setToolTip("السماح بهذه العلامة في " + place)
            adjacent.setToolTip("منع ظهور هذه العلامة مرتين متتاليتين، مثل نقطتين بجانب بعضهما.")
            self.specials[char] = enabled, count, positions, adjacent
        special_row.addStretch()
        self.common_specials = special_row
        layout.addLayout(special_row)
        advanced_layout.addLayout(grid)

    def load(self, rule):
        self.minimum.setValue(rule.minimum)
        self.maximum.setValue(rule.maximum)
        for check, subset, alphabet in self.groups.values():
            chosen = "".join(c for c in rule.alphabet if c in alphabet)
            check.setChecked(bool(chosen))
            subset.setText(chosen or alphabet)
        self.first.setCurrentIndex(self.first.findData(rule.first))
        self.last.setCurrentIndex(self.last.findData(rule.last))
        self.prefix.setText(rule.prefix)
        self.suffix.setText(rule.suffix)
        known = {row[0]: row[1:] for row in rule.specials}
        self.specials_label.setText("العلامات المسموحة" if known else "لا علامات خاصة في إعداد المنصة")
        self.extra_label.setVisible(len(known) < 3)
        for char, (enabled, count, positions, adjacent) in self.specials.items():
            maximum, places = known.get(char, (rule.maximum, "sbe"))
            enabled.setChecked(char in known)
            # Move only the checkbox; moving it never changes its configured value.
            self.common_specials.removeWidget(enabled)
            self.extra_specials.removeWidget(enabled)
            if char in known:
                self.common_specials.insertWidget(self.common_specials.count() - 1, enabled)
                enabled.setToolTip("السماح بالعلامة وفق القيود التفصيلية في الإعدادات المتقدمة.")
            else:
                self.extra_specials.addWidget(enabled)
                enabled.setToolTip(
                    "علامة إضافية خارج الإعداد المرجعي؛ قد ترفض المنصة الناتج. اختيارها يفعّل القواعد المخصصة."
                )
            count.setValue(maximum)
            for check, code in zip(positions, "sbe", strict=True):
                check.setChecked(code in places)
            adjacent.setChecked(char in rule.no_repeat)

    def rule(self):
        alphabet = ""
        for check, subset, allowed in self.groups.values():
            if check.isChecked():
                if set(subset.text()) - set(allowed):
                    raise ValueError("المجموعة المخصصة خارج فئة المحارف المحددة.")
                alphabet += subset.text()
        special, no_repeat = [], ""
        for char, (enabled, count, positions, adjacent) in self.specials.items():
            if enabled.isChecked():
                special.append(
                    (
                        char,
                        count.value(),
                        "".join(
                            code for check, code in zip(positions, "sbe", strict=True) if check.isChecked()
                        ),
                    )
                )
                if adjacent.isChecked():
                    no_repeat += char
        return Grammar(
            self.minimum.value(),
            self.maximum.value(),
            alphabet,
            tuple(special),
            self.first.currentData(),
            self.last.currentData(),
            self.prefix.text(),
            self.suffix.text(),
            no_repeat,
        )


class FileGeneratorPanel(QScrollArea):
    idle = Signal()
    finished = Signal(object, str)
    progressed = Signal(object)

    def __init__(self):
        super().__init__()
        self.busy = self.closing = self.loading = False
        self.cancel = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="username-files")
        self.finished.connect(self.done)
        self.progressed.connect(self.progress)
        self.setWidgetResizable(True)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        body = QWidget()
        layout = QVBoxLayout(body)
        title = QLabel("صانع ملفات اليوزرات")
        title.setObjectName("title")
        layout.addWidget(title)
        warning = QLabel(
            "أنشئ كل الأسماء الممكنة من اختياراتك واحفظها في ملف. لا يُفحص توفر الأسماء، "
            "وقد ترفض المنصة بعضها. ندعم الحروف الإنجليزية والأرقام والعلامات المحددة فقط."
        )
        warning.setToolTip(
            "اختلاف الحروف الكبيرة والصغيرة لا يعني دائمًا حسابين مختلفين؛ لا نتحقق من الحجز أو سياسات المنصة."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        root_layout = layout
        self.basic = QGroupBox("الإعدادات الأساسية")
        root_layout.addWidget(self.basic)
        layout = QVBoxLayout(self.basic)
        self.categories = QTabWidget()
        self.platforms = []
        for label, names in (("التواصل الاجتماعي", SOCIAL), ("الألعاب", GAMING)):
            page = QWidget()
            form = QFormLayout(page)
            combo = QComboBox()
            combo.addItems(names)
            style_combo_popup(combo)
            form.addRow("المنصة", combo)
            combo.currentIndexChanged.connect(self.platform_changed)
            self.platforms.append(combo)
            self.categories.addTab(page, label)
        self.categories.currentChanged.connect(self.platform_changed)
        layout.addWidget(self.categories)
        self.variant = QComboBox()
        style_combo_popup(self.variant)
        self.variant.currentIndexChanged.connect(self.restore_defaults)
        identity_form = QFormLayout()
        identity_form.addRow("نوع الاسم", self.variant)
        self.variant.setToolTip(
            "اختر هوية الحساب المقصودة؛ اسم العرض قد لا يكون فريدًا، وبعض المعرّفات مركبة من جزأين."
        )
        layout.addLayout(identity_form)
        self.metadata = QLabel()
        self.metadata.setTextFormat(Qt.TextFormat.PlainText)
        self.metadata.setWordWrap(True)
        layout.addWidget(self.metadata)
        mode_row = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem("قواعد المنصة", "Platform Rules")
        self.mode.addItem("قواعد مخصصة", "Custom Rules")
        self.mode.setToolTip(
            "قواعد المنصة هي الإعدادات المرجعية. تعديل أي خيار يفعّل القواعد المخصصة، مع توضيح الاختلافات."
        )
        style_combo_popup(self.mode)
        mode_row.addWidget(QLabel("القواعد الفعالة"))
        mode_row.addWidget(self.mode)
        self.reset = QPushButton("استعادة إعدادات المنصة")
        self.reset.clicked.connect(self.restore_defaults)
        mode_row.addWidget(self.reset)
        layout.addLayout(mode_row)
        self.primary = RuleEditor("اسم المستخدم")
        self.secondary = RuleEditor("العلامة المرافقة (Tagline)")
        self.secondary.setToolTip(
            "الجزء الذي يأتي بعد # في Riot ID؛ الاسم والعلامة معًا يصنعان المعرّف، لا اسم اللاعب وحده."
        )
        self.primary.changed.connect(self.customized)
        self.secondary.changed.connect(self.customized)
        layout.addWidget(self.primary)
        layout.addWidget(self.secondary)
        self.active_summary = QLabel()
        self.active_summary.setWordWrap(True)
        layout.addWidget(self.active_summary)
        self.advanced_toggle = QPushButton("إعدادات متقدمة ▾")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolTip(
            "إظهار قيود العلامات وبداية الاسم ونهايته والتفاصيل؛ الطي لا يغير أي اختيار."
        )
        root_layout.addWidget(self.advanced_toggle)
        self.advanced = QGroupBox("الإعدادات المتقدمة")
        advanced_layout = QVBoxLayout(self.advanced)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        advanced_layout.addWidget(self.details)
        advanced_layout.addWidget(self.primary.advanced)
        advanced_layout.addWidget(self.secondary.advanced)
        root_layout.addWidget(self.advanced)
        self.advanced.setVisible(False)
        self.advanced_toggle.toggled.connect(self.show_advanced)
        self.rule_warning = QLabel()
        self.rule_warning.setWordWrap(True)
        self.rule_warning.setObjectName("simulation")
        layout.addWidget(self.rule_warning)
        self.mode.currentIndexChanged.connect(self.update_warning)
        output = QFormLayout()
        self.format = QComboBox()
        self.format.addItems(["TXT", "CSV"])
        style_combo_popup(self.format)
        self.destination = QLineEdit()
        self.destination.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.destination.setPlaceholderText("اختر المجلد الرئيسي لحفظ الملفات")
        self.destination.setToolTip("سيُحفظ الملف داخل مجلد Username Finder Bot دون إنشاء مجلد مكرر بداخله.")
        self.choose = QPushButton("اختيار مجلد الحفظ…")
        self.choose.clicked.connect(self.choose_file)
        self.format.setToolTip("TXT: اسم واحد في كل سطر. CSV: جدول بعمود للأسماء يمكن فتحه في برامج الجداول.")
        output.addRow("نوع الملف", self.format)
        output.addRow("المجلد الرئيسي", self.destination)
        output.addRow(self.choose)
        self.suggested_path = QLineEdit()
        self.suggested_path.setReadOnly(True)
        self.suggested_path.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        output.addRow("الملف المقترح", self.suggested_path)
        layout.addLayout(output)
        actions = QHBoxLayout()
        self.preview = QPushButton("حساب عدد الأسماء وحجم الملف")
        self.start = QPushButton("توليد الملف")
        self.start.setObjectName("primary")
        self.resume = QPushButton("استئناف ملف محفوظ…")
        self.stop = QPushButton("إيقاف وحفظ التقدم")
        self.preview.clicked.connect(lambda: self.prepare(False))
        self.start.clicked.connect(lambda: self.prepare(True))
        self.resume.clicked.connect(self.resume_file)
        self.stop.clicked.connect(self.cancel.set)
        for control in (self.preview, self.start, self.resume, self.stop):
            actions.addWidget(control)
        self.summary = QLabel("اختر المنصة واضبط المحارف، ثم احسب الاحتمالات قبل التأكيد.")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        layout.addLayout(actions)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        layout.addWidget(self.bar)
        root_layout.addStretch()
        self.format.currentIndexChanged.connect(self.refresh_description)
        self.destination.textChanged.connect(self.refresh_description)
        self.setWidget(body)
        self.platform_changed()
        self.controls()

    @property
    def platform(self):
        return self.platforms[self.categories.currentIndex()].currentText()

    @property
    def profile(self):
        return variants(self.platform)[self.variant.currentIndex()]

    def platform_changed(self):
        self.variant.blockSignals(True)
        self.variant.clear()
        for profile in variants(self.platform):
            label = "Minecraft Bedrock / " if self.platform == "Minecraft" and profile.key == "xbox" else ""
            self.variant.addItem(label + identifier_labels(profile)[0])
        self.variant.blockSignals(False)
        self.restore_defaults()

    def restore_defaults(self):
        self.loading = True
        profile = self.profile
        self.primary.load(profile.grammar)
        self.secondary.setVisible(profile.tagline is not None)
        self.secondary.advanced.setVisible(profile.tagline is not None)
        self.primary.setTitle(identifier_labels(profile)[1])
        self.primary.advanced.setTitle("تفاصيل " + identifier_labels(profile)[1])
        if profile.tagline:
            self.secondary.load(profile.tagline)
        self.mode.setCurrentIndex(0)
        bounds = (
            f"{profile.grammar.minimum}–{profile.grammar.maximum}" if profile.bounds_known else "غير مثبتة"
        )
        self.metadata.setText(
            f"{identifier_labels(profile)[0]} • {profile.uniqueness}\n"
            f"القواعد: {CONFIDENCE[profile.confidence]} ({profile.confidence}) • عدد الخانات: {bounds}"
        )
        self.details.setText(
            f"{profile.note}\nالمصدر: {profile.source}\n"
            "البداية والنهاية الثابتة جزء من الطول المختار، وليستا شرطين إضافيين للمنصة."
        )
        self.loading = False
        self.update_warning()

    def customized(self):
        if not self.loading:
            self.mode.setCurrentIndex(1)
            self.update_warning()

    def show_advanced(self, expanded):
        self.advanced.setVisible(expanded)
        self.advanced_toggle.setText("إخفاء الإعدادات المتقدمة ▴" if expanded else "إعدادات متقدمة ▾")

    def refresh_description(self):
        if self.loading:
            return
        try:
            rule = self.primary.rule()
        except ValueError as exc:
            self.active_summary.setText(str(exc))
            self.suggested_path.clear()
            return
        groups = [check.text() for check, _, _ in self.primary.groups.values() if check.isChecked()]
        self.active_summary.setText(
            f"{self.platform} • {identifier_labels(self.profile)[0]} • {rule.minimum}–{rule.maximum} خانة\n"
            f"{self.mode.currentText()} • " + "، ".join(groups)
        )
        try:
            path = (
                proposed_output(self.destination.text(), self.config())
                if self.destination.text().strip()
                else ""
            )
            self.suggested_path.setText(str(path))
            self.suggested_path.setToolTip(str(path))
        except (OSError, ValueError):
            self.suggested_path.clear()
        if not self.busy:
            self.summary.setText("اضغط «حساب عدد الأسماء وحجم الملف» لتحديث العدد بعد تعديل اختياراتك.")

    def issues(self):
        result = violations(self.profile, self.primary.rule())
        if self.profile.tagline:
            result += violations(self.profile, self.secondary.rule(), self.profile.tagline)
        return result

    def update_warning(self):
        if self.loading:
            return
        try:
            issues = self.issues()
            if self.mode.currentIndex() == 0 and self.profile.confidence == "Incomplete":
                issues.append("القواعد غير مكتملة؛ اختر «قواعد مخصصة». لا نضمن قبول المنصة للأسماء الناتجة.")
            if self.profile.confidence == "Provisional":
                issues.append("هذه القواعد مؤقتة، ولا تثبت قبول المنصة.")
            self.rule_warning.setText(
                "\n".join(dict.fromkeys(issues)) or "تُطبق الصيغة المرجعية المحددة فقط؛ لا إثبات إتاحة أو حجز."
            )
        except ValueError as exc:
            self.rule_warning.setText(str(exc))
        self.refresh_description()

    def config(self):
        if self.profile.confidence == "Incomplete" and self.mode.currentIndex() == 0:
            raise ValueError(
                "القواعد غير مكتملة؛ اختر «قواعد مخصصة» قبل التوليد. قد ترفض المنصة هذه الأسماء."
            )
        if self.mode.currentIndex() == 0 and self.issues():
            raise ValueError(
                "الإعداد يخالف قواعد المنصة؛ اختر «قواعد مخصصة» مع التحذير أو استعد إعدادات المنصة."
            )
        if self.mode.currentIndex() == 0 and (
            self.primary.rule() != self.profile.grammar
            or (self.profile.tagline and self.secondary.rule() != self.profile.tagline)
        ):
            raise ValueError(
                "الإعداد مخصص؛ اختر «قواعد مخصصة» أو استعد إعدادات المنصة. لم تتغير اختياراتك تلقائيًا."
            )
        return {
            "platform": self.platform,
            "category": self.categories.tabText(self.categories.currentIndex()),
            "profile": self.profile.key,
            "confidence": self.profile.confidence,
            "mode": self.mode.currentData(),
            "primary": self.primary.rule().data(),
            "secondary": self.secondary.rule().data() if self.profile.tagline else None,
            "format": self.format.currentText().lower(),
        }

    def choose_file(self):
        path = QFileDialog.getExistingDirectory(self, "اختر المجلد الرئيسي لحفظ الملفات")
        if path:
            self.destination.setText(path)

    def submit(self, work, callback):
        if self.busy or self.closing:
            return
        self.busy = True
        self.callback = callback
        self.controls()
        future = self.executor.submit(work)

        def done(future):
            try:
                result, error = future.result(), ""
            except Exception as exc:
                result, error = None, friendly_error(exc)
            self.finished.emit(result, error)

        future.add_done_callback(done)

    def done(self, result, error):
        self.busy = False
        if error:
            self.summary.setText(
                "توقف العمل: " + error + "\nلم يُستبدل ملف موجود؛ تبقى آخر نقطة حفظ صالحة للاستئناف إن وُجدت."
            )
        elif not self.closing:
            self.callback(result)
        self.controls()
        if not self.busy:
            if self.closing:
                self.executor.shutdown(wait=False)
            self.idle.emit()

    def prepare(self, start):
        if self.busy:
            return
        try:
            config, parent = self.config(), self.destination.text().strip()
        except ValueError as exc:
            self.summary.setText(str(exc))
            return
        self.cancel.clear()

        def ready(space):
            resource_warning = ""
            destination = ""
            if parent:
                try:
                    destination = proposed_output(parent, config)
                    check_capacity(parent, space.bytes)
                    self.suggested_path.setText(str(destination))
                    self.suggested_path.setToolTip(str(destination))
                except (OSError, ValueError) as exc:
                    resource_warning = "\n" + friendly_error(exc)
            text = (
                f"{self.active_summary.text()}\nالعدد الدقيق للأسماء: {space.total:,}\n"
                f"حجم الملف المحسوب: {space.bytes:,} بايت • {config['format'].upper()}\n{destination}\n"
                f"{self.rule_warning.text()}\nالأعداد الكبيرة تحتاج وقتًا ومساحة. لا يُفحص توفر الأسماء."
                + resource_warning
            )
            self.summary.setText(text)
            if start and not self.closing and not resource_warning:
                if not parent:
                    self.summary.setText("اختر المجلد الرئيسي لحفظ الملفات أولًا.")
                elif (
                    QMessageBox.question(
                        self,
                        "تأكيد توليد الملف",
                        text + "\nهل تريد المتابعة؟",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    == QMessageBox.StandardButton.Yes
                ):

                    def write():
                        create_output_directory(parent, destination)
                        return generate_file(
                            config, destination, cancel=self.cancel, notify=self.progressed.emit
                        )

                    self.submit(write, self.progress)

        self.submit(lambda: IdentifierSpace(config, self.cancel), ready)

    def progress(self, record):
        total = record["total"]
        self.bar.setValue(record["index"] * 1000 // total if total else 0)
        state = "اكتمل" if record["state"] == "Completed" else "تقدم محفوظ"
        self.suggested_path.setText(record["destination"])
        self.suggested_path.setToolTip(record["destination"])
        self.summary.setText(
            f"{state}: {record['index']:,} / {total:,} • {record['offset']:,} بايت\n"
            f"{record['destination']}\nيمكن استئناف العمل من ملف الحفظ generation.json دون تكرار الأسماء."
        )

    def resume_file(self):
        destination, _ = QFileDialog.getOpenFileName(
            self, "اختر الملف أو ملف .part أو .generation.json المحفوظ", "", "All files (*)"
        )
        if not destination:
            return
        for suffix in (".generation.json", ".part"):
            if destination.endswith(suffix):
                destination = destination[: -len(suffix)]
                break
        try:
            record = read_journal(destination)
        except (OSError, ValueError) as exc:
            self.summary.setText("تعذر قراءة ملف الاستئناف: " + str(exc))
            return
        config = record["config"]
        if not {"platform", "category", "profile", "mode", "format", "primary"} <= set(config):
            self.summary.setText("ملف الاستئناف لا يتضمن إعداد واجهة صالحًا؛ لم يُعدّل الملف.")
            return
        if config["platform"] not in SOCIAL + GAMING:
            self.summary.setText("منصة ملف الاستئناف غير معتمدة.")
            return
        text = f"استئناف {config['platform']} عند {record['index']:,} بنفس القواعد المحفوظة؟\n{destination}"
        if (
            QMessageBox.question(
                self,
                "تأكيد الاستئناف",
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.cancel.clear()
        self.categories.setCurrentIndex(
            0 if config["platform"] in SOCIAL and config["category"] == "التواصل الاجتماعي" else 1
        )
        self.platforms[self.categories.currentIndex()].setCurrentText(config["platform"])
        selected = next(
            (i for i, p in enumerate(variants(self.platform)) if p.key == config["profile"]), None
        )
        if selected is None:
            self.summary.setText("الملف يحوي ملف قواعد غير معروف؛ لم يبدأ الاستئناف.")
            return
        self.variant.setCurrentIndex(selected)
        self.loading = True
        self.primary.load(Grammar.from_data(config["primary"]))
        if config.get("secondary"):
            self.secondary.load(Grammar.from_data(config["secondary"]))
        self.mode.setCurrentIndex(self.mode.findData(config["mode"]))
        self.format.setCurrentText(config["format"].upper())
        self.destination.setText(str(Path(destination).parent))
        self.loading = False
        self.update_warning()
        self.suggested_path.setText(destination)
        self.suggested_path.setToolTip(destination)
        self.submit(
            lambda: generate_file(
                config, destination, cancel=self.cancel, resume=True, notify=self.progressed.emit
            ),
            self.progress,
        )

    def controls(self):
        for control in (
            self.categories,
            self.variant,
            self.mode,
            self.reset,
            self.primary,
            self.secondary,
            self.advanced,
            self.advanced_toggle,
            self.format,
            self.destination,
            self.choose,
            self.preview,
            self.start,
            self.resume,
        ):
            control.setEnabled(not self.busy and not self.closing)
        self.stop.setEnabled(self.busy)

    def shutdown(self):
        self.closing = True
        self.cancel.set()
        if not self.busy:
            self.executor.shutdown(wait=False)
