"""Tenant sources. Credentials live exclusively in a per-tenant environment."""
import json
import os
from pathlib import Path

TENANT = os.environ.get('MONITOR_TENANT', 'asterus')
DATA = json.loads((Path(__file__).parent / 'clients.json').read_text())[TENANT]
CLIENT_NAME = DATA['name']
CATEGORIES = {'residential': 'Жилая недвижимость'}
PROJECTS = {key: value['name'] for key, value in DATA['projects'].items()}
WEBSITES = {key: value['website'] for key, value in DATA['projects'].items() if value.get('website')}
MASTER_SOURCES = {'residential': {key: value['master'] for key, value in DATA['projects'].items()}}
FEEDS = {'residential': {key: value['feeds'] for key, value in DATA['projects'].items()}}
PLATFORM_CARDS = {'residential': {key: value['cards'] for key, value in DATA['projects'].items()}}
FEED_LABELS = {'cian': 'Циан', 'yandex': 'Яндекс Недвижимость', 'avito': 'Авито', 'domclick': 'Домклик', 'm2': 'М2', 'yandex3': 'Яндекс — дополнительный XML', 'yandex2': 'Яндекс — малый XML'}
CIAN_API_BASE = 'https://public-api.cian.ru'
AVITO_API_BASE = 'https://api.avito.ru'
API_ACCOUNTS = DATA['accounts']
API_SECRETS = {'residential': {key: tuple(a['secrets']) for key, a in API_ACCOUNTS.items()}}
API_ACTIVE_PROJECTS = {'residential': {key: tuple(a['projects']) for key, a in API_ACCOUNTS.items()}}
for key, account in API_ACCOUNTS.items():
    FEED_LABELS[key] = FEED_LABELS[account['source']]
