#!/usr/bin/env python3
"""DAR inventory/feed/platform monitor.

The client residential exchange JSON files are authoritative. Outbound 6feeds XMLs are
compared with them lot by lot. Marketplace APIs and active public project cards
are a separate delivery layer. The script deliberately uses only the Python
standard library and never calls the GitHub API.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import decimal
import hashlib
import html
import http.client
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from config import (
    API_ACCOUNTS,
    CLIENT_NAME,
    DATA,
    API_ACTIVE_PROJECTS,
    API_SECRETS,
    AVITO_API_BASE,
    CATEGORIES,
    CIAN_API_BASE,
    FEED_LABELS,
    FEEDS,
    MASTER_SOURCES,
    PLATFORM_CARDS,
    PROJECTS,
    WEBSITES,
)


MOSCOW_TZ = ZoneInfo("Europe/Moscow")
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class InventoryIncomplete(ValueError):
    """The API answered, but its pagination cannot substantiate a total."""


DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 2
DOWNLOAD_TIMEOUT_SECONDS = 10
MASTER_DOWNLOAD_TIMEOUT_SECONDS = 60
AVITO_PAGE_SIZE = 25
AVITO_EMPTY_SUFFIX = 5
AVITO_PAGE_DELAY_SECONDS = 2.6
MAX_TELEGRAM_MESSAGE = 3900
USER_AGENT = "ADVelopFeedMonitor/1.0"
COMPARE_FIELDS = ("deal", "area", "rooms", "floor", "floors_total", "apartment")
FIELD_LABELS = {
    "deal": "тип сделки",
    "price": "цена",
    "area": "площадь",
    "rooms": "комнатность",
    "floor": "этаж",
    "floors_total": "этажность",
    "apartment": "номер квартиры",
}
RUSSIAN_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма(?:й|я|е)": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}


@dataclasses.dataclass(frozen=True)
class Lot:
    external_id: str
    source: str
    project: str
    category: str
    deal: str | None = None
    price: int | None = None
    area: decimal.Decimal | None = None
    rooms: int | None = None
    floor: int | None = None
    floors_total: int | None = None
    apartment: str | None = None
    house: str | None = None
    description: str = ""

    def compact(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        if self.area is not None:
            value["area"] = str(self.area)
        value.pop("description", None)
        return value


def _now() -> str:
    return dt.datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _xml_root(path: Path) -> ET.Element:
    root = ET.parse(path).getroot()
    for node in root.iter():
        node.tag = _local_name(node.tag)
    return root


def _text(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    child = node.find(path)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    cleaned = re.sub(r"[^\d,.-]", "", str(value)).replace(",", ".")
    if not cleaned:
        return None
    try:
        return int(decimal.Decimal(cleaned))
    except (decimal.InvalidOperation, ValueError):
        return None


def _decimal(value: Any) -> decimal.Decimal | None:
    if value in (None, ""):
        return None
    cleaned = re.sub(r"[^\d,.-]", "", str(value)).replace(",", ".")
    if not cleaned:
        return None
    try:
        return decimal.Decimal(cleaned)
    except decimal.InvalidOperation:
        return None


def _rooms(value: Any, description: str = "", studio: Any = "") -> int | None:
    if str(studio).strip().casefold() in {"1", "true", "yes", "да"}:
        return 0
    if "студи" in str(value).casefold() or "студи" in description[:160].casefold():
        return 0
    return _integer(value)


def _deal(value: Any) -> str | None:
    normalized = str(value or "").casefold()
    if any(word in normalized for word in ("аренд", "сдам", "rent")):
        return "rent"
    if any(word in normalized for word in ("прод", "sale")):
        return "sale"
    return None


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _feed_external_id(value: Any, *, source: str, project: str) -> str:
    ident = _clean_id(value)
    suffix = DATA.get('projects', {}).get(project, {}).get('feed_id_suffixes', {}).get(source)
    if suffix:
        if not ident.endswith(suffix) or not ident[:-len(suffix)]:
            raise ValueError('Configured feed ID namespace changed')
        return ident[:-len(suffix)]
    return ident


def parse_macro(path: Path, *, project: str, category: str, source: dict) -> tuple[list[Lot], dict]:
    rows = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
        raise ValueError('invalid Macro inventory')
    parent_ids = {str(v) for v in source['parent_ids']}
    selected = [r for r in rows if str(r.get('parent_id')) in parent_ids and r.get('category') == 'flat']
    if not selected:
        raise ValueError('project missing from shared client source')
    statuses = collections.Counter(str(r.get('status_name')) for r in selected)
    lots = []
    reserved = 0
    for r in selected:
        if r.get('is_reserved'):
            reserved += 1
            continue
        if r.get('is_sold') or not r.get('is_published') or r.get('activity') != 'sell':
            continue
        if any(r.get(k) in (None, '') for k in ('id', 'estate_area', 'estate_floor')):
            raise ValueError('missing Macro identity or inventory fields')
        lots.append(Lot(external_id=_clean_id(r['id']), source='master_client', project=project,
            category=category, deal='sale', price=_integer(r.get('estate_price')),
            area=_decimal(r.get('estate_areaBti') or r['estate_area']),
            # Macro's studio-layout flag also marks 2-room euro layouts.
            # Only a one-room studio becomes zero rooms in advertising XML.
            rooms=_rooms(r.get('estate_rooms'),studio=r.get('estate_studia') if (_integer(r.get('estate_rooms')) or 0) <= 1 else ''),
            # Outbound schemas describe the building, not a shorter entrance.
            floor=_integer(r['estate_floor']), floors_total=(_integer(r.get('estate_floors_in_house')) or _integer(r.get('estate_floors_in_entrance'))),
            apartment=_clean_id(r.get('geo_flatnum')) or None, house=r.get('house_title')))
    return lots, {'raw_count': len(rows), 'project_residential_count':len(selected),
        'all_status_counts': dict(statuses), 'excluded_reserved': reserved, 'parent_ids': sorted(parent_ids)}


def parse_profitbase(path: Path, *, project: str, category: str) -> tuple[list[Lot], dict]:
    root = _xml_root(path)
    offers = root.findall('offer')
    if not offers or not all(o.find('property_type') is not None and o.find('status') is not None for o in offers):
        raise ValueError('Profitbase schema changed')
    lots = []
    states = collections.Counter()
    for offer in offers:
        if _text(offer, 'property_type').casefold() not in {'квартира', 'апартаменты'}:
            continue
        status = _text(offer, 'status')
        states[status] += 1
        if status != 'AVAILABLE':
            continue
        if not offer.attrib.get('internal-id') or not _text(offer, 'area/value'):
            raise ValueError('Profitbase identity/area missing')
        lots.append(Lot(external_id=offer.attrib['internal-id'], source='master_client', project=project,
            category=category, deal='sale', price=_integer(_text(offer,'price/value')),
            area=_decimal(_text(offer,'area/value')), rooms=_rooms(_text(offer,'rooms'),studio=_text(offer,'studio')),
            floor=_integer(_text(offer,'floor')), floors_total=_integer(_text(offer,'house/floors-total')),
            apartment=_text(offer,'number') or None, house=_text(offer,'house/name') or None))
    return lots, {'all_status_counts':dict(states), 'excluded_reserved':sum(n for s,n in states.items() if s.startswith('BOOKED')),
        'excluded_sold': states.get('SOLD',0), 'excluded_unavailable':states.get('UNAVAILABLE',0)}


def parse_master_xml(
    path: Path, *, project: str, category: str, allowed_categories: Iterable[str]
) -> tuple[list[Lot], dict[str, Any]]:
    root = _xml_root(path)
    allowed = {value.casefold() for value in allowed_categories}
    lots: list[Lot] = []
    all_categories: collections.Counter[str] = collections.Counter()
    status_counts: collections.Counter[str] = collections.Counter()
    for offer in root.findall(".//offer"):
        category_value = _text(offer, "category")
        all_categories[category_value or "missing"] += 1
        if category_value.casefold() not in allowed:
            continue
        description = _text(offer, "description")
        status = _text(offer, "price/Status") or _text(offer, "status")
        status_counts[status or "missing"] += 1
        lots.append(
            Lot(
                external_id=_clean_id(offer.attrib.get("internal-id")),
                source="master_xml",
                project=project,
                category=category,
                deal=_deal(_text(offer, "type")),
                price=_integer(_text(offer, "price/value")),
                area=_decimal(_text(offer, "area/value")),
                rooms=_rooms(
                    _text(offer, "rooms"), description, _text(offer, "studio")
                ),
                floor=_integer(_text(offer, "floor")),
                floors_total=_integer(_text(offer, "floors-total")),
                apartment=_text(offer, "location/apartment") or None,
                house=_text(offer, "building-name") or None,
                description=description,
            )
        )
    return lots, {
        "generation_date": _text(root, "generation-date") or None,
        "all_category_counts": dict(all_categories),
        "selected_status_counts": dict(status_counts),
    }


def parse_cian(path: Path, *, project: str, category: str, source: dict | None = None) -> tuple[list[Lot], dict[str, Any]]:
    root = _xml_root(path)
    objects = root.findall("object")
    raw_count = len(objects)
    if source is not None:
        if root.tag != 'feed' or not objects:
            raise ValueError('Client Cian XML schema is missing')
        names = {str(name).strip().casefold() for name in source.get('project_names', [])}
        name_path = source.get('project_name_path', 'JKSchema/Name')
        allowed = set(source.get('allowed_categories', ['newBuildingFlatSale', 'flatSale']))
        objects = [obj for obj in objects if _text(obj, 'Category') in allowed
                   and (not names or _text(obj, name_path).casefold() in names)]
        if not objects:
            raise ValueError('Residential project not found in client XML')
        if any(not _text(obj, 'ExternalId') or not _text(obj, 'TotalArea') for obj in objects):
            raise ValueError('Client Cian XML identity/area missing')
    lots: list[Lot] = []
    for obj in objects:
        description = _text(obj, "Description")
        category_value = _text(obj, "Category")
        building_floors = _integer(_text(obj, "Building/FloorsCount"))
        if source is not None and building_floors is not None and building_floors <= 0:
            building_floors = None
        lots.append(
            Lot(
                external_id=(_clean_id(_text(obj, "ExternalId")) if source is not None
                             else _feed_external_id(_text(obj, "ExternalId"), source='cian', project=project)),
                source="cian",
                project=project,
                category=category,
                deal=_deal(category_value),
                price=_integer(_text(obj, "BargainTerms/Price")),
                area=_decimal(_text(obj, "TotalArea")),
                # Cian XML uses 9 for a studio, not nine rooms:
                # https://www.cian.ru/xml_import/doc/
                rooms=_rooms(_text(obj, "FlatRoomsCount"), description,
                    "1" if _text(obj, "FlatRoomsCount") == "9" else ""),
                floor=_integer(_text(obj, "FloorNumber")),
                floors_total=building_floors,
                apartment=_text(obj, "Apartment") or None,
                house=_text(obj, "Building/Name") or None,
                description=description,
            )
        )
    meta = {"feed_version": _text(root, "feed_version") or None}
    if source is not None:
        meta.update(raw_count=raw_count, project_residential_count=len(lots),
                    project_names=list(source.get('project_names', [])),
                    missing_building_height_count=sum(lot.floors_total is None for lot in lots))
    return lots, meta


def parse_avito(path: Path, *, project: str, category: str) -> tuple[list[Lot], dict[str, Any]]:
    root = _xml_root(path)
    lots: list[Lot] = []
    dates: list[str] = []
    for ad in root.findall("Ad"):
        date_begin = _text(ad, "DateBegin")
        if date_begin:
            dates.append(date_begin)
        description = _text(ad, "Description")
        lots.append(
            Lot(
                external_id=_clean_id(_text(ad, "Id")),
                source="avito",
                project=project,
                category=category,
                deal=_deal(_text(ad, "OperationType")),
                price=_integer(_text(ad, "Price")),
                area=_decimal(_text(ad, "Square")),
                rooms=_rooms(_text(ad, "Rooms"), description),
                floor=_integer(_text(ad, "Floor")),
                floors_total=_integer(_text(ad, "Floors")),
                apartment=_text(ad, "ApartmentNumber") or None,
                house=_text(ad, "Address") or None,
                description=description,
            )
        )
    return lots, {
        "format_version": root.attrib.get("formatVersion"),
        "date_begin_max": max(dates) if dates else None,
    }


def parse_yandex(path: Path, *, project: str, category: str) -> tuple[list[Lot], dict[str, Any]]:
    root = _xml_root(path)
    lots: list[Lot] = []
    for offer in root.findall("offer"):
        description = _text(offer, "description")
        lots.append(
            Lot(
                external_id=_clean_id(offer.attrib.get("internal-id")),
                source="yandex",
                project=project,
                category=category,
                deal=_deal(_text(offer, "type")),
                price=(
                    _integer(_text(offer, "discount/final-price"))
                    or _integer(_text(offer, "price/value"))
                ),
                area=_decimal(_text(offer, "area/value")),
                rooms=_rooms(
                    _text(offer, "rooms"), description, _text(offer, "studio")
                ),
                floor=_integer(_text(offer, "floor")),
                floors_total=_integer(_text(offer, "floors-total")),
                apartment=_text(offer, "location/apartment") or None,
                house=_text(offer, "building-name") or None,
                description=description,
            )
        )
    return lots, {"generation_date": _text(root, "generation-date") or None}


def parse_domclick(path: Path, *, project: str, category: str) -> tuple[list[Lot], dict[str, Any]]:
    root = _xml_root(path)
    complex_node = root.find("complex")
    lots: list[Lot] = []
    for building in root.findall("complex/buildings/building"):
        floors_total = _integer(_text(building, "floors"))
        house = _text(building, "name") or None
        for flat in building.findall("flats/flat"):
            description = _text(flat, "description")
            lots.append(
                Lot(
                    external_id=_clean_id(_text(flat, "flat_id")),
                    source="domclick",
                    project=project,
                    category=category,
                    deal="sale",
                    price=_integer(_text(flat, "price")),
                    area=_decimal(_text(flat, "area")),
                    rooms=_rooms(_text(flat, "room"), description),
                    floor=_integer(_text(flat, "floor")),
                    floors_total=floors_total,
                    apartment=_text(flat, "apartment") or None,
                    house=house,
                    description=description,
                )
            )
    return lots, {
        "complex_id": _text(complex_node, "id") or None,
        "complex_name": _text(complex_node, "name") or None,
    }


FEED_PARSERS = {
    "cian": parse_cian,
    "yandex": parse_yandex,
    "avito": parse_avito,
    "domclick": parse_domclick,
    "yandex3": parse_yandex,
    "m2": parse_yandex,
    "yandex2": parse_yandex,
}


def parse_m2(path: Path, *, project: str, category: str):
    root = _xml_root(path)
    if root.find('offer') is not None:
        return parse_yandex(path, project=project, category=category)
    if root.find('object') is not None:
        return parse_cian(path, project=project, category=category)
    raise ValueError('M2 XML schema not recognized; refusing a false zero')


FEED_PARSERS['m2'] = parse_m2


def parse_planeta(path: Path, *, project: str, category: str,
                  statuses: Iterable[str] = ("free",)) -> tuple[list[Lot], dict[str, Any]]:
    """Client exchange JSON. Validate shape before interpreting empty stock."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("empty or invalid client inventory")
    if any(not isinstance(row, dict) or not {"id", "t", "st", "sq", "f", "fs", "rc", "n"}.issubset(row) for row in data):
        raise ValueError("client inventory schema changed")
    allowed = set(statuses)
    counts: collections.Counter[str] = collections.Counter()
    lots = []
    for row in data:
        if str(row["t"]).casefold() != "квартира":
            continue
        counts[str(row["st"])] += 1
        if row["st"] not in allowed:
            continue
        number = str(row["n"]).strip()
        if project == "rakurs":
            number = re.sub(r"^[КK]\d+-", "", number, flags=re.IGNORECASE)
        lots.append(Lot(external_id=_clean_id(row["id"]), source="master_client",
            project=project, category=category, deal="sale", price=_integer(row.get("pr")),
            area=_decimal(row["sq"]), rooms=_rooms(row["rc"]),
            floor=_integer(row["f"]), floors_total=_integer(row["fs"]),
            apartment=number or None, house=str(row.get("h_no") or "") or None))
    if any(lot.area is None or lot.floor is None or lot.floors_total is None for lot in lots):
        raise ValueError("invalid client inventory fields")
    return lots, {"all_status_counts": dict(counts), "selected_statuses": sorted(allowed),
                  "excluded_reserved": counts.get("reserved", 0), "raw_count": len(data)}


