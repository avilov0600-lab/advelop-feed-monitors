"""Read public site availability and named count fields; never dump HTML."""
import re
import urllib.request

sites={'dar-chasovaya':'https://ddoomm.moscow/','dar-rakurs':'https://rakurs.moscow/',
       'ikon-newzel':'https://newzelenograd.ru/','ikon-skolkovo':'https://skolkovoone.ru/'}
for name,url in sites.items():
    try:
        with urllib.request.urlopen(url,timeout=10) as response:
            document=response.read().decode('utf8',errors='replace')
            fields=re.findall(r'(?:flatsTotalCount|flatsCount|availableCount|apartmentsCount|totalCount)["\x27\s:=]+[0-9]+',document)
            print(name,'HTTP',response.status,'counter_fields',fields[:12])
    except Exception as error:
        print(name,type(error).__name__)
