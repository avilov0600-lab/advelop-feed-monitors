"""DAR public source map. All private credentials are environment-only."""

CATEGORIES = {"residential": "Жилая недвижимость"}
PROJECTS = {"chasovaya": "Дом на Часовой", "rakurs": "ЖК «Ракурс»"}
WEBSITES = {"chasovaya": "https://ddoomm.moscow/apartments", "rakurs": "https://rakurs.moscow/"}

# Chasovaya endpoint discovered through the website's catalogue: house 1189,
# project 816, ЖК 1318. Its exchange export uses the same apartment IDs.
# Only free apartments are expected in advertising; reservations are reported
# separately, never silently counted as sold or missing advertising inventory.
MASTER_SOURCES = {"residential": {
    "chasovaya": {"kind": "planeta", "url": "https://api.planetarf.ru/exchange/planetaPlaces.php?id_jk=1318", "label": "Каталог клиента — Дом на Часовой", "statuses": ("free",)},
    "rakurs": {"kind": "planeta", "url": "https://api.planetarf.ru/exchange/planetaPlaces.php?id_jk=1316", "label": "Материнский источник клиента", "statuses": ("free",)},
}}

FEEDS = {"residential": {
    "chasovaya": {source: f"https://newfeed.6feeds.ru/feeds/static/chasovaya_adv/{source}" for source in ("cian", "yandex", "avito", "yandex3")},
    "rakurs": {source: f"https://newfeed.6feeds.ru/feeds/static/rakurs_moscow/{source}" for source in ("cian", "yandex", "avito", "domclick", "yandex3")},
}}
FEED_LABELS = {"cian": "Циан", "yandex": "Яндекс Недвижимость", "avito": "Авито", "domclick": "Домклик", "yandex3": "Яндекс — дополнительный XML"}
PLATFORM_CARDS = {"residential": {
    "chasovaya": {
        "cian": "https://zhk-ddoomm-dom-na-chasovoy-i.cian.ru/",
        "yandex": "https://realty.yandex.ru/moskva_i_moskovskaya_oblast/kupit/novostrojka/dd-oo-mm-4209791/",
    },
    "rakurs": {
        "cian": "https://zhk-rakurs-k-1-i.cian.ru/",
        "yandex": "https://realty.yandex.ru/moskva_i_moskovskaya_oblast/kupit/novostrojka/rakurs-3992944/",
        "domclick": "https://domclick.ru/complexes/zhk-rakurs__122410",
    },
}}
CIAN_API_BASE = "https://public-api.cian.ru"
AVITO_API_BASE = "https://api.avito.ru"
API_SECRETS = {"residential": {
    "cian": ("CIAN_API_KEY_RESIDENTIAL",),
    "avito": ("AVITO_CLIENT_ID_RESIDENTIAL", "AVITO_CLIENT_SECRET_RESIDENTIAL"),
}}
API_ACTIVE_PROJECTS = {"residential": {"cian": ("chasovaya", "rakurs"), "avito": ("chasovaya", "rakurs")}}
