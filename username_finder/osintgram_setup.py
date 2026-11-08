"""Account and external installation controls; login account is never the analysis target."""

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .osintgram_credentials import CredentialError


def text_label(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class AccountSetup(QWidget):
    updated = Signal()

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.analysis_running = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        installation = QGroupBox("برنامج التحليل المحلي")
        folder_layout = QVBoxLayout(installation)
        self.directory = QLineEdit(str(manager.directory))
        self.directory.setReadOnly(True)
        self.directory.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.directory, 1)
        self.browse = QPushButton("اختيار المجلد…")
        self.browse.clicked.connect(self.choose_directory)
        folder_row.addWidget(self.browse)
        folder_layout.addLayout(folder_row)
        folder_layout.addWidget(
            text_label("يبدأ البرنامج محليًا عند فتح التحليل. لا تُثبّت حزم ولا تُشغّل خدمة على الشبكة العامة.")
        )
        layout.addWidget(installation)
        account = QGroupBox("إعداد حساب Instagram للتحليل")
        form_layout = QVBoxLayout(account)
        form_layout.addWidget(
            text_label(
                "يُستخدم هذا الحساب للاتصال بـInstagram وجلب المعلومات. يُفضّل حساب ثانوي، "
                "فقد يطلب Instagram تأكيد الدخول أو يفرض قيودًا مؤقتة. هذا حساب الدخول، "
                "وليس الحساب المطلوب تحليله."
            )
        )
        form = QFormLayout()
        self.username = QLineEdit(str(manager.settings.value("login_username", "")))
        self.username.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.username.setMaximumWidth(410)
        self.username.setMaxLength(30)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.password.setMaximumWidth(410)
        self.password.setMaxLength(1000)
        self.password.setPlaceholderText("أدخل كلمة المرور لحفظ الحساب أو تحديثها")
        self.show_password = QCheckBox("إظهار كلمة المرور")
        self.show_password.toggled.connect(
            lambda show: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
            )
        )
        form.addRow("حساب الدخول", self.username)
        form.addRow("كلمة المرور", self.password)
        form.addRow("", self.show_password)
        form_layout.addLayout(form)
        actions = QHBoxLayout()
        self.save = QPushButton("حفظ / تحديث الحساب")
        self.login = QPushButton("تسجيل الدخول / تجديد الجلسة")
        self.remove = QPushButton("حذف بيانات الدخول المحفوظة")
        self.cancel = QPushButton("إلغاء تسجيل الدخول")
        self.save.clicked.connect(self.save_account)
        self.login.clicked.connect(manager.login)
        self.remove.clicked.connect(self.remove_account)
        self.cancel.clicked.connect(manager.cancel_login)
        # Two compact rows remain usable at 900 logical pixels.
        for button in (self.save, self.login):
            actions.addWidget(button)
        actions.addStretch()
        form_layout.addLayout(actions)
        secondary = QHBoxLayout()
        secondary.addWidget(self.remove)
        secondary.addWidget(self.cancel)
        secondary.addStretch()
        form_layout.addLayout(secondary)
        self.status = text_label("المحرك والجلسة حالتان منفصلتان؛ تشغيل البرنامج لا يثبت نجاح تسجيل الدخول.")
        form_layout.addWidget(self.status)
        form_layout.addWidget(
            text_label(
                "تُحفظ كلمة المرور في Windows Credential Manager. تُمرّر في بيئة العملية "
                "التابعة فقط عند الحاجة؛ هذه الذاكرة ليست مخزنًا مشفرًا. تبقى الجلسة "
                "الحساسة داخل Osintgram. حذف بيانات الدخول هنا لا يلغي الجلسة لدى Instagram."
            )
        )
        layout.addWidget(account)
        layout.addStretch()
        manager.auth_event.connect(self.auth_status)
        manager.changed.connect(lambda *_: self.refresh())
        manager.idle.connect(self.refresh)
        self.refresh()

    def refresh(self):
        blocked = (
            self.analysis_running
            or self.manager.busy
            or self.manager.closing
            or self.manager.state in ("checking", "validating", "starting", "stopping")
        )
        for widget in (
            self.username,
            self.password,
            self.show_password,
            self.save,
            self.browse,
            self.remove,
            self.login,
        ):
            widget.setEnabled(not blocked)
        self.cancel.setEnabled(bool(self.manager.auth and self.manager.auth.active))

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "مجلد تثبيت Osintgram", str(self.manager.directory))
        if path:
            from pathlib import Path

            if not (Path(path) / "src/web/app.py").is_file():
                self.status.setText("هذا المجلد لا يحتوي خادم Osintgram المتوقع.")
                return
            self.manager.settings.setValue("directory", path)
            self.manager.settings.sync()
            self.directory.setText(path)
            self.manager.restart_owned()
            self.updated.emit()

    def save_account(self):
        username = self.username.text().strip().removeprefix("@").lower()
        password = self.password.text()
        if not re.fullmatch(r"[a-z0-9._]{1,30}", username) or not password:
            self.status.setText("أدخل اسم حساب الدخول وكلمة المرور لحفظهما أو تحديثهما.")
            return
        try:
            self.manager.credentials.save(username, password)
        except CredentialError:
            self.status.setText(str(CredentialError()))
            return
        self.manager.settings.setValue("login_username", username)
        self.manager.settings.sync()
        self.password.clear()
        self.show_password.setChecked(False)
        self.status.setText("حُفظ الحساب بأمان. اضغط «تسجيل الدخول / تجديد الجلسة» للتحقق من الحساب والجلسة.")
        self.manager.restart_owned()
        self.updated.emit()

    def remove_account(self):
        choice = QMessageBox.question(
            self,
            "حذف بيانات الدخول",
            "حذف كلمة المرور المحفوظة وإيقاف المحرك الذي شغّله التطبيق؟ "
            "ستبقى جلسة Osintgram؛ هذا لا يسجّل خروج الحساب من Instagram.",
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.credentials.delete()
        except CredentialError:
            self.status.setText(str(CredentialError()))
            return
        for name in ("login_username", "session_account"):
            self.manager.settings.remove(name)
        self.manager.settings.sync()
        self.username.clear()
        self.password.clear()
        self.show_password.setChecked(False)
        self.manager.restart_owned(restart=False)
        self.status.setText(
            "حُذفت بيانات الدخول من Windows Credential Manager. الجلسة الخارجية لم تُحذف أو تُلغَ."
        )
        self.updated.emit()

    def auth_status(self, kind):
        messages = {
            "authenticating": "جارٍ التحقق من حساب الدخول؛ لا تغلق النافذة…",
            "authenticated": "نجح تسجيل الدخول؛ ننتظر اكتمال حفظ الجلسة.",
            "session_saved": "حُفظت الجلسة بنجاح داخل Osintgram. يمكنك اختيار الحساب المطلوب تحليله.",
            "credentials_missing": "احفظ حساب الدخول وكلمة المرور أولًا.",
            "configuration_missing": "اختر مجلد Osintgram وبيئة Python الصحيحة أولًا.",
            "authentication_failed": (
                "تعذر تسجيل الدخول أو انتهت الجلسة. تحقق من البيانات وأي إشعار أرسله Instagram؛ "
                "لم تُستبدل جلسة سابقة عند الفشل."
            ),
            "challenge_required": (
                "يطلب Instagram تحققًا لا تستطيع المكتبة إكماله هنا. أكمل التأكيد عبر التطبيق الرسمي "
                "ثم أعد المحاولة؛ لا نتجاوز CAPTCHA أو قيود Instagram."
            ),
            "authentication_cancelled": (
                "أُلغي تسجيل الدخول أو انتهت مهلته؛ بقيت بيانات الدخول والجلسة السابقة محفوظتين."
            ),
        }
        if kind in ("challenge", "two_factor"):
            self.status.setText("أدخل رمز التحقق الذي تلقيته من Instagram؛ لن يُحفظ.")
            code, accepted = QInputDialog.getText(
                self, "تأكيد تسجيل الدخول", "رمز التحقق من Instagram", QLineEdit.EchoMode.Password
            )
            if accepted and code.strip():
                self.manager.submit_code(code)
            else:
                self.manager.cancel_login()
        else:
            self.status.setText(messages.get(kind, "لم يُؤكد تسجيل الدخول."))
        self.refresh()
        self.updated.emit()
