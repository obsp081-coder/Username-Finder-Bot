"""Offline identifier profiles; confidence applies to documented syntax, never availability."""

from __future__ import annotations

import string
from dataclasses import asdict, dataclass

LOWER, UPPER, DIGITS = string.ascii_lowercase, string.ascii_uppercase, string.digits
SOCIAL = (
    "Instagram",
    "X / Twitter",
    "TikTok",
    "Snapchat",
    "Sony / PlayStation",
    "Xbox",
    "Tellonym",
    "Discord",
    "Telegram",
    "JACO",
    "Twitch",
    "Steam",
)
GAMING = (
    "PUBG Mobile",
    "Fortnite",
    "Valorant",
    "FIFA",
    "GTA V",
    "Clash of Clans",
    "Clash Royale",
    "Call of Duty",
    "Apex Legends",
    "Rainbow Six Siege",
    "Genshin Impact",
    "Discord",
    "Steam",
    "Origin",
    "Ubisoft",
    "Rockstar",
    "CS:GO",
    "League of Legends",
    "8 Ball Pool",
    "Overwatch",
    "Rocket League",
    "Roblox",
    "Minecraft",
)


@dataclass(frozen=True)
class Grammar:
    minimum: int = 3
    maximum: int = 3
    alphabet: str = LOWER + DIGITS
    # (character, maximum occurrences, allowed position classes: start/body/end)
    specials: tuple = ()
    first: str = "any"
    last: str = "any"
    prefix: str = ""
    suffix: str = ""
    no_repeat: str = ""

    def data(self):
        return asdict(self)

    @classmethod
    def from_data(cls, data):
        return cls(**dict(data, specials=tuple(tuple(row) for row in data.get("specials", []))))


@dataclass(frozen=True)
class Profile:
    key: str
    identifier: str
    uniqueness: str
    confidence: str
    grammar: Grammar
    note: str
    source: str = "User-supplied research; current full syntax not independently established"
    tagline: Grammar | None = None
    bounds_known: bool = True


def grammar(low, high, specials="", *, upper=False, first="any", last="any", no_repeat=""):
    return Grammar(
        low,
        high,
        LOWER + (UPPER if upper else "") + DIGITS,
        tuple((c, high, "sbe") for c in specials),
        first,
        last,
        no_repeat=no_repeat,
    )


