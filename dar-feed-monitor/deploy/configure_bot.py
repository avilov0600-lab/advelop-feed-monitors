"""Idempotent DAR profile setup; read secrets only from protected server env."""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

env=dict(line.split('=',1) for line in Path('/etc/dar-feed-monitor.env').read_text().splitlines() if '=' in line)
token=env['TELEGRAM_BOT_TOKEN']
def call(method, values=None):
    request=urllib.request.Request('https://api.telegram.org/bot'+token+'/'+method,
        data=urllib.parse.urlencode(values or {}).encode(),method='POST')
    try:
        with urllib.request.urlopen(request,timeout=15) as response: result=json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit('Telegram HTTP '+str(error.code)) from None
    if not result.get('ok'): raise SystemExit('Telegram did not confirm '+method)
    return result['result']

bot=call('getMe')
assert bot['username']=='advelop_dar_bot', 'Unexpected bot: refusing profile edits'
if '--inspect' not in sys.argv:
    call('setMyDescription',{'description':'Контроль выгрузок DAR: Дом на Часовой и Ракурс. Эталон клиента → XML агентства → площадки. Отчёты ежедневно в 09:00, 13:00, 15:30 и 18:30 МСК. /start — подписка и последний отчёт; /stop — отключить рассылку.'})
    call('setMyShortDescription',{'short_description':'DAR — контроль XML и объявлений. 09:00, 13:00, 15:30 и 18:30 МСК.'})
    call('setMyCommands',{'commands':json.dumps([{'command':'start','description':'Подписаться и получить последний отчёт'},{'command':'stop','description':'Отключить рассылку'}],ensure_ascii=False)})
webhook=call('getWebhookInfo')
print(json.dumps({'username':bot['username'],'name':bot['first_name'],
    'webhook_configured':bool(webhook.get('url')),'pending_updates':webhook.get('pending_update_count'),
    'webhook_has_error':bool(webhook.get('last_error_message')),
    'webhook_last_error':str(webhook.get('last_error_message','')).replace(token,'[hidden]').replace(env['TELEGRAM_LATEST_MESSAGE_URL'],'[hidden-url]'),
    'webhook_last_error_date':webhook.get('last_error_date')},ensure_ascii=False))
