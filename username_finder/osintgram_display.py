"""Arabic presentation only; original result keys and export values stay intact."""

from __future__ import annotations

MISSING = object()

FIELD_LABELS = {
    "id": "معرّف الحساب",
    "pk": "معرّف الحساب",
    "user_id": "معرّف الحساب",
    "username": "اسم المستخدم",
    "full_name": "الاسم الكامل",
    "biography": "النبذة",
    "follower_count": "عدد المتابعين",
    "following_count": "عدد الحسابات المتابَعة",
    "media_count": "عدد المنشورات",
    "is_private": "حساب خاص",
    "is_verified": "حساب موثّق",
    "is_business": "حساب تجاري",
    "is_memorialized": "حساب تذكاري",
    "account_type": "نوع الحساب",
    "profile_pic_url": "صورة الحساب",
    "profile_pic_url_hd": "صورة الحساب عالية الدقة",
    "public_email": "البريد الإلكتروني المعلن",
    "email": "البريد الإلكتروني",
    "contact_phone_number": "رقم هاتف التواصل",
    "public_phone_number": "رقم الهاتف المعلن",
    "public_phone_country_code": "رمز الدولة للهاتف المعلن",
    "phone": "رقم الهاتف",
    "whatsapp_number": "رقم واتساب المعلن",
    "business_contact_method": "طريقة التواصل التجاري",
    "external_url": "الرابط الخارجي",
    "bio_links": "روابط النبذة",
    "title": "العنوان",
    "url": "الرابط",
    "lynx_url": "رابط التوجيه عبر Instagram",
    "thumbnail_url": "رابط الصورة المصغرة",
    "media_url": "رابط ملف الوسائط",
    "permalink": "رابط المنشور",
    "slides": "عناصر الألبوم",
    "like_count": "عدد الإعجابات",
    "comment_count": "عدد التعليقات",
    "play_count": "عدد المشاهدات",
    "caption": "نص المنشور",
    "coauthors": "الحسابات المشاركة في النشر",
    "paid_partnership": "شراكة مدفوعة",
    "audio": "المقطع الصوتي",
    "author": "الحساب الناشر",
    "category": "تصنيف الحساب",
    "category_name": "اسم تصنيف الحساب",
    "business_category_name": "التصنيف التجاري",
    "pronouns": "الضمائر المعروضة",
    "fbid_v2": "معرّف Facebook المرتبط",
    "address_street": "عنوان الشارع",
    "city_name": "المدينة",
    "city": "المدينة",
    "zip": "الرمز البريدي",
    "latitude": "خط العرض",
    "longitude": "خط الطول",
    "lat": "خط العرض",
    "lng": "خط الطول",
    "instagram_location_id": "معرّف الموقع في Instagram",
    "country": "البلد",
    "date_joined": "تاريخ الانضمام",
    "total_clips_count": "عدد مقاطع ريلز",
    "total_igtv_videos": "عدد فيديوهات IGTV",
    "usertags_count": "عدد الإشارات إلى الحساب",
    "highlight_reel_count": "عدد مجموعات القصص البارزة",
    "hashtag": "الوسم",
    "count": "العدد",
    "counter": "عدد مرات الظهور",
    "address": "العنوان الجغرافي",
    "name": "الاسم",
    "time": "الوقت",
    "comment": "التعليق",
    "post_id": "معرّف المنشور",
    "comment_counter": "مجموع التعليقات",
    "like_counter": "مجموع الإعجابات",
    "posts": "عدد المنشورات",
    "min": "أقل قيمة في العينة",
    "max": "أعلى قيمة في العينة",
    "avg": "المتوسط في العينة",
    "previews": "معاينات المنشورات",
    "carousels": "منشورات الألبومات",
    "collaborations": "المنشورات المشتركة",
    "paid_partnerships": "الشراكات المدفوعة",
    "photos": "الصور",
    "videos": "الفيديوهات",
    "total": "المجموع",
    "video_plays_avg": "متوسط مشاهدات الفيديو",
    "video_plays_total": "مجموع مشاهدات الفيديو",
    "avg_days_between_posts": "متوسط الأيام بين المنشورات",
    "by_hour": "التوزيع حسب الساعة",
    "by_weekday": "التوزيع حسب يوم الأسبوع",
    "first_post": "أول منشور في العينة",
    "last_post": "آخر منشور في العينة",
    "most_active_day": "اليوم الأكثر نشاطًا",
    "most_active_hour": "الساعة الأكثر نشاطًا",
    "timestamps": "الأوقات المسجلة",
    "timezone": "المنطقة الزمنية",
    "description": "الوصف",
    "descriptions": "الأوصاف النصية",
    "photos_scanned": "عدد الصور المفحوصة",
    "with_alt_text": "عدد الصور ذات الوصف البديل",
    "taken_at": "وقت النشر",
    "created_at": "تاريخ الإنشاء",
    "last_updated": "آخر تحديث",
    "media_type": "نوع الوسائط",
    "authors": "الحسابات الناشرة",
    "page_id": "معرّف صفحة النتائج",
    "max_id": "مؤشر صفحة النتائج التالية",
    "place": "المكان",
    "alternatives": "الأماكن البديلة",
    "target": "الحساب المستهدف",
    "other": "الحساب الآخر",
    "common_followers": "المتابعون المشتركون",
    "common_followings": "الحسابات المتابَعة المشتركة",
    "scanned": "أعداد الحسابات المفحوصة",
    "target_followers": "متابعو الحساب المستهدف المفحوصون",
    "other_followers": "متابعو الحساب الآخر المفحوصون",
    "target_followings": "الحسابات التي يتابعها الهدف والمفحوصة",
    "other_followings": "الحسابات التي يتابعها الحساب الآخر والمفحوصة",
    "scope": "نطاق المقارنة",
    "sort": "الترتيب",
    "error": "خطأ من المحرك",
    "Monday": "الاثنين",
    "Tuesday": "الثلاثاء",
    "Wednesday": "الأربعاء",
    "Thursday": "الخميس",
    "Friday": "الجمعة",
    "Saturday": "السبت",
    "Sunday": "الأحد",
}

