import csv
import json
from copy import deepcopy

import pytest

from username_finder.osintgram_display import MISSING, dataset_label, display_value, field_hint, field_label
from username_finder.osintgram_results import export_csv, export_json, json_preview, table_data


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("id", "معرّف الحساب"),
        ("pk", "معرّف الحساب"),
        ("username", "اسم المستخدم"),
        ("full_name", "الاسم الكامل"),
        ("biography", "النبذة"),
        ("follower_count", "عدد المتابعين"),
        ("following_count", "عدد الحسابات المتابَعة"),
        ("media_count", "عدد المنشورات"),
        ("is_private", "حساب خاص"),
        ("is_verified", "حساب موثّق"),
        ("is_business", "حساب تجاري"),
        ("account_type", "نوع الحساب"),
        ("profile_pic_url_hd", "صورة الحساب عالية الدقة"),
        ("public_email", "البريد الإلكتروني المعلن"),
        ("public_phone_number", "رقم الهاتف المعلن"),
        ("external_url", "الرابط الخارجي"),
        ("category", "تصنيف الحساب"),
        ("comment_counter", "مجموع التعليقات"),
    ],
)
def test_arabic_field_labels(key, label):
    assert field_label(key) == label


def test_field_context_and_unknown_fields_are_not_invented():
    assert field_label("id", "get_user_photos") == "معرّف المنشور"
    assert field_label("id", "post_preview") == "معرّف المنشور"
    assert field_label("counter", "get_people_who_commented") == "عدد التعليقات"
    assert field_label("counter", "get_people_who_tagged") == "عدد الإشارات"
    assert field_label("unfamiliar_field") == "unfamiliar_field"
    assert "unfamiliar_field" in field_hint("unfamiliar_field")
    assert dataset_label("bio_links") == "روابط النبذة"
    assert dataset_label("unfamiliar.rows") == "unfamiliar / rows"
    assert "2" in display_value(2, "account_type") and "رمز" in display_value(2, "account_type")
    assert "لا تتوفر دلالة موثقة" in field_hint("account_type")


def test_contextual_empty_and_boolean_values_preserve_all_distinctions():
    assert display_value("", "biography") == "لا توجد نبذة"
    assert display_value("", "external_url") == "لا يوجد رابط"
    assert display_value(None, "is_private") == "غير معروف (null)"
    assert len({display_value(v) for v in (MISSING, None, "", [], {}, 0, False)}) == 7
    assert display_value(False, "follower_count") == "لا"
    assert display_value(True, "is_private") == "نعم"
    assert display_value(0, "follower_count") == "0"
    assert display_value(1234567, "follower_count") == "1,234,567"
    assert display_value(9876543210123456789, "id") == "9876543210123456789"
    assert display_value("00120", "public_phone_number") == "00120"


def test_localization_does_not_mutate_projection_json_or_csv(tmp_path):
    original = {
        "id": 9876543210123456789,
        "biography": "",
        "is_private": False,
        "follower_count": 10000,
        "account_type": 2,
        "external_url": None,
    }
    value = deepcopy(original)
    for key, item in value.items():
        field_label(key)
        display_value(item, key)
    assert value == original and json.loads(json_preview(value)) == original
    headers, rows = table_data([value])
    assert headers == list(original) and rows == [list(original.values())]
    export_json(tmp_path / "raw.json", value)
    assert json.loads((tmp_path / "raw.json").read_text(encoding="utf-8")) == original
    export_csv(tmp_path / "raw.csv", [value])
    with (tmp_path / "raw.csv").open(encoding="utf-8-sig", newline="") as stream:
        exported = list(csv.reader(stream))
    assert [json.loads(cell) for cell in exported[0]] == list(original)
    assert [json.loads(cell) for cell in exported[1]] == list(original.values())
