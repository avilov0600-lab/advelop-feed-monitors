"""Read-only live tests of the exact scheduled-send adapters, without sending messages."""
import ast
import json
from pathlib import Path
import sqlite3
import sys
import urllib.parse
import urllib.request

from deploy import CLIENTS, token_for

for tenant, directory in [('dar', 'dar-feed-monitor'), ('ikon', 'ikon-feed-monitor'),
                           ('asterus', 'developer-feed-monitor')]:
    client = CLIENTS[tenant]
    source = Path('/opt') / directory / 'monitor.py'
    tree = ast.parse(source.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_telegram_urlopen']
    if len(functions) != 1:
        raise RuntimeError('Missing scheduled sender adapter')
    namespace = {'sys': sys}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
    with sqlite3.connect('file:' + client['database'] + '?mode=ro', uri=True) as con:
        row = con.execute('SELECT chat_id FROM subscribers WHERE tenant=? AND active=1 LIMIT 1', (tenant,)).fetchone()
    request = urllib.request.Request('https://api.telegram.org/bot' + token_for(client) + '/getChat',
        data=urllib.parse.urlencode({'chat_id': row[0]}).encode(), method='POST')
    with namespace['_telegram_urlopen'](request, timeout=10) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError('Scheduled transport failed')
    print(json.dumps({'scheduled_sender_module': directory, 'live_post_with_required_parameter': 'ok'}), flush=True)
