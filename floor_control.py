"""Building storeys are not an apartment's floor.

Keep per-lot audit counts intact; group public warnings by distinct height pairs,
never claim that a height pair identifies a physical building or section.
"""
from collections import Counter


def height_profile(master, feed):
    expected={row.external_id:row for row in master if row.external_id}
    actual={row.external_id:row for row in feed if row.external_id}
    pairs=Counter()
    missing_master=missing_feed=known=0
    for ident in expected.keys() & actual.keys():
        a,b=expected[ident].floors_total,actual[ident].floors_total
        missing_master+=a is None
        missing_feed+=b is None
        if a is None or b is None:continue
        known+=1
        if a!=b:pairs[(a,b)]+=1
    return {'floors_total_groups':[{'master':a,'feed':b,'lot_count':n}
                                  for (a,b),n in sorted(pairs.items())],
            'floors_total_coverage':{'comparable':known,'missing_in_master':missing_master,
                                     'missing_in_feed':missing_feed},
            'floors_total_grouping':'height_pair_not_building_identity'}


def non_height_counts(comparison):
    return {k:v for k,v in comparison.get('field_mismatch_counts',{}).items()
            if k!='floors_total' and v}


def height_lines(comparisons,labels):
    groups={}
    missing=[]
    for source,comparison in comparisons.items():
        for group in comparison.get('floors_total_groups',[]):
            groups.setdefault((group['master'],group['feed']),[]).append(labels[source])
        unknown=comparison.get('floors_total_coverage',{}).get('missing_in_feed',0)
        if unknown:missing.append(labels[source]+' ('+str(unknown)+' квартир)')
    lines=['🔴 Этажность здания: эталон '+str(a)+' → XML '+str(b)+
            ' ('+', '.join(dict.fromkeys(sources))+'); требуется проверить'
            for (a,b),sources in sorted(groups.items())]
    if missing:lines.append('⚪ Этажность здания не указана в XML: '+', '.join(missing)+'; сверка не выполнена')
    return lines


def invalid_floor_count(lots):
    return sum(row.floor is not None and row.floors_total is not None and
               row.floor>row.floors_total for row in lots)


def cross_height_profile(feeds):
    indexes={s:{r.external_id:r for r in rows if r.external_id} for s,rows in feeds.items()}
    common=set.intersection(*(set(rows) for rows in indexes.values())) if indexes else set()
    patterns=Counter()
    for ident in common:
        values=tuple((s,rows[ident].floors_total) for s,rows in sorted(indexes.items())
                     if rows[ident].floors_total is not None)
        if len({value for _,value in values})>1:patterns[values]+=1
    return [{'values':dict(values),'lot_count':n} for values,n in sorted(patterns.items())]


def cross_height_lines(groups,labels):
    return ['⚪ Этажность между XML (без эталона): '+', '.join(labels[s]+' '+str(value)
            for s,value in group['values'].items())+'; сопоставимость секций требует проверки' for group in groups]
