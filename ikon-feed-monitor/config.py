"""Public configuration for the IKON DEVELOPMENT feed monitor.

Credentials never belong here.  The workflow reads them from GitHub Secrets.
"""

from __future__ import annotations


CATEGORIES = {
    "residential": "Жилая недвижимость",
}

PROJECTS = {
    "new_zelenograd": "ЖК «Новый Зеленоград»",
    "skolkovo_one": "ЖК Skolkovo ONE",
}

WEBSITES = {
    "new_zelenograd": "https://newzelenograd.ru/",
    "skolkovo_one": "https://skolkovoone.ru/",
}

# Client-provided residential sources of truth.
MASTER_SOURCES = {
    "residential": {
        "new_zelenograd": {
            "kind": "xml",
            "url": "https://newzel.ikon-pm.ru/cian.xml",
            "categories": ("Квартира",),
        },
        "skolkovo_one": {
            "kind": "xml",
            "url": "https://1c.ikon-pm.ru/ZaKvar/Za-kvartal.xml",
            "categories": ("Квартира",),
        },
    },
}

FEEDS = {
    "residential": {
        "new_zelenograd": {
            "cian": "https://newfeed.6feeds.ru/feeds/static/new_zelenograd/cian",
            "yandex": "https://newfeed.6feeds.ru/feeds/static/new_zelenograd/yandex",
            "avito": "https://newfeed.6feeds.ru/feeds/static/new_zelenograd/avito",
            "domclick": "https://newfeed.6feeds.ru/feeds/static/new_zelenograd/domclick",
        },
        "skolkovo_one": {
            "cian": "https://newfeed.6feeds.ru/feeds/static/skolkovo_one/cian",
            "yandex": "https://newfeed.6feeds.ru/feeds/static/skolkovo_one/yandex",
            "avito": "https://newfeed.6feeds.ru/feeds/static/skolkovo_one/avito",
            "domclick": "https://newfeed.6feeds.ru/feeds/static/skolkovo_one/domclick",
        },
    },
}

FEED_LABELS = {
    "cian": "Циан",
    "yandex": "Яндекс Недвижимость",
    "avito": "Авито",
    "domclick": "Домклик",
}

# Only active September placements are reported.  Yandex/New Zelenograd and
# Avito/Skolkovo were marked red in the source document and deliberately stay
# disabled until they are explicitly re-enabled by the owner.
PLATFORM_CARDS = {
    "residential": {
        "new_zelenograd": {
            "cian": "https://zhk-novyy-zelenograd-khimki-i.cian.ru/",
            "avito": "https://www.avito.ru/catalog/novostroyki/andreevka/zhk_novyy_zelenograd/25635",
            "domclick": "https://domclick.ru/complexes/zhk-novyi-zelenograd__6304",
        },
        "skolkovo_one": {
            "cian": "https://zhk-skolkovo-one-i.cian.ru/",
            "yandex": "https://realty.yandex.ru/moskva_i_moskovskaya_oblast/kupit/novostrojka/skolkovo-one-2798471/",
            "domclick": "https://domclick.ru/complexes/skolkovo-one-skolkovo-van__116713?utm_referrer=https%3A%2F%2Fyandex.ru%2F",
        },
    },
}

CIAN_API_BASE = "https://public-api.cian.ru"
AVITO_API_BASE = "https://api.avito.ru"

API_SECRETS = {
    "residential": {
        "cian": ("CIAN_API_KEY_RESIDENTIAL",),
        "avito": (
            "AVITO_CLIENT_ID_RESIDENTIAL",
            "AVITO_CLIENT_SECRET_RESIDENTIAL",
        ),
    },
}

# API totals must be compared with placements that are actually enabled, not
# every XML that exists. Avito/Skolkovo residential is excluded in September.
API_ACTIVE_PROJECTS = {
    "residential": {
        "cian": ("new_zelenograd", "skolkovo_one"),
        "avito": ("new_zelenograd",),
    },
}