PROFILES = {
    "discord": Profile(
        "discord",
        "Discord username",
        "فريد عالميًا؛ دون discriminator القديم",
        "Verified",
        grammar(2, 32, "_.", no_repeat="."),
        "صيغة ASCII موثقة؛ الأسماء المحجوزة والاعتدال لا يُحسمان محليًا.",
        "https://support.discord.com/hc/en-us/articles/12620128861463",
    ),
    "roblox": Profile(
        "roblox",
        "Roblox username",
        "فريد؛ مختلف عن اسم العرض",
        "Verified",
        Grammar(3, 20, LOWER + UPPER + DIGITS, (("_", 1, "b"),)),
        "قواعد الصيغة المحلية الحالية؛ لا تحقق من الاعتدال أو الإتاحة.",
        "https://en.help.roblox.com/hc/en-us/articles/4412614080532-Xbox-Error-901",
    ),
    "x": Profile(
        "x",
        "X handle",
        "فريد؛ قد يكون محجوزًا",
        "Provisional",
        grammar(5, 15, "_", upper=True),
        "الأحرف والحد الأعلى موثقان؛ الحد الأدنى 5 من سياق تغيير الاسم. قيود الأسماء المحجوزة غير شاملة.",
        "https://help.x.com/en/managing-your-account/x-username-rules",
    ),
    "instagram": Profile(
        "instagram",
        "Instagram username",
        "فريد",
        "Provisional",
        Grammar(1, 30, LOWER + DIGITS, (("_", 30, "sbe"), (".", 30, "b")), no_repeat="."),
        "الحد الأدنى وقواعد النقاط مؤقتة؛ الإعداد مجموعة ASCII محافظة لا كامل الصيغ المقبولة.",
    ),
    "tiktok": Profile(
        "tiktok",
        "TikTok username",
        "فريد",
        "Provisional",
        Grammar(2, 24, LOWER + DIGITS, (("_", 24, "sbe"), (".", 24, "sb"))),
        "منع النقطة النهائية موثق؛ مجال الطول 2–24 مؤقت، ومجموعة ASCII ليست شاملة.",
        "https://artists.tiktok.com/help-center/change-username",
    ),
    "snapchat": Profile(
        "snapchat",
        "Snapchat username",
        "فريد",
        "Provisional",
        grammar(3, 15, "_.-", first="letter", last="alnum"),
        "حدود وبداية ونهاية من البحث السابق؛ لا يُفرض حد رمز خاص واحد دون دليل.",
    ),
    "psn": Profile(
        "psn",
        "PSN Online ID",
        "معرّف حساب",
        "Provisional",
        grammar(3, 16, "_-", upper=True, first="letter"),
        "قواعد 3–16 والبداية بحرف من البحث؛ لم تُثبت الصيغة الكاملة حاليًا.",
        "https://www.playstation.com/en-gb/support/account/change-online-id/",
    ),
    "telegram": Profile(
        "telegram",
        "Telegram basic username",
        "فريد دون اعتبار حالة الأحرف",
        "Provisional",
        grammar(5, 32, "_", upper=True, first="letter"),
        "المحارف موثقة؛ الطول والبداية مؤقتان. collectible username نظام مختلف.",
        "https://core.telegram.org/method/account.checkUsername",
    ),
    "twitch": Profile(
        "twitch",
        "Twitch username",
        "معرّف حساب",
        "Provisional",
        grammar(4, 25, "_"),
        "مجال 4–25 ومجموعة المحارف يحتاجان تأكيدًا كاملًا.",
    ),
    "epic": Profile(
        "epic",
        "Epic display name",
        "هوية Epic؛ قد تختلف هوية المنصة المرتبطة",
        "Provisional",
        grammar(3, 16, upper=True),
        "الطول موثق؛ المحارف هنا مجموعة ASCII مختارة، لا قائمة كاملة. لا يشمل PSN/Xbox تلقائيًا.",
        "https://www.epicgames.com/help/c-Category_EpicAccounts/c-42947264/a14584514?lang=en-US",
    ),
    "riot": Profile(
        "riot",
        "Riot ID: GameName#Tagline",
        "مركّب؛ GameName وحده غير فريد",
        "Verified",
        grammar(3, 16, upper=True),
        "توليد مجموعة ASCII من الحروف والأرقام؛ Unicode أوسع. الحجز الإقليمي والاعتدال منفصلان.",
        "https://support.riotgames.com/en-us/riot/account/riot-id-faq",
        grammar(3, 5, upper=True),
    ),
    "ea": Profile(
        "ea",
        "EA ID",
        "حساب EA؛ قد تعرض اللعبة حساب المنصة المرتبطة",
        "Provisional",
        grammar(4, 16, upper=True),
        "الطول 4–16 من البحث؛ بقية المحارف غير مثبتة، وهذه مجموعة مختارة.",
    ),
    "minecraft_java": Profile(
        "minecraft_java",
        "Minecraft Java profile name",
        "ملف Java؛ ليس Bedrock",
        "Provisional",
        grammar(3, 16, "_", upper=True),
        "الصيغة 3–16 وحروف/أرقام/_ من البحث؛ لم تتوفر وثيقة رسمية كاملة في هذه الجولة.",
    ),
}


def incomplete(key, identifier, uniqueness):
    return Profile(
        key,
        identifier,
        uniqueness,
        "Incomplete",
        grammar(3, 3, "_.-", upper=True),
        "حدود الطول والمحارف الكاملة غير مؤكدة. الإعداد 3–3 نقطة بداية مخصصة فقط؛ "
        "يلزم اختيار Custom Rules، ولا يُنسب الناتج إلى صلاحية المنصة.",
        bounds_known=False,
    )


