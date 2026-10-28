"""Safe API-based bot profile setup, outbound IPv6; never log tokens."""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from config import DATA, CLIENT_NAME
from monitor import _telegram_urlopen

def call(method,params=None):
    req=urllib.request.Request('https://api.telegram.org/bot'+os.environ['TELEGRAM_BOT_TOKEN']+'/'+method,
        data=urllib.parse.urlencode(params or {}).encode(),method='POST')
    for attempt in range(3):
        try:
            with _telegram_urlopen(req,timeout=15) as r: data=json.load(r)
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2)
    if not data.get('ok'):raise RuntimeError('Telegram rejected '+method)
    return data['result']

def main(*, verify_only=False):
    bot=call('getMe')
    if bot['username']!=DATA['username']:
        raise ValueError('Unexpected bot identity')
    if verify_only:
        print(bot['username']+': identity verified')
        return
    call('setMyName',{'name':CLIENT_NAME+' — контроль выгрузок'})
    call('setMyDescription',{'description':CLIENT_NAME+' — контроль выгрузок. Материнский источник клиента → XML агентства → площадки. Проверки: 09:00, 13:00, 15:30, 18:30 МСК. /start — подписаться и получить последний отчёт; /stop — отключить рассылку.'})
    call('setMyShortDescription',{'short_description':CLIENT_NAME+' — контроль выгрузок. 4 проверки в сутки.'})
    call('setMyCommands',{'commands':json.dumps([{'command':'start','description':'Подписаться и получить последний отчёт'}, {'command':'stop','description':'Отключить рассылку'}],ensure_ascii=False)})
    print(bot['username']+': profile verified')

if __name__=='__main__':
    try:main(verify_only='--verify-only' in sys.argv)
    except Exception as e:raise SystemExit('Profile setup failed: '+type(e).__name__) from None
