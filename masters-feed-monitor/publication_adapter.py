"""MASTERS adapter: APIs for Cian/Avito, independent cards for the rest."""
import json
import os
from publication_control import daily_cian, extract_count, statistics_lines
from floor_control import height_profile


def install(m):
    original_snapshots=m.download_marketplace_api_snapshots
    original_analyze=m.analyze_directory
    original_render=m.format_telegram
    original_compare=m.compare_master_to_feeds

    def compare(master,feeds):
        result=original_compare(master,feeds)
        for source,rows in feeds.items():
            if source in result['sources']:
                result['sources'][source].update(height_profile(master,rows))
        return result

    def snapshots(cache_dir):
        original_snapshots(cache_dir)
        path=m._marketplace_api_path(cache_dir,'cian')
        snapshot=json.loads(path.read_text())
        snapshot['daily_statistics']=daily_cian(cache_dir,'masters-cian',os.environ.get('CIAN_API_KEY'),budget=100)
        path.write_text(json.dumps(snapshot,ensure_ascii=False))

    def analyze(cache_dir):
        report=original_analyze(cache_dir)
        snapshot=m._read_marketplace_api_snapshot(cache_dir,'cian') or {}
        report['daily_statistics']=snapshot.get('daily_statistics')
        return report

    def render(report):
        text=original_render(report)
        text=text.replace('Площадки (API, карточки и контроль по фиду):','Площадки — факт размещения (API / карточки ЖК):')
        stats=statistics_lines({'cian':{'daily_statistics':report.get('daily_statistics')}},{'cian':'Циан'})
        if stats:
            before,separator,after=text.rpartition('Проверено:')
            text=before+'Статистика за вчера · Циан\nПо опубликованным XML-объявлениям; не влияет на статус выгрузки.\n'+'\n'.join(stats)+'\n\n'+separator+after
        return text

    m.download_marketplace_api_snapshots=snapshots
    m.compare_master_to_feeds=compare
    m.analyze_directory=analyze
    m.format_detailed_telegram=render
    from concise_reports import render_masters
    m.format_telegram=lambda report: render_masters(report, m)
