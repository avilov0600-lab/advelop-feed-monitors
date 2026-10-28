"""Verified website inventory, separate from the client's exchange export.

The Chasovaya frontend requests Planeta /places, house 1189, apartments on
sale, noBooking=1. Read every page; never interpret the SSR zero as inventory.
The frontend's public appToken is discovered from its own bundle, kept in RAM,
and is neither a marketplace credential nor included in reports/logs.
"""
import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request


def parse_domdau_catalogue(document):
    """DomDau's loadLiving uses /hydra/json/data.json and st=1 only."""
    data=json.loads(document)
    rows=data.get('apartments') if isinstance(data,dict) else None
    if not isinstance(rows,dict) or not rows:
        raise ValueError('DomDau catalogue missing')
    ids=set()
    reserved=0
    for row in rows.values():
        if not isinstance(row,dict) or str(row.get('st')) not in {'0','1','2'}:
            raise ValueError('DomDau catalogue schema/status changed')
        if str(row['st']) == '2': reserved+=1
        if str(row['st']) != '1': continue
        ident=str(row.get('id',''))
        if not ident.isdecimal() or int(ident)<=0 or ident in ids:
            raise ValueError('DomDau catalogue ID missing or duplicated')
        ids.add(ident)
    return {'lot_count':len(ids),'ids':sorted(ids),'raw_count':len(rows),
            'excluded_reserved':reserved,'scope':'website available apartments; st=1',
            'check_method':'website_catalogue_json'}


def read_planeta_catalogue(fetch_page, house_id, *, max_pages=100):
    ids = set()
    total = None
    for page in range(1, max_pages + 1):
        data = fetch_page(page)
        if not isinstance(data, dict):
            raise ValueError('catalogue schema changed')
        count, rows = data.get('count'), data.get('places')
        if isinstance(count, str) and count.isdecimal():
            count = int(count)
        if type(count) is not int or count < 0 or not isinstance(rows, list):
            raise ValueError('catalogue count/list missing')
        if total is not None and count != total:
            raise ValueError('catalogue changed during pagination')
        total = count
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('catalogue row invalid')
            ident = str(row.get('id', ''))
            if not ident.isdecimal() or int(ident) <= 0 or ident in ids:
                raise ValueError('catalogue ID missing or duplicated')
            if str(row.get('id_house')) != str(house_id):
                raise ValueError('catalogue house scope changed')
            if row.get('status') != 'free' or str(row.get('saleStatus')) != '1':
                raise ValueError('catalogue includes reserved or unavailable lots')
            ids.add(ident)
        if len(ids) > total:
            raise ValueError('catalogue count inconsistent')
        if len(ids) == total:
            return {'lot_count': total, 'ids': sorted(ids), 'pages': page,
                    'excluded_reserved': True, 'scope': 'free apartments; noBooking=1'}
        if not rows:
            raise ValueError('catalogue pagination incomplete')
    raise ValueError('catalogue pagination limit exceeded')


def download_chasovaya_catalogue(url, document, *, house_id=1189, read_url=None):
    """Use only the site's own app bundle and its verified public API origin."""
    def read(address, headers=None):
        for attempt in range(3):
            try:
                request = urllib.request.Request(address, headers={
                    'User-Agent': 'ADVelop-Feed-Monitor/1.0', **(headers or {})})
                with urllib.request.urlopen(request, timeout=15) as response:
                    return response.read().decode('utf-8')
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(2)

    read_url = read_url or read
    origin = urllib.parse.urlsplit(url)
    scripts = re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)["\']', document, re.I)
    bundles = [urllib.parse.urljoin(url, src) for src in scripts
               if re.fullmatch(r'/_next/static/chunks/pages/_app-[\w-]+\.js', src)]
    if len(bundles) != 1 or origin.hostname != 'ddoomm.moscow':
        raise ValueError('website app bundle not verified')
    bundle = read_url(bundles[0])
    # Public build config: production URL followed by NEXT_PUBLIC appToken.
    match = re.search(r'production:["\']https://api\.planetarf\.ru/api/v3["\']'
                      r'.{0,100}?=["\']([a-f0-9]{32})["\']', bundle)
    if not match:
        raise ValueError('website API configuration changed')
    app_token = match.group(1)

    def fetch_page(page):
        query = urllib.parse.urlencode({'id_house[]': house_id, 'category[]': 'Квартира',
            'saleStatus[]': 1, 'noBooking[]': 1, 'page': page})
        address = 'https://api.planetarf.ru/api/v3/places?' + query
        return json.loads(read_url(address, {'appToken': app_token,
                                             'Content-Type': 'application/json'}))

    result = read_planeta_catalogue(fetch_page, house_id)
    return {**result, 'status': 'OK', 'url': url,
            'check_method': 'website_frontend_api',
            'data_origin': 'Planeta; общий backend с каталогом клиента',
            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
