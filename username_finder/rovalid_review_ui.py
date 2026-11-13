"""Explicit local review dialog; never starts checks or clears restrictions."""

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
)

from . import rovalid, rovalid_review
from .ui_cards import caption


def review_dialog(parent, database, mode, connection):
    status = rovalid.permission(database, mode=mode, connection=connection)
    dialog = QDialog(parent)
    dialog.setWindowTitle("مراجعة قيد RoValid — لا إرسال")
    dialog.setMinimumWidth(420)
    form = QFormLayout(dialog)
    form.addRow(caption(status["reason"]))
    form.addRow(
        caption(
            "راجع ملفات العملية الأصلية. سجّل مرجع الدليل ونطاقه؛ لا تكتب أسرارًا. "
            "ردود CLI وأرقام HTTP 200 لا تثبت السعة أو الإتاحة أو زوال القيد."
        )
    )
    fingerprint = status.get("fingerprint", "")
    form.addRow(caption("بصمة الحالة: " + fingerprint[:16]))
    reviewer, evidence = QLineEdit(), QLineEdit()
    reviewer.setMaxLength(100)
    evidence.setMaxLength(1000)
    form.addRow("اسم المراجع", reviewer)
    form.addRow("مرجع الدليل ونطاقه وسبب القرار", evidence)
    decision = QComboBox()
    decision.addItem("تسجيل المراجعة فقط — بلا تفويض", "review")
    decision.addItem("رفض التشغيل لهذا النطاق", "deny")
    if status.get("local_allowed"):
        decision.addItem("تفويض صريح لهذا النمط والاتصال والدليل فقط", "authorize")
    form.addRow("القرار", decision)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    form.addRow(buttons)
    buttons.rejected.connect(dialog.reject)

    def save():
        if not reviewer.text().strip() or not evidence.text().strip() or not fingerprint:
            QMessageBox.warning(dialog, "مراجعة غير مكتملة", "يلزم مراجع ومرجع دليل وحالة سليمة.")
            return
        if (
            decision.currentData() == "authorize"
            and QMessageBox.question(
                dialog,
                "تأكيد قرار مستقل",
                "هل تفوض النطاق المعروض بعد مراجعة الدليل؟ "
                "لا يُحرر ذلك أي قيد محفوظ، ولا يبدأ التشغيل تلقائيًا.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            rovalid_review.record_review(
                database,
                mode=mode,
                connection=connection,
                decision=decision.currentData(),
                reviewer=reviewer.text(),
                evidence=evidence.text(),
                fingerprint=fingerprint,
            )
        except (ValueError, OSError):
            QMessageBox.warning(dialog, "لم يُحفظ القرار", "تغير الدليل أو تعذر تدقيقه؛ أعد عرض الحالة.")
            return
        dialog.accept()

    buttons.accepted.connect(save)
    return dialog.exec()
