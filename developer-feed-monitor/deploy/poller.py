#!/usr/bin/env python3
"""DAR command intake using outbound-only Telegram long polling.

No reliance on Telegram reaching the Russian VPS. Latest report and subscriber
logic remain shared with the isolated DAR relay, in the same SQLite database.
"""
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import _telegram_urlopen
from relay import RelayApplication, RelayStore, _token_hash


def telegram(token, method, payload):
    request=urllib.request.Request('https://api.telegram.org/bot'+token+'/'+method,
        data=urllib.parse.urlencode(payload).encode(),method='POST')
    with _telegram_urlopen(request,timeout=40) as response:
        body=json.load(response)
    if not body.get('ok'): raise RuntimeError('Telegram rejected '+method)
    return body['result']


def process_update(app, path, token, update, send):
    """Return only after command handling and any required reply succeed."""
    status, response=app.handle(path,{'X-Telegram-Bot-Api-Secret-Token':_token_hash(token)},update)
    if status!=200: raise RuntimeError('DAR command intake rejected update')
    method=response.get('method')
    if method:
        try:
            send(token,method,{key:value for key,value in response.items() if key!='method'})
        except urllib.error.HTTPError as error:
            if error.code!=403: raise
            # A blocked chat must not pin the global getUpdates cursor and
            # prevent commands from every other colleague being processed.
            app.store.mark_unreachable(os.environ['MONITOR_TENANT'],str(response['chat_id']),update['update_id'])


def main():
    token=os.environ['TELEGRAM_BOT_TOKEN']
    tenant=os.environ['MONITOR_TENANT']
    base='https://localhost'
    secret=os.environ['RELAY_PATH_SECRET']
    database=Path(os.environ['RELAY_DATABASE'])
    store=RelayStore(database)
    app=RelayApplication(store,base,{tenant:secret})
    path=urllib.parse.urlsplit(base).path+'/v1/'+tenant+'/'+secret
    # Idempotent. Do not discard pending /start commands on restart.
    telegram(token,'deleteWebhook',{'drop_pending_updates':'false'})
    store.register_token(tenant,_token_hash(token))
    store.mark_webhook_configured(tenant)
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS poll_cursor (id INTEGER PRIMARY KEY CHECK(id=1), update_id INTEGER NOT NULL)')
        row=connection.execute('SELECT update_id FROM poll_cursor WHERE id=1').fetchone()
    offset=(row[0]+1) if row else 0
    print(tenant+' command polling active',flush=True)
    while True:
        try:
            updates=telegram(token,'getUpdates',{'offset':offset,'timeout':25,'limit':50,'allowed_updates':json.dumps(['message'])})
            if not isinstance(updates,list): raise ValueError('invalid Telegram updates')
            for update in updates:
                update_id=update.get('update_id')
                if type(update_id)!=int: raise ValueError('invalid Telegram update id')
                process_update(app,path,token,update,telegram)
                with sqlite3.connect(database) as connection:
                    connection.execute('INSERT INTO poll_cursor(id,update_id) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET update_id=excluded.update_id',(update_id,))
                offset=update_id+1
                print('DAR command handled',flush=True)
        except (OSError,RuntimeError,ValueError,sqlite3.Error) as error:
            # Never log an exception URL containing the token.
            print('DAR polling retry: '+type(error).__name__+(' HTTP '+str(error.code) if isinstance(error,urllib.error.HTTPError) else ''),flush=True)
            time.sleep(5)


if __name__=='__main__':
    try: main()
    except (OSError,RuntimeError,ValueError) as error:
        raise SystemExit('DAR polling startup failed: '+type(error).__name__) from None