for key, identifier, identity in (
    ("xbox", "Xbox modern Gamertag", "نظام حديث قد يضيف لاحقة رقمية؛ الاسم وحده غير مضمون التفرد"),
    ("xbox_legacy", "Xbox legacy Gamertag", "نظام قديم؛ قواعد مختلفة عن الحديث"),
    ("tellonym", "Tellonym username", "معرّف حساب؛ القيود غير مكتملة"),
    ("jaco", "JACO username", "معرّف حساب؛ القيود غير مكتملة"),
    ("steam", "Steam display name", "اسم عرض غير فريد؛ ليس SteamID أو اسم تسجيل الدخول"),
    ("steam_url", "Steam custom profile URL", "رابط مخصص؛ مختلف عن اسم العرض"),
    ("pubg", "PUBG Mobile nickname", "اسم داخل اللعبة؛ لا إثبات لتفرده"),
    ("rockstar", "Rockstar nickname", "حساب Rockstar؛ قد تختلف هوية المنصة المرتبطة"),
    ("coc", "Clash of Clans player name", "اسم عرض غير فريد؛ Player Tag منفصل"),
    ("cr", "Clash Royale player name", "اسم عرض غير فريد؛ Player Tag منفصل"),
    ("activision", "Activision display portion", "جزء عرض فقط؛ discriminator يُعيّن خارجيًا ولا نولّده"),
    ("ubisoft", "Ubisoft username", "حساب Ubisoft؛ القيود الكاملة غير مثبتة"),
    ("genshin", "Genshin Impact nickname", "اسم عرض غير فريد؛ UID منفصل"),
    ("8ball", "8 Ball Pool nickname", "اسم عرض؛ Unique ID رقمي منفصل"),
    ("battletag", "BattleTag display portion", "جزء عرض فقط؛ اللاحقة الرقمية ليست اسمًا قابلًا للحجز هنا"),
):
    PROFILES[key] = incomplete(key, identifier, identity)

ALIASES = dict(
    zip(
        SOCIAL,
        (
            "instagram",
            "x",
            "tiktok",
            "snapchat",
            "psn",
            "xbox",
            "tellonym",
            "discord",
            "telegram",
            "jaco",
            "twitch",
            "steam",
        ),
        strict=True,
    )
)
ALIASES.update(
    dict(
        zip(
            GAMING,
            (
                "pubg",
                "epic",
                "riot",
                "ea",
                "rockstar",
                "coc",
                "cr",
                "activision",
                "ea",
                "ubisoft",
                "genshin",
                "discord",
                "steam",
                "ea",
                "ubisoft",
                "rockstar",
                "steam",
                "riot",
                "8ball",
                "battletag",
                "epic",
                "roblox",
                "minecraft_java",
            ),
            strict=True,
        )
    )
)


def variants(platform):
    key = ALIASES[platform]
    keys = {
        "Xbox": ("xbox", "xbox_legacy"),
        "Minecraft": ("minecraft_java", "xbox"),
        "Steam": ("steam", "steam_url"),
        "CS:GO": ("steam", "steam_url"),
        "Apex Legends": ("ea", "psn", "xbox"),
        "Fortnite": ("epic", "psn", "xbox"),
        "Rocket League": ("epic", "psn", "xbox"),
    }.get(platform, (key,))
    return tuple(PROFILES[k] for k in keys)


def violations(profile, selected, baseline=None):
    base = baseline or profile.grammar
    issues = []
    if profile.confidence == "Incomplete":
        issues.append("قواعد المنصة غير مكتملة؛ الناتج مخصص وغير مثبت الصلاحية.")
    if profile.bounds_known and (selected.minimum < base.minimum or selected.maximum > base.maximum):
        issues.append("الطول خارج الحدود المعروفة أو المؤقتة للمنصة.")
    if set(selected.alphabet) - set(base.alphabet):
        issues.append("توجد محارف خارج المجموعة المرجعية المختارة؛ لا ادعاء بصلاحيتها للمنصة.")
    if base.first != "any" and selected.first != base.first:
        issues.append("شرط بداية الاسم يخالف الإعداد المرجعي.")
    if base.last != "any" and selected.last != base.last:
        issues.append("شرط نهاية الاسم يخالف الإعداد المرجعي.")
    limits = {row[0]: row[1:] for row in base.specials}
    for char, count, positions in selected.specials:
        if char not in limits or count > limits[char][0] or set(positions) - set(limits[char][1]):
            issues.append(f"مواضع أو تكرار الرمز {char} تتجاوز الإعداد المرجعي.")
    if set(base.no_repeat) - set(selected.no_repeat):
        issues.append("حظر تتابع الرمز غير مفعل وفق الإعداد المرجعي.")
    return issues
