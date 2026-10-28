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
from pathlib import Path
from relay import RelayApplication, RelayStore, _token_hash


def telegram(token, method, payload):
    request=urllib.request.Request('https://api.telegram.org/bot'+token+'/'+method,
        data=urllib.parse.urlencode(payload).encode(),method='POST')
    with urllib.request.urlopen(request,timeout=40) as response:
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
            app.store.mark_unreachable('dar',str(response['chat_id']),update['update_id'])


def main():
    token=os.environ['TELEGRAM_BOT_TOKEN']
    base=os.environ['RELAY_PUBLIC_BASE'].rstrip('/')
    secret=os.environ['DAR_PATH_SECRET']
    database=Path(os.environ.get('RELAY_DATABASE','/var/lib/dar-telegram-relay/relay.sqlite3'))
    store=RelayStore(database)
    app=RelayApplication(store,base,{'dar':secret})
    path=urllib.parse.urlsplit(base).path+'/v1/dar/'+secret
    # Idempotent. Do not discard pending /start commands on restart.
    telegram(token,'deleteWebhook',{'drop_pending_updates':'false'})
    store.register_token('dar',_token_hash(token))
    store.mark_webhook_configured('dar')
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS poll_cursor (id INTEGER PRIMARY KEY CHECK(id=1), update_id INTEGER NOT NULL)')
        row=connection.execute('SELECT update_id FROM poll_cursor WHERE id=1').fetchone()
    offset=(row[0]+1) if row else 0
    print('DAR command polling active',flush=True)
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