def _duplicates(lots: Iterable[Lot]) -> list[str]:
    counts = collections.Counter(lot.external_id for lot in lots)
    return sorted(key for key, count in counts.items() if key and count > 1)


def summarize_lots(lots: list[Lot], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    ids = [lot.external_id for lot in lots if lot.external_id]
    deals = collections.Counter(lot.deal or "unknown" for lot in lots)
    return {
        **(meta or {}),
        "lot_count": len(lots),
        "unique_id_count": len(set(ids)),
        "missing_id_count": sum(not lot.external_id for lot in lots),
        "duplicate_ids": _duplicates(lots),
        "deal_counts": dict(deals),
    }


def detect_marketing_issues(path: Path, *, today: dt.date | None = None) -> list[str]:
    """Flag expired promo dates and unambiguously stale month wording."""
    today = today or dt.datetime.now(MOSCOW_TZ).date()
    try:
        root = _xml_root(path)
    except (FileNotFoundError, ET.ParseError):
        return []
    issues: list[str] = []
    for node in root.iter():
        if _local_name(node.tag).casefold() not in {"end_date", "end-date", "dateend"}:
            continue
        raw = (node.text or "").strip()
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if not match:
            continue
        try:
            end_date = dt.date(*map(int, match.groups()))
        except ValueError:
            continue
        if end_date < today:
            kind = "публикации" if _local_name(node.tag).casefold() == "dateend" else "акции"
            issues.append(f"истёк срок {kind} {raw}")
    document = " ".join((node.text or "") for node in root.iter())
    for stem, month in RUSSIAN_MONTHS.items():
        if month >= today.month:
            continue
        pattern = re.compile(
            rf"(?:скидк|акци|ипотек)\w*.{{0,100}}?\b{stem}\w*(?:\s+(20\d{{2}}))?",
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(document)
        if match and (not match.group(1) or int(match.group(1)) <= today.year):
            issues.append(f"устаревшее упоминание месяца №{month} в акции")
    return sorted(set(issues))


def _json_value(value: Any) -> Any:
    return str(value) if isinstance(value, decimal.Decimal) else value


def compare_master_to_feed(master: list[Lot], feed: list[Lot]) -> dict[str, Any]:
    master_by_id = {lot.external_id: lot for lot in master if lot.external_id}
    feed_by_id = {lot.external_id: lot for lot in feed if lot.external_id}
    missing = sorted(set(master_by_id) - set(feed_by_id))
    extra = sorted(set(feed_by_id) - set(master_by_id))
    mismatch_counts: collections.Counter[str] = collections.Counter()
    samples: list[dict[str, Any]] = []
    for external_id in sorted(set(master_by_id) & set(feed_by_id)):
        expected, actual = master_by_id[external_id], feed_by_id[external_id]
        differences: dict[str, Any] = {}
        # Residential outbound prices may include platform promotions, so price
        # is checked across outgoing feeds instead of against the raw source.
        for field in COMPARE_FIELDS:
            expected_value, actual_value = getattr(expected, field), getattr(actual, field)
            if expected_value is None or actual_value is None:
                continue
            if expected_value != actual_value:
                mismatch_counts[field] += 1
                differences[field] = {
                    "master": _json_value(expected_value),
                    "feed": _json_value(actual_value),
                }
        if differences and len(samples) < 20:
            samples.append({"external_id": external_id, "fields": differences})
    duplicate_ids = _duplicates(feed)
    missing_id_count = sum(not lot.external_id for lot in feed)
    error_count = (
        len(missing) + len(extra) + sum(mismatch_counts.values())
        + len(duplicate_ids) + missing_id_count
    )
    return {
        "status": "ok" if error_count == 0 else "error",
        "master_count": len(master_by_id),
        "feed_count": len(feed_by_id),
        "missing_from_feed": missing,
        "extra_in_feed": extra,
        "field_mismatch_counts": dict(mismatch_counts),
        "field_mismatch_samples": samples,
        "duplicate_ids": duplicate_ids,
        "missing_id_count": missing_id_count,
        "error_count": error_count,
    }


def compare_feeds(feed_lots: dict[str, list[Lot]]) -> dict[str, Any]:
    sources = list(feed_lots)
    if not sources:
        return {"status": SOURCE_UNAVAILABLE, "count_by_feed": {}, "id_differences": {}}
    baseline = "cian" if "cian" in feed_lots else sources[0]
    baseline_ids = {lot.external_id for lot in feed_lots[baseline]}
    count_by_feed = {source: len(lots) for source, lots in feed_lots.items()}
    id_differences: dict[str, Any] = {}
    for source, lots in feed_lots.items():
        ids = {lot.external_id for lot in lots}
        id_differences[source] = {
            "baseline": baseline,
            "missing": sorted(baseline_ids - ids),
            "extra": sorted(ids - baseline_ids),
        }
    common = set.intersection(
        *({lot.external_id for lot in lots} for lots in feed_lots.values())
    )
    mismatches: collections.Counter[str] = collections.Counter()
    samples: list[dict[str, Any]] = []
    indexes = {
        source: {lot.external_id: lot for lot in lots} for source, lots in feed_lots.items()
    }
    for external_id in sorted(common):
        differences: dict[str, Any] = {}
        for field in ("price", "deal", "area", "rooms", "floor", "apartment"):
            values = {source: getattr(index[external_id], field) for source, index in indexes.items()}
            comparable = {_json_value(value) for value in values.values() if value is not None}
            if len(comparable) > 1:
                mismatches[field] += 1
                differences[field] = {
                    source: _json_value(value) for source, value in values.items()
                }
        if differences and len(samples) < 20:
            samples.append({"external_id": external_id, "fields": differences})
    has_id_diff = any(v["missing"] or v["extra"] for v in id_differences.values())
    return {
        "status": "error" if has_id_diff or mismatches else "ok",
        "baseline": baseline,
        "count_by_feed": count_by_feed,
        "id_differences": id_differences,
        "field_mismatch_counts": dict(mismatches),
        "field_mismatch_samples": samples,
    }


def _download(
    url: str,
    destination: Path,
    *,
    attempts: int = DOWNLOAD_ATTEMPTS,
    retry_delay: float = DOWNLOAD_RETRY_DELAY_SECONDS,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    last_error: BaseException | None = None
    maximum_attempts = max(1, attempts)
    for attempt in range(1, maximum_attempts + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(response.read())
                return {
                    "status": "OK",
                    "downloaded": True,
                    "attempts": attempt,
                    "http_status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "checked_at": _now(),
                }
        except urllib.error.HTTPError as error:
            last_error = error
            if (error.code != 429 and error.code < 500) or attempt >= maximum_attempts:
                break
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as error:
            last_error = error
            if attempt >= maximum_attempts:
                break
        if retry_delay:
            time.sleep(retry_delay)
    assert last_error is not None
    try:
        setattr(last_error, "dar_attempts", attempt)
    except (AttributeError, TypeError):
        pass
    raise last_error


def _safe_failure(error: BaseException, attempts: int = DOWNLOAD_ATTEMPTS) -> dict[str, Any]:
    actual_attempts = int(getattr(error, "dar_attempts", attempts))
    if isinstance(error, urllib.error.HTTPError):
        reason = f"HTTP {error.code}"
    elif isinstance(error, urllib.error.URLError):
        reason = f"сетевая ошибка ({type(error.reason).__name__})"
    else:
        reason = f"сетевая ошибка ({type(error).__name__})"
    return {
        "status": SOURCE_UNAVAILABLE,
        "downloaded": False,
        "attempts": actual_attempts,
        "reason": reason,
        "checked_at": _now(),
    }


def _capture_download(
    url: str, destination: Path, *, attempts: int = DOWNLOAD_ATTEMPTS,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    try:
        return _download(url, destination, attempts=attempts, timeout=timeout)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
    ) as error:
        destination.unlink(missing_ok=True)
        return _safe_failure(error, attempts)


def _request_json(
    url: str,
    *,
    token: str | None = None,
    data: dict[str, str] | None = None,
    attempts: int = DOWNLOAD_ATTEMPTS,
) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8") if data else None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    payload: Any = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if attempt >= attempts or (error.code != 429 and error.code < 500):
                raise
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ):
            if attempt >= attempts:
                raise
        time.sleep(min(attempt, 2))
    if not isinstance(payload, dict):
        raise ValueError("API response is not a JSON object")
    return payload


def _api_reason(error: BaseException) -> str:
    if isinstance(error, InventoryIncomplete):
        return str(error)
    if isinstance(error, urllib.error.HTTPError):
        return f"API вернул HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return f"сетевая ошибка API ({type(error.reason).__name__})"
    return f"ошибка API ({type(error).__name__})"


def _issue_count(value: Any) -> int:
    if isinstance(value, (list, dict)):
        return len(value)
    return int(bool(value))


def fetch_cian_snapshot(category: str, account: str = "cian_1") -> dict[str, Any]:
    if API_ACCOUNTS.get(account, {}).get('enabled') is False:
        return {'status': 'not_configured', 'checked_at': _now(),
                'reason': API_ACCOUNTS[account]['disabled_reason']}
    secret_name = API_SECRETS[category][account][0]
    token = os.environ.get(secret_name)
    checked_at = _now()
    if not token:
        return {
            "status": "not_configured",
            "check_method": "api_published_count",
            "checked_at": checked_at,
            "reason": "доступ к API Циан не подключён",
        }
    try:
        query = urllib.parse.urlencode(
            {"source": "upload", "statuses": "published", "page": 1, "pageSize": 100}
        )
        payload = _request_json(f"{CIAN_API_BASE}/v2/get-my-offers?{query}", token=token)
        result = payload.get("result") or {}
        if not isinstance(result, dict) or not ({"totalCount", "announcements"} & result.keys()):
            raise ValueError("Cian response has no inventory result")
        announcements = list(result.get("announcements") or [])
        snapshot: dict[str, Any] = {
            "status": "ok",
            "check_method": "api_published_count",
            "checked_at": checked_at,
            "published_count": int(result.get("totalCount") or len(announcements)),
        }
        try:
            order = _request_json(f"{CIAN_API_BASE}/v1/get-order", token=token)
            offers = list((order.get("result") or {}).get("offers") or [])
            statuses = collections.Counter(str(row.get("status") or "unknown") for row in offers)
            snapshot.update(
                {
                    "import_offer_count": len(offers),
                    "import_status_counts": dict(statuses),
                    "import_error_count": sum(_issue_count(row.get("errors")) for row in offers),
                    "import_warning_count": sum(_issue_count(row.get("warnings")) for row in offers),
                }
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            ValueError,
            http.client.HTTPException,
        ) as error:
            snapshot["import_report_reason"] = _api_reason(error)
        return snapshot
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        OSError,
        ValueError,
        http.client.HTTPException,
    ) as error:
        return {
            "status": "unavailable",
            "check_method": "api_published_count",
            "checked_at": checked_at,
            "reason": _api_reason(error),
        }


def fetch_avito_snapshot(category: str, account: str = "avito_1") -> dict[str, Any]:
    client_name, secret_name = API_SECRETS[category][account]
    client_id, client_secret = os.environ.get(client_name), os.environ.get(secret_name)
    checked_at = _now()
    if not client_id or not client_secret:
        return {
            "status": "not_configured",
            "check_method": "api_active_count",
            "checked_at": checked_at,
            "reason": "доступ к API Авито не подключён",
        }
    try:
        auth = _request_json(
            f"{AVITO_API_BASE}/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        token = str(auth["access_token"])
        items: dict[str, dict[str, Any]] = {}
        # /core/v1/items occasionally contains sparse pages: for this account
        # page 2 was empty while later pages still contained active ads. With
        # 25 records, observed non-empty pages return unique IDs; larger pages
        # overlap. Pace calls below the listing API limit and walk a bounded
        # suffix of empty pages rather than trusting the first gap.
        empty_pages_after_last_item = 0
        for page in range(1, 101):
            if page > 1 and AVITO_PAGE_DELAY_SECONDS:
                time.sleep(AVITO_PAGE_DELAY_SECONDS)
            query = urllib.parse.urlencode({"status": "active", "page": page, "per_page": AVITO_PAGE_SIZE})
            payload = _request_json(f"{AVITO_API_BASE}/core/v1/items?{query}", token=token)
            if not isinstance(payload.get("resources", payload.get("items")), list):
                raise ValueError("Avito response has no inventory list")
            rows = list(payload.get("resources") or payload.get("items") or [])
            new_count = 0
            for row in rows:
                item_id = str(row.get("id") or "")
                if item_id and item_id not in items:
                    items[item_id] = row
                    new_count += 1
            if rows:
                empty_pages_after_last_item = 0
            else:
                empty_pages_after_last_item += 1
                if empty_pages_after_last_item >= AVITO_EMPTY_SUFFIX:
                    break
            if rows and new_count == 0:
                raise InventoryIncomplete("Авито повторяет страницу API; полное число объявлений не подтверждено")
            if page == 100:
                raise ValueError("Avito pagination limit reached")
        return {
            "status": "ok",
            "check_method": "api_active_count",
            "checked_at": checked_at,
            "active_count": len(items),
        }
    except (
        KeyError,
        urllib.error.HTTPError,
        urllib.error.URLError,
        OSError,
        ValueError,
        http.client.HTTPException,
    ) as error:
        return {
            "status": "unavailable",
            "check_method": "api_active_count",
            "checked_at": checked_at,
            "reason": _api_reason(error),
        }


def _cache_key(*parts: str) -> str:
    return "-".join(part.replace("_", "-") for part in parts)


def _master_xml_path(cache_dir: Path, project: str) -> Path:
    return cache_dir / f"master-{project}.xml"


def _feed_path(cache_dir: Path, category: str, project: str, source: str) -> Path:
    return cache_dir / f"feed-{_cache_key(category, project, source)}.xml"


def _card_path(cache_dir: Path, category: str, project: str, source: str) -> Path:
    return cache_dir / f"card-{_cache_key(category, project, source)}.html"


def _api_path(cache_dir: Path, category: str, source: str) -> Path:
    return cache_dir / f"api-{_cache_key(category, source)}.json"


def download_sources(cache_dir: Path) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    statuses: dict[str, Any] = {"masters": {}, "feeds": {}, "cards": {}, "apis": {}, "websites": {}}
    website_downloads = {}
    for project, url in WEBSITES.items():
        spec = DATA['projects'][project]
        actual_url = spec.get('website_catalog_url', url)
        if actual_url not in website_downloads:
            target = cache_dir / ('website-' + project + '.html')
            state = _capture_download(actual_url, target, attempts=1)
            count = None
            catalogue = {}
            if state.get('status') == 'OK' and spec.get('website_catalogue_kind') == 'domdau':
                from website_catalogues import parse_domdau_catalogue
                try:
                    catalogue = parse_domdau_catalogue(target.read_text())
                    count = catalogue['lot_count']
                except (OSError, ValueError):
                    state = {**state, 'status': SOURCE_UNAVAILABLE,
                             'reason': 'Каталог сайта не подтверждён: неполные данные или изменившаяся схема'}
            pattern = spec.get('website_count_pattern')
            if state.get('status') == 'OK' and pattern:
                match = re.search(pattern, target.read_text(encoding='utf-8',errors='replace'))
                count = int(match.group(1)) if match else None
            website_downloads[actual_url] = {**state, **catalogue, 'lot_count': count, 'url': actual_url}
        statuses['websites'][project] = dict(website_downloads[actual_url])

    # Download each distinct client residential exchange once.
    xml_sources: dict[str, str] = {}
    for category_sources in MASTER_SOURCES.values():
        for project, source in category_sources.items():
            if source["kind"] in {"xml", "planeta", "macro", "cian", "profitbase"}:
                xml_sources[project] = str(source["url"])
    downloaded = {}
    for project, url in xml_sources.items():
        if url in downloaded:
            previous_project = downloaded[url]
            statuses['masters'][project] = dict(statuses['masters'][previous_project])
            if statuses['masters'][project].get('status') == 'OK':
                shutil.copyfile(_master_xml_path(cache_dir,previous_project), _master_xml_path(cache_dir,project))
        else:
            # Maternal CRM exports are generated on demand and can take longer
            # than static agency XML. Keep a bounded, separate timeout.
            statuses['masters'][project] = _capture_download(
                url, _master_xml_path(cache_dir,project), timeout=MASTER_DOWNLOAD_TIMEOUT_SECONDS
            )
            downloaded[url] = project

    # All 6feeds downloads are intentionally sequential: bursts against the
    # same host previously caused false SSL failures in production.
    for category, projects in FEEDS.items():
        statuses["feeds"][category] = {}
        for project, sources in projects.items():
            statuses["feeds"][category][project] = {}
            for source, url in sources.items():
                statuses["feeds"][category][project][source] = _capture_download(
                    url, _feed_path(cache_dir, category, project, source)
                )

    # Marketplace cards are secondary evidence.  A blocked public page remains
    # visible as "not automatically verified" but does not erase API evidence.
    for category, projects in PLATFORM_CARDS.items():
        statuses["cards"][category] = {}
        for project, sources in projects.items():
            statuses["cards"][category][project] = {}
            for source, url in sources.items():
                if source in {"cian", "avito"}:
                    statuses["cards"][category][project][source] = {
                        "status": "not_requested",
                        "reason": "контроль по API или XML; публичная страница не опрашивается",
                    }
                    continue
                statuses["cards"][category][project][source] = _capture_download(
                    url, _card_path(cache_dir, category, project, source), attempts=1
                )

    for category in CATEGORIES:
        statuses["apis"][category] = {}
        for source, account in API_ACCOUNTS.items():
            fetcher = fetch_cian_snapshot if account['source'] == 'cian' else fetch_avito_snapshot
            snapshot = fetcher(category, source)
            statuses["apis"][category][source] = snapshot
            _api_path(cache_dir, category, source).write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    (cache_dir / "source-statuses.json").write_text(
        json.dumps(statuses, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return statuses


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _unavailable_summary(status: dict[str, Any]) -> dict[str, Any]:
    return {
        **status,
        "status": SOURCE_UNAVAILABLE,
        "lot_count": None,
        "unique_id_count": None,
        "missing_id_count": None,
        "duplicate_ids": [],
        "deal_counts": {},
    }


def _master_for(
    cache_dir: Path, statuses: dict[str, Any], category: str, project: str
) -> tuple[list[Lot] | None, dict[str, Any]]:
    source = MASTER_SOURCES[category][project]
    if source['kind'] == 'unavailable':
        return None, _unavailable_summary({'reason': source['label']})
    if source["kind"] in {"xml", "planeta", "macro", "cian", "profitbase"}:
        status = statuses.get("masters", {}).get(project, {})
        if status.get("status") == SOURCE_UNAVAILABLE:
            return None, _unavailable_summary(status)
        try:
            if source["kind"] == "planeta":
                lots, meta = parse_planeta(_master_xml_path(cache_dir, project),
                    project=project, category=category, statuses=source["statuses"])
            elif source['kind'] == 'profitbase':
                lots, meta = parse_profitbase(_master_xml_path(cache_dir, project), project=project, category=category)
            elif source['kind'] == 'macro':
                lots, meta = parse_macro(_master_xml_path(cache_dir, project), project=project, category=category, source=source)
            elif source['kind'] == 'cian':
                lots, meta = parse_cian(_master_xml_path(cache_dir, project), project=project, category=category, source=source)
                if not lots:
                    raise ValueError('client XML has no residential inventory')
            else:
                lots, meta = parse_master_xml(_master_xml_path(cache_dir, project),
                    project=project, category=category, allowed_categories=source["categories"])
            return lots, {**summarize_lots(lots, meta), "status": "OK", "label": source.get("label"), "url": source["url"]}
        except (FileNotFoundError, ET.ParseError, ValueError) as error:
            failed = {
                "status": SOURCE_UNAVAILABLE,
                "reason": f"не удалось разобрать эталон ({type(error).__name__})",
            }
            return None, _unavailable_summary(failed)

    raise ValueError(f"неподдерживаемый тип эталона: {source.get('kind')}")


def _load_api_snapshot(cache_dir: Path, category: str, source: str) -> dict[str, Any]:
    value = _read_json(_api_path(cache_dir, category, source))
    if value:
        return value
    return {
        "status": "not_configured",
        "reason": "снимок API отсутствует",
        "checked_at": None,
    }


def analyze_api(
    category: str,
    source: str,
    snapshot: dict[str, Any],
    feeds_by_project: dict[str, dict[str, list[Lot]]],
) -> dict[str, Any]:
    active_projects = API_ACTIVE_PROJECTS[category][source]
    feed_source = API_ACCOUNTS[source]['source']
    if API_ACCOUNTS[source].get('enabled') is False:
        return {**snapshot, 'status': 'not_configured', 'comparison_status': 'error',
                'active_projects': list(active_projects), 'expected_feed_count': None,
                'actual_platform_count': None, 'reason': API_ACCOUNTS[source]['disabled_reason']}
    missing_projects = [
        project for project in active_projects
        if feed_source not in feeds_by_project.get(project, {})
    ]
    if missing_projects:
        return {
            **snapshot,
            "comparison_status": "error",
            "expected_feed_count": None,
            "reason": "сравнение API невозможно: не все активные XML доступны",
            "unavailable_feed_projects": missing_projects,
        }
    expected = sum(len(feeds_by_project[project][feed_source]) for project in active_projects)
    result = {**snapshot, "expected_feed_count": expected, "active_projects": list(active_projects)}
    if snapshot.get("status") != "ok":
        result["comparison_status"] = "error"
        return result
    if feed_source == "cian":
        actual = int(snapshot.get("published_count") or 0)
        import_errors = int(snapshot.get("import_error_count") or 0)
        result["actual_platform_count"] = actual
        result["comparison_status"] = (
            "ok" if actual == expected and import_errors == 0 else "error"
        )
        if actual != expected:
            result["reason"] = "число опубликованных объявлений отличается от XML"
        elif import_errors:
            result["reason"] = f"ошибок импорта Циан: {import_errors}"
    else:
        actual = int(snapshot.get("active_count") or 0)
        result["actual_platform_count"] = actual
        result["comparison_status"] = "ok" if actual == expected else "error"
        if actual != expected:
            result["reason"] = "число активных объявлений отличается от XML"
    return result


CARD_COUNT_PATTERNS = {
    "cian": (
        re.compile(r"(\d+)\s+квартир\w*\s+от\s+застройщика", re.IGNORECASE),
        re.compile(r"Квартиры\s+(\d+)", re.IGNORECASE),
    ),
    "yandex": (
        re.compile(r"Skolkovo\s*ONE.{0,160}?[—–:]\s*(\d+)\s+квартир", re.IGNORECASE),
        re.compile(r"(\d+)\s+квартир\w*\s+от\s+застройщика", re.IGNORECASE),
    ),
}


def _extract_card_count(source: str, document: str) -> int | None:
    plain = html.unescape(re.sub(r"<[^>]+>", " ", document))
    plain = re.sub(r"\s+", " ", plain)
    for pattern in CARD_COUNT_PATTERNS.get(source, ()):
        match = pattern.search(plain)
        if match:
            return _integer(match.group(1))
    return None


def analyze_cards(
    cache_dir: Path,
    statuses: dict[str, Any],
    category: str,
    feeds_by_project: dict[str, dict[str, list[Lot]]],
    api_checks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for project, sources in PLATFORM_CARDS.get(category, {}).items():
        results[project] = {}
        for source, url in sources.items():
            feed = feeds_by_project.get(project, {}).get(source)
            feed_count = len(feed) if feed is not None else None
            base = {
                "label": FEED_LABELS[source],
                "url": url,
                "feed_count": feed_count,
            }
            if feed is None:
                results[project][source] = {
                    **base,
                    "status": "source_unavailable",
                    "reason": "XML-фид площадки недоступен",
                }
                continue
            if source == "domclick":
                subsets = set(DATA['projects'][project].get('subset_feeds', []))
                project_counts = {key: len(value) for key, value in feeds_by_project[project].items() if key not in subsets}
                all_available = len(project_counts) == len(set(FEEDS[category][project])-subsets)
                result_status = "ok" if all_available and len(set(project_counts.values())) == 1 else "error"
                results[project][source] = {
                    **base,
                    "status": result_status,
                    "check_method": "feed_proxy",
                    "offer_count": feed_count,
                    "other_feed_counts": project_counts,
                    "reason": None if result_status == "ok" else "число лотов Домклик отличается от остальных XML",
                }
                continue

            download_status = (
                statuses.get("cards", {}).get(category, {}).get(project, {}).get(source, {})
            )
            count: int | None = None
            if download_status.get("status") == "OK":
                try:
                    document = _card_path(cache_dir, category, project, source).read_text(
                        encoding="utf-8", errors="replace"
                    )
                    count = _extract_card_count(source, document)
                except FileNotFoundError:
                    pass
            if count is not None:
                status = "ok" if count == feed_count else "error"
                results[project][source] = {
                    **base,
                    "status": status,
                    "check_method": "public_page",
                    "offer_count": count,
                    "reason": None if status == "ok" else "число на карточке отличается от XML",
                }
                continue
            api = api_checks.get(source)
            if api and api.get("comparison_status") == "ok":
                results[project][source] = {
                    **base,
                    "status": "covered_by_api",
                    "check_method": "category_api",
                    "offer_count": None,
                    "reason": "карточка не отдала число; категория подтверждена API",
                }
            else:
                results[project][source] = {
                    **base,
                    "status": "not_verified",
                    "check_method": "public_page",
                    "offer_count": None,
                    "reason": download_status.get("reason") or "число на карточке не найдено автоматически",
                }
    return results


def _card_has_error(cards: dict[str, Any]) -> bool:
    return any(
        card.get("status") in {"error", "source_unavailable"}
        for sources in cards.values()
        for card in sources.values()
    )


def analyze_directory(cache_dir: Path) -> dict[str, Any]:
    statuses = _read_json(cache_dir / "source-statuses.json")
    report: dict[str, Any] = {
        "client": CLIENT_NAME,
        "checked_at": _now(),
        "source_status": statuses,
        "categories": {},
    }
    for category in CATEGORIES:
        projects_result: dict[str, Any] = {}
        feeds_by_project: dict[str, dict[str, list[Lot]]] = {}
        category_error = False
        for project in FEEDS[category]:
            master_lots, master_summary = _master_for(cache_dir, statuses, category, project)
            source_lots: dict[str, list[Lot]] = {}
            feed_summaries: dict[str, Any] = {}
            comparisons: dict[str, Any] = {}
            for source in FEEDS[category][project]:
                download_status = (
                    statuses.get("feeds", {}).get(category, {}).get(project, {}).get(source, {})
                )
                if download_status.get("status") == SOURCE_UNAVAILABLE:
                    feed_summaries[source] = _unavailable_summary(download_status)
                    comparisons[source] = {
                        "status": SOURCE_UNAVAILABLE,
                        "master_count": master_summary.get("lot_count"),
                        "feed_count": None,
                        "missing_from_feed": [],
                        "extra_in_feed": [],
                        "field_mismatch_counts": {},
                        "field_mismatch_samples": [],
                        "error_count": 1,
                    }
                    category_error = True
                    continue
                try:
                    lots, meta = FEED_PARSERS[source](
                        _feed_path(cache_dir, category, project, source),
                        project=project,
                        category=category,
                    )
                    source_lots[source] = lots
                    marketing_issues = detect_marketing_issues(
                        _feed_path(cache_dir, category, project, source)
                    )
                    feed_summaries[source] = {
                        **summarize_lots(lots, meta),
                        "status": "OK",
                        "download": download_status,
                        "marketing_issues": marketing_issues,
                    }
                    if master_lots is None:
                        comparisons[source] = {
                            "status": SOURCE_UNAVAILABLE,
                            "master_count": None,
                            "feed_count": len(lots),
                            "missing_from_feed": [],
                            "extra_in_feed": [],
                            "field_mismatch_counts": {},
                            "field_mismatch_samples": [],
                            "error_count": 1,
                        }
                    else:
                        subsets = DATA['projects'][project].get('subset_feeds', [])
                        compare_master = master_lots
                        if source in subsets:
                            # The deliberately small XML must be a valid subset.
                            # Do not report every unselected master lot as missing.
                            ids = {lot.external_id for lot in lots}
                            compare_master = [lot for lot in master_lots if lot.external_id in ids]
                        comparisons[source] = compare_master_to_feed(compare_master, lots)
                    if comparisons[source]["status"] != "ok":
                        category_error = True
                    if marketing_issues:
                        category_error = True
                except (FileNotFoundError, ET.ParseError, ValueError) as error:
                    failed = {
                        "status": SOURCE_UNAVAILABLE,
                        "reason": f"не удалось разобрать XML ({type(error).__name__})",
                    }
                    feed_summaries[source] = _unavailable_summary(failed)
                    comparisons[source] = {
                        "status": SOURCE_UNAVAILABLE,
                        "master_count": master_summary.get("lot_count"),
                        "feed_count": None,
                        "missing_from_feed": [],
                        "extra_in_feed": [],
                        "field_mismatch_counts": {},
                        "field_mismatch_samples": [],
                        "error_count": 1,
                    }
                    category_error = True
            feeds_by_project[project] = source_lots
            subsets = DATA['projects'][project].get('subset_feeds', [])
            cross_feed = compare_feeds({s: lots for s, lots in source_lots.items() if s not in subsets})
            if cross_feed.get("status") != "ok":
                category_error = True
            if master_lots is None or master_summary.get("duplicate_ids"):
                category_error = True
            project_error = (
                master_lots is None
                or bool(master_summary.get("duplicate_ids"))
                or bool(master_summary.get("missing_id_count"))
                or any(item.get("status") != "ok" for item in comparisons.values())
                or cross_feed.get("status") != "ok"
                or any(item.get("marketing_issues") for item in feed_summaries.values())
            )
            if project_error:
                category_error = True
            projects_result[project] = {
                "status": "error" if project_error else "ok",
                "website": statuses.get('websites',{}).get(project,{}),
                "master": master_summary,
                "feeds": feed_summaries,
                "master_vs_feeds": comparisons,
                "cross_feed": cross_feed,
            }

        api_checks: dict[str, Any] = {}
        for source in API_ACCOUNTS:
            api_checks[source] = analyze_api(
                category,
                source,
                _load_api_snapshot(cache_dir, category, source),
                feeds_by_project,
            )
            if api_checks[source].get("comparison_status") != "ok":
                category_error = True
        cards = analyze_cards(
            cache_dir, statuses, category, feeds_by_project, api_checks
        )
        if _card_has_error(cards):
            category_error = True
        report["categories"][category] = {
            "label": CATEGORIES[category],
            "status": "error" if category_error else "ok",
            "projects": projects_result,
            "api_checks": api_checks,
            "platform_cards": cards,
        }
    report["overall_status"] = (
        "error"
        if any(value["status"] == "error" for value in report["categories"].values())
        else "ok"
    )
    return report


def _count(value: Any) -> str:
    return "—" if value is None else str(value)


def _short_ids(values: list[str], limit: int = 3) -> str:
    shown = ", ".join(values[:limit])
    if len(values) > limit:
        shown += f" и ещё {len(values) - limit}"
    return shown or "—"


def _feed_counts_line(feeds: dict[str, Any]) -> str:
    return ", ".join(
        f"{FEED_LABELS[source]} {_count(summary.get('lot_count'))}"
        for source, summary in feeds.items()
    )


def _project_problem_lines(project: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for source, result in project["master_vs_feeds"].items():
        label = FEED_LABELS[source]
        if result.get("status") == SOURCE_UNAVAILABLE:
            feed = project["feeds"].get(source, {})
            if feed.get("status") == SOURCE_UNAVAILABLE:
                lines.append(f"🔴 {label}: фид недоступен — {feed.get('reason', 'SOURCE_UNAVAILABLE')}")
            continue
        missing = result.get("missing_from_feed", [])
        extra = result.get("extra_in_feed", [])
        fields = result.get("field_mismatch_counts", {})
        if missing:
            lines.append(
                f"🔴 {label}: не выгружено {len(missing)} ({_short_ids(missing)})"
            )
        if extra:
            lines.append(f"🔴 {label}: лишних {len(extra)} ({_short_ids(extra)})")
        if fields:
            details = ", ".join(f"{FIELD_LABELS.get(key, key)} {value}" for key, value in fields.items())
            lines.append(f"🔴 {label}: расхождения полей — {details}")
        if result.get("duplicate_ids"):
            lines.append(f"🔴 {label}: повторяющихся ID — {len(result['duplicate_ids'])}")
        if result.get("missing_id_count"):
            lines.append(f"🔴 {label}: записей без ID — {result['missing_id_count']}")
    cross = project["cross_feed"]
    if cross.get("field_mismatch_counts"):
        details = ", ".join(
            f"{FIELD_LABELS.get(key, key)} {value}" for key, value in cross["field_mismatch_counts"].items()
        )
        lines.append(f"🔴 Фиды между собой: {details}")
    return lines


def _marketing_lines(project: dict[str, Any]) -> list[str]:
    """Independent of the master: its outage must not hide text problems."""
    lines = []
    unchecked = []
    for source, feed in project["feeds"].items():
        if feed.get("status") == SOURCE_UNAVAILABLE or "marketing_issues" not in feed:
            unchecked.append(FEED_LABELS[source])
            continue
        for issue in feed["marketing_issues"]:
            lines.append(f"🔴 {FEED_LABELS[source]} · текст акции: {issue}")
    if unchecked:
        lines.append("⚪ Даты и месяцы акций: не проверены — " + ", ".join(unchecked))
    elif not lines:
        lines.append("✅ Даты и месяцы акций: замечаний нет")
    return lines


def _platform_lines(category: dict[str, Any], project_key: str | None = None) -> list[str]:
    cards = category.get("platform_cards", {})
    if project_key is not None:
        cards = {project_key: cards[project_key]} if project_key in cards else {}
    if not cards:
        return []
    lines = []
    for project, sources in cards.items():
        for source, card in sources.items():
            status = card.get("status")
            api = category.get("api_checks", {}).get(source, {})
            if (
                status == "covered_by_api"
                and project in api.get("active_projects", [])
                and api.get("comparison_status") == "ok"
                and api.get("actual_platform_count") is not None
                and api.get("actual_platform_count") == api.get("expected_feed_count")
            ):
                # Account totals belong in the category block, not under each ЖК.
                continue
            icon = {
                "ok": "✅",
                "covered_by_api": "⚪",
                "not_verified": "⚪",
                "error": "🔴",
                "source_unavailable": "🔴",
            }.get(status, "⚪")
            label = FEED_LABELS[source] if project_key else f"{FEED_LABELS[source]} · {PROJECTS[project]}"
            if card.get("check_method") == "feed_proxy" and status == "ok":
                text = f"{_count(card.get('feed_count'))} по фиду; совпадает с остальными XML"
            elif status == "ok":
                text = f"{_count(card.get('offer_count'))} на площадке / {_count(card.get('feed_count'))} в XML"
            elif status == "covered_by_api":
                text = f"в фиде ЖК — {_count(card.get('feed_count'))}; число на карточке ЖК не проверено"
            elif status == "not_verified":
                text = "число на карточке ЖК не удалось прочитать; нужна ручная проверка"
            elif card.get("offer_count") is not None:
                text = f"{card['offer_count']} на карточке / {_count(card.get('feed_count'))} в XML — требуется проверить"
            else:
                text = card.get("reason") or "требуется проверить"
            lines.append(f"{icon} {label}: {text}")
    return ["Площадки:", *lines] if lines else []


def _api_lines(category: dict[str, Any]) -> list[str]:
    """Show account counts once, with the exact project scope used for XML totals."""
    lines = []
    for source, api in category.get("api_checks", {}).items():
        projects = api.get("active_projects", [])
        if len(projects) == 2 and set(projects) == set(category.get("projects", {})):
            scope = "оба ЖК"
        elif projects:
            scope = ", ".join(PROJECTS.get(project, project) for project in projects)
        else:
            scope = "итог по категории"
        actual = api.get("actual_platform_count")
        expected = api.get("expected_feed_count")
        complete = actual is not None and expected is not None
        matched = complete and actual == expected
        ok = api.get("comparison_status") == "ok" and matched
        actual_label = _count(actual) if actual is not None else "нет данных"
        expected_label = _count(expected) if expected is not None else "нет полных данных"
        detail = f"всего в кабинете — {actual_label}, в XML контролируемых ЖК — {expected_label}"
        if complete and not matched:
            result = f"Расхождение: {abs(actual - expected)}. Требуется проверить"
        elif ok:
            result = "Количество совпадает"
        elif matched:
            result = "Количество совпадает, но проверка API не пройдена"
        else:
            result = "Сравнение не выполнено"
        if not ok and api.get("reason"):
            result += f" — {api['reason']}"
        icon = "✅" if ok else "🔴"
        lines.append(f"{icon} {FEED_LABELS[source]} · {scope}: {detail}. {result}")
    return lines


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _checked_label(value: str) -> str:
    try:
        timestamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is not None:
            return timestamp.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y, %H:%M:%S МСК")
    except (ValueError, AttributeError):
        pass
    return value


def format_telegram(report: dict[str, Any]) -> str:
    """Telegram HTML; every value from sources is escaped, only our tags render."""
    overall_icon = "🟢" if report.get("overall_status") == "ok" else "🔴"
    lines = [f"{overall_icon} <b>{_escape(CLIENT_NAME)}</b>", "<i>Контроль выгрузок</i>"]
    for category_key in CATEGORIES:
        category = report["categories"][category_key]
        category_icon = "🟢" if category["status"] == "ok" else "🔴"
        lines.extend(["", f"{category_icon} <b>{_escape(CATEGORIES[category_key].upper())}</b>"])
        for project_key, project in category["projects"].items():
            master_count = project["master"].get("lot_count")
            lines.extend(["", f"<b>{_escape(PROJECTS[project_key])}</b>", f"<i>Эталон: {_escape(_count(master_count))}</i>"])
            if project["master"].get("excluded_reserved"):
                lines.append(f"Брони: {project['master']['excluded_reserved']} — отдельно, не ожидаются в рекламе")
            if project_key == "chasovaya":
                lines.append("Источник эталона: каталог клиента, связанный с сайтом")
            if project["master"].get("status") == SOURCE_UNAVAILABLE:
                lines.append(_escape(f"🔴 Эталон недоступен: {project['master'].get('reason', 'нет данных')}; сравнение отложено"))
            website = project.get('website',{})
            if website.get('lot_count') is not None:
                lines.append(_escape(f"Сайт: {website['lot_count']} квартир; дополнительный источник"))
            elif website.get('status') == SOURCE_UNAVAILABLE:
                lines.append('⚪ Сайт: временно недоступен; число квартир не проверено')
            else:
                lines.append('⚪ Сайт: отдельный счётчик квартир пока не подтверждён')
            lines.append(_escape(f"Фиды: {_feed_counts_line(project['feeds'])}"))
            problems = _project_problem_lines(project)
            if problems:
                lines.extend(_escape(line) for line in problems)
            elif project["master"].get("status") != SOURCE_UNAVAILABLE:
                lines.append("✅ Состав и проверяемые поля фидов совпадают с эталоном")
            lines.extend(_escape(line) for line in _marketing_lines(project))
            platform_lines = _platform_lines(category, project_key)
            if platform_lines:
                lines.extend(["", "<i>Площадки проекта</i>"])
                lines.extend(_escape(line) for line in platform_lines[1:])

        lines.extend(["", "<i>Объявления в кабинетах — по API</i>"])
        lines.extend(_escape(line) for line in _api_lines(category))
        if not category.get("platform_cards"):
            lines.append("<i>Карточки ЖК для категории не заданы; контроль по API.</i>")
    lines.extend(["", f"<i>Проверено: {_escape(_checked_label(report['checked_at']))}</i>"])
    message = "\n".join(lines)
    if len(message) <= MAX_TELEGRAM_MESSAGE:
        return message
    return _format_compact_telegram(report)


def _format_compact_telegram(report: dict[str, Any]) -> str:
    """Preserve the residential category and timestamp during a large outage."""
    icon = "🟢" if report.get("overall_status") == "ok" else "🔴"
    lines = [f"{icon} <b>{_escape(CLIENT_NAME)}</b>", "<i>Контроль выгрузок · краткий отчёт</i>"]
    for category_key in CATEGORIES:
        category = report["categories"][category_key]
        icon = "🟢" if category["status"] == "ok" else "🔴"
        lines.extend(["", f"{icon} <b>{_escape(CATEGORIES[category_key].upper())}</b>"])
        for project_key, project in category["projects"].items():
            lines.extend(["", f"<b>{_escape(PROJECTS[project_key])}</b>",
                f"<i>Эталон: {_escape(_count(project['master'].get('lot_count')))}</i>",
                _escape("Фиды: " + _feed_counts_line(project["feeds"]))])
            missing = sum(len(value.get("missing_from_feed", [])) for value in project["master_vs_feeds"].values())
            extra = sum(len(value.get("extra_in_feed", [])) for value in project["master_vs_feeds"].values())
            fields = sum(sum(value.get("field_mismatch_counts", {}).values()) for value in project["master_vs_feeds"].values())
            unavailable = sum(value.get("status") == SOURCE_UNAVAILABLE for value in project["master_vs_feeds"].values())
            marketing = sum(bool(value.get("marketing_issues")) for value in project["feeds"].values())
            lines.append(
                f"Суммарно по XML: не выгружено {missing}, лишних {extra}, "
                f"расхождений полей {fields}, недоступных проверок {unavailable}, "
                f"фидов с замечаниями по акциям {marketing}."
            )
            if marketing:
                lines.extend(_escape(line) for line in _marketing_lines(project) if 'текст акции:' in line)
        lines.extend(["", "<i>Объявления в кабинетах — по API</i>"])
        lines.extend(_escape(line) for line in _api_lines(category))
    lines.extend(["", "Подробности по лотам и карточкам — в JSON-отчёте запуска."])
    lines.append(f"<i>Проверено: {_escape(_checked_label(report['checked_at']))}</i>")
    return "\n".join(lines)


def _connect_ipv6(address, *args, **kwargs):
    addresses = socket.getaddrinfo(address[0], address[1], socket.AF_INET6, socket.SOCK_STREAM)
    return socket.create_connection((addresses[0][4][0], address[1]), *args, **kwargs)


class _IPv6HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_ipv6


class _IPv6HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_IPv6HTTPSConnection, request, context=self._context)


def _telegram_urlopen(request, *, timeout):
    # Shared IPv4-first transport; subscriber intake and scheduled delivery use
    # the same healthy route, with validated TLS and IPv6 fallback.
    import importlib.util
    module_name = "advelop_intake_transport"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, "/opt/advelop-command-intake/transport.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[module_name] = module
    return sys.modules[module_name].urlopen(request, timeout=timeout)


def send_telegram(message: str, *, chat_id: str | None = None) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    target = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not target:
        raise RuntimeError("TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID не настроены")
    data = urllib.parse.urlencode(
        {"chat_id": target, "text": message, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST"
    )
    for attempt in range(3):
        try:
            with _telegram_urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise
            if attempt == 2:
                raise
        except (OSError, http.client.HTTPException):
            if attempt == 2:
                raise
        time.sleep(2)
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError("Telegram не подтвердил отправку")
    return payload


def _telegram_state_request(payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = os.environ.get("TELEGRAM_LATEST_MESSAGE_URL")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not endpoint:
        return {}
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не настроен")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-DAR-Bot-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError("не удалось сохранить последнее сообщение для /start")
    return payload


def sync_latest_message(message: str, checked_at: str) -> dict[str, Any]:
    payload = {"action": "sync_latest", "message": message,
               "checked_at": checked_at, "parse_mode": "HTML"}
    owner = os.environ.get("TELEGRAM_CHAT_ID")
    if owner:
        payload["owner_chat_id"] = owner
    return _telegram_state_request(payload)


def broadcast_telegram(message: str, checked_at: str) -> dict[str, Any]:
    """Send one HTML digest to each DAR subscriber, not only the configured owner."""
    if not os.environ.get("TELEGRAM_LATEST_MESSAGE_URL"):
        send_telegram(message)
        return {"subscribers": 1, "delivered": 1, "blocked": 0, "failed": 0}
    owner = os.environ.get("TELEGRAM_CHAT_ID")
    registry_available = True
    try:
        sync = sync_latest_message(message, checked_at)
        if sync.get("stored") is False and sync.get("reason") == "older_report":
            print("Telegram: более новый отчёт уже сохранён; старый не рассылается")
            return {"subscribers": 0, "delivered": 0, "blocked": 0, "failed": 0,
                    "skipped_older_report": True}
        recipients = sync.get("recipients")
        if (not isinstance(recipients, list) or len(recipients) > 100
                or any(not isinstance(item, dict)
                       or not re.fullmatch(r"[1-9][0-9]{0,19}", str(item.get("chat_id")))
                       or type(item.get("revision")) is not int for item in recipients)):
            raise RuntimeError("DAR: список подписчиков недоступен или некорректен")
    except (OSError, RuntimeError, ValueError) as error:
        # Registry downtime must not silence the configured owner.  The next
        # scheduled run retries the registry automatically.
        print(f"Telegram registry unavailable; owner fallback: {type(error).__name__}")
        registry_available = False
        recipients = [{"chat_id": owner, "revision": -1}] if owner else []
    if not recipients and owner:
        recipients = [{"chat_id": owner, "revision": -1}]
    if not recipients:
        if not registry_available:
            raise RuntimeError("DAR: нет доступных получателей Telegram; реестр недоступен")
        print("Telegram: отчёт сохранён; пока никто не нажал /start")
        return {"subscribers": 0, "delivered": 0, "blocked": 0, "failed": 0}
    recipients = list({str(item["chat_id"]): item for item in recipients}.values())
    blocked = []
    failed = 0
    for position, recipient in enumerate(recipients):
        try:
            send_telegram(message, chat_id=str(recipient["chat_id"]))
        except urllib.error.HTTPError as error:
            print(f"Telegram delivery HTTP {error.code}")
            if error.code == 403:
                blocked.append(recipient)
            else:
                failed += 1
        except (OSError, RuntimeError, ValueError) as error:
            print(f"Telegram delivery failed: {type(error).__name__}")
            failed += 1
        if position + 1 < len(recipients):
            time.sleep(0.05)
    summary = {"subscribers": len(recipients),
               "delivered": len(recipients) - len(blocked) - failed,
               "blocked": len(blocked), "failed": failed}
    print("Telegram broadcast: " + " ".join(f"{key}={value}" for key, value in summary.items()))
    if blocked and registry_available:
        try:
            _telegram_state_request({"action": "mark_unreachable", "recipients": blocked})
        except (OSError, RuntimeError, ValueError) as error:
            print(f"Telegram registry cleanup deferred: {type(error).__name__}")
    if failed:
        raise RuntimeError(f"DAR: не удалось доставить отчёт {failed} подписчикам")
    return summary


def save_latest_telegram_message(state_path: Path, message: str, checked_at: str | None, *, parse_mode: str | None = None) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {"latest_message": message, "latest_checked_at": checked_at, "saved_at": _now(),
             "parse_mode": "HTML" if parse_mode == "HTML" else None},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def telegram_start_payload(update: dict[str, Any], state_path: Path) -> dict[str, str] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    text = str(message.get("text") or "")
    if not (
        re.match(r"^/start(?:@\w+)?(?:\s|$)", text, re.IGNORECASE)
        and isinstance(chat, dict)
        and chat.get("type") == "private"
        and chat.get("id") is not None
    ):
        return None
    state = _read_json(state_path)
    latest = state.get("latest_message")
    if not isinstance(latest, str) or not latest.strip():
        return None
    result = {"chat_id": str(chat["id"]), "text": latest}
    if state.get("parse_mode") == "HTML":
        result["parse_mode"] = "HTML"
    return result


def problem_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "overall_status": report.get("overall_status"),
        "categories": {
            category: {
                "status": value.get("status"),
                "projects": {
                    project: {
                        "status": project_value.get("status"),
                        "comparisons": project_value.get("master_vs_feeds"),
                        "marketing": {
                            source: summary.get("marketing_issues", [])
                            for source, summary in project_value.get("feeds", {}).items()
                        },
                    }
                    for project, project_value in value.get("projects", {}).items()
                },
                "apis": {
                    source: {
                        "status": api.get("comparison_status"),
                        "expected": api.get("expected_feed_count"),
                        "actual": api.get("actual_platform_count"),
                    }
                    for source, api in value.get("api_checks", {}).items()
                },
            }
            for category, value in report.get("categories", {}).items()
        },
    }


def should_notify(report: dict[str, Any], state_path: Path, threshold: int) -> bool:
    payload = problem_payload(report)
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    previous = _read_json(state_path)
    has_problem = report.get("overall_status") == "error"
    same = previous.get("last_fingerprint") == fingerprint
    consecutive = int(previous.get("consecutive", 0)) + 1 if has_problem and same else int(has_problem)
    active_alert = bool(previous.get("active_alert"))
    notify = False
    if has_problem and consecutive >= max(1, threshold):
        notify = previous.get("last_notified_fingerprint") != fingerprint
        active_alert = True
    elif not has_problem and active_alert:
        notify = True
        active_alert = False
    state = {
        "last_fingerprint": fingerprint,
        "last_notified_fingerprint": (
            fingerprint if notify and has_problem else previous.get("last_notified_fingerprint")
        ),
        "consecutive": consecutive,
        "active_alert": active_alert,
        "updated_at": report.get("checked_at"),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return notify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument("--force-notify", action="store_true")
    parser.add_argument("--state-file", type=Path, default=Path(".state.json"))
    parser.add_argument("--notify-after", type=int, default=2)
    parser.add_argument('--refresh-site-availability', action='store_true',
                        help='Refresh only public website status in an existing --output report')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.refresh_site_availability:
        from site_availability_integration import read_existing_report
        report = read_existing_report(args.output)
    else:
        if not args.offline:
            download_sources(args.cache_dir)
        report = analyze_directory(args.cache_dir)
    from site_availability_integration import check_report_sites
    check_report_sites(report, CLIENT_NAME, PROJECTS, WEBSITES, offline=args.offline)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    message = format_telegram(report)
    print(message)
    if args.send_telegram:
        pending_state = args.state_file.with_name(args.state_file.name + ".pending")
        pending_state.unlink(missing_ok=True)
        if args.state_file.exists():
            shutil.copyfile(args.state_file, pending_state)
        due = args.force_notify or should_notify(report, pending_state, args.notify_after)
        if due:
            broadcast_telegram(message, report["checked_at"])
        else:
            print("Telegram: сообщение подавлено до подтверждения повторной ошибки")
        # Advance alert state only after successful delivery. A failed send
        # leaves the previous state intact so a retry is not suppressed.
        if pending_state.exists():
            pending_state.replace(args.state_file)
    return 0


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from publication_control import install as _install_publication_control
_install_publication_control(sys.modules[__name__], CLIENT_NAME)

if __name__ == "__main__":
    sys.exit(main())