COUNTERS = {
    "follower_count",
    "following_count",
    "media_count",
    "total_clips_count",
    "total_igtv_videos",
    "usertags_count",
    "highlight_reel_count",
    "count",
    "counter",
    "comment_counter",
    "like_counter",
    "posts",
    "min",
    "max",
    "total",
    "carousels",
    "collaborations",
    "paid_partnerships",
    "photos",
    "videos",
    "video_plays_total",
    "photos_scanned",
    "with_alt_text",
    "like_count",
    "comment_count",
    "play_count",
    "target_followers",
    "other_followers",
    "target_followings",
    "other_followings",
}
LINKS = {
    "external_url",
    "profile_pic_url",
    "profile_pic_url_hd",
    "url",
    "lynx_url",
    "thumbnail_url",
    "media_url",
    "permalink",
}


def field_label(key, command=""):
    if key == "id" and command in ("get_user_photos", "get_user_stories", "get_highlights", "post_preview"):
        return {
            "get_user_photos": "معرّف المنشور",
            "get_user_stories": "معرّف القصة",
            "get_highlights": "معرّف مجموعة القصص",
            "post_preview": "معرّف المنشور",
        }[command]
    if key == "counter" and command in ("get_people_who_commented", "get_people_who_tagged"):
        return "عدد التعليقات" if command == "get_people_who_commented" else "عدد الإشارات"
    return FIELD_LABELS.get(key, key)


def field_hint(key):
    if key == "account_type":
        return "رمز كما ورد من المصدر؛ لا تتوفر دلالة موثقة لقيمه الرقمية في هذا التكامل."
    return "معرّف الحقل الأصلي: " + key


def dataset_label(path):
    return " / ".join(field_label(part) for part in path.split("."))


def display_value(value, field=""):
    if value is MISSING:
        return "غير موجود في الاستجابة"
    if value is None:
        return "غير معروف (null)" if field.startswith("is_") else "غير متوفر (null)"
    if isinstance(value, bool):
        return "نعم" if value else "لا"
    if value == "":
        if field == "biography":
            return "لا توجد نبذة"
        if field in LINKS:
            return "لا يوجد رابط"
        return "لا توجد قيمة نصية"
    if isinstance(value, (dict, list)):
        if not value:
            return "لا توجد نتائج (قائمة فارغة)" if isinstance(value, list) else "لا توجد تفاصيل (كائن فارغ)"
        return (
            f"{'تفاصيل' if isinstance(value, dict) else 'قائمة نتائج'} ({len(value)}) — راجع التفاصيل أو JSON"
        )
    if isinstance(value, int) and field in COUNTERS:
        return f"{value:,}"
    if field == "media_type" and isinstance(value, str):
        return {"photo": "صورة", "video": "فيديو", "carousel": "ألبوم", "other": "نوع آخر"}.get(value, value)
    text = str(value)
    if field == "account_type" and isinstance(value, (int, float)):
        text += " — رمز من المصدر"
    return text[:2000] + ("…" if len(text) > 2000 else "")
