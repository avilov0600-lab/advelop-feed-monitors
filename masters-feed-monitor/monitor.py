#!/usr/bin/env python3
"""Monitor the MASTERS inventory, marketplace XML feeds, and live listings.

The script uses only the Python standard library.  It treats the client XML as
the source of truth, checks all configured marketplace feeds against it, and
uses the official site as a secondary control.  Cian and Avito can additionally
be checked through their authenticated APIs.  Telegram delivery is optional.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import datetime as dt
import decimal
import hashlib
import html as html_module
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo
from floor_control import height_lines, non_height_counts


SITE_API_URL = (
    "https://cg-projects.ru/api/properties/"
    "?completion=2029-4&deal=sell&limit=1000&offset=0&order=price"
    "&project=masters&type=flats&with_pagination=true"
)

MASTER_FEED_URL = (
    "https://pics.capitalgroup.ru/pic/%D0%91%D0%BE%D0%B9/"
    "%D0%A0%D0%9A_%D0%A1%D0%B0%D0%B9%D1%82_"
    "%D0%92%D0%B8%D0%BA%D1%82%D0%BE%D1%80%D0%B5%D0%BD%D0%BA%D0%BE.xml"
)

# In the Capital Group source this status is the set exported to marketplaces.
# The other observed status (686340001) is intentionally absent from every
# outbound feed and from the active inventory cross-check.
MASTER_AVAILABLE_STATUS = "686340000"

FEED_URLS = {
    "cian": "https://newfeed.6feeds.ru/feeds/static/masters_adv/cian",
    "yandex": "https://newfeed.6feeds.ru/feeds/static/masters_adv/yandex",
    "avito": "https://newfeed.6feeds.ru/feeds/static/masters_adv/avito",
    "domclick": "https://newfeed.6feeds.ru/feeds/static/masters_adv/domclick",
    "m2": "https://newfeed.6feeds.ru/feeds/static/masters_adv/m2",
}

PLATFORM_PAGES = {
    "cian": {
        "label": "Циан",
        "url": "https://www.cian.ru/zhiloy-kompleks-premialnyy-dom-masters-moskva-5747793/",
        "feed": "cian",
        "count_patterns": (
            r"(\d+)\s+квартир\s+от\s+застройщика",
            r"Квартиры\s+(\d+)",
        ),
    },
    "yandex_realty": {
        "label": "Яндекс Недвижимость",
        "url": "https://realty.yandex.ru/moskva_i_moskovskaya_oblast/kupit/novostrojka/po-ul-viktorenko-4540998/",
        "feed": "yandex",
        "count_patterns": (
            r"МАСТЕРС[^<]{0,200}?—\s*(\d+)\s+квартир",
            r"продаж[а-я]*\s+(\d+)\s+квартир",
        ),
    },
    "avito": {
        "label": "Авито",
        "url": "https://www.avito.ru/catalog/novostroyki/moskva/zhk_novyy_premialnyy_dom/14626576",
        "feed": "avito",
        "count_patterns": (),
        "automated": False,
    },
    "domclick": {
        "label": "Домклик",
        "url": "https://domclick.ru/complexes/zhk-v-khoroshevskom-raione__125616",
        "feed": "domclick",
        "count_patterns": (r"(\d+)\s+предложений\s+от\s+застройщика",),
        "automated": True,
    },
    "m2": {
        "label": "М2",
        "url": "https://m2.ru/moskva/novostroyki/zhk-novii-premialnii-dom-14443/",
        "feed": "m2",
        "count_patterns": (r"(\d+)\s+предложений",),
    },
}

OFFER_FEEDS = ("cian", "yandex", "avito", "domclick", "m2")
FEED_LABELS = {
    "cian": "Циан",
    "yandex": "Яндекс Недвижимость",
    "avito": "Авито",
    "domclick": "Домклик",
    "m2": "М2",
}
STALE_PATTERNS = {
    "july_discount": re.compile(r"скидк\w*\s+10%\s+в\s+июл", re.IGNORECASE),
}
DECIMAL_ZERO = decimal.Decimal("0")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
CIAN_API_BASE = "https://public-api.cian.ru"
AVITO_API_BASE = "https://api.avito.ru"
AVITO_PROJECT_ADDRESS_MARKER = "викторенко"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 2
DOWNLOAD_TIMEOUT_SECONDS = 10
SOURCE_STATUS_FILENAME = "source-downloads.json"


@dataclasses.dataclass(frozen=True)
class Lot:
    source: str
    external_id: str
    price: int | None
    base_price: int | None
    area: decimal.Decimal | None
    rooms: int | None
    floor: int | None
    floors_total: int | None
    section: str | None = None
    apartment: str | None = None
    address: str | None = None
    description: str = ""
    image_count: int = 0
    site_id: int | None = None

    def compact(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        if self.area is not None:
            data["area"] = str(self.area)
        data.pop("description", None)
        return data


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _strip_namespaces(root: ET.Element) -> ET.Element:
    for element in root.iter():
        element.tag = _local_name(element.tag)
    return root


def _text(element: ET.Element | None, path: str, default: str = "") -> str:
    if element is None:
        return default
    found = element.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        cleaned = re.sub(r"\s+", "", str(value)).replace(",", ".")
        return int(decimal.Decimal(cleaned))
    except (decimal.InvalidOperation, ValueError):
        return None


def _decimal(value: Any) -> decimal.Decimal | None:
    if value in (None, ""):
        return None
    try:
        cleaned = re.sub(r"\s+", "", str(value)).replace(",", ".")
        return decimal.Decimal(cleaned)
    except decimal.InvalidOperation:
        return None


def _rooms(value: str, description: str = "", studio_value: str = "") -> int | None:
    if studio_value.strip().lower() in {"1", "true", "yes", "да"}:
        return 0
    if "студи" in value.lower() or "студи" in description[:120].lower():
        return 0
    return _integer(value)


def _section(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    match = re.search(r"(?:секц(?:ия|ии)?\s*)?(\d+)", value, re.IGNORECASE)
    return match.group(1) if match else None


def _xml_root(path: Path) -> ET.Element:
    return _strip_namespaces(ET.parse(path).getroot())


def parse_cian(path: Path) -> tuple[list[Lot], dict[str, Any]]:
    root = _xml_root(path)
    lots: list[Lot] = []
    for obj in root.findall("object"):
        description = _text(obj, "Description")
        lots.append(
            Lot(
                source="cian",
                external_id=_text(obj, "ExternalId"),
                price=_integer(_text(obj, "BargainTerms/Price")),
                base_price=None,
                area=_decimal(_text(obj, "TotalArea")),
                rooms=_rooms(_text(obj, "FlatRoomsCount"), description),
                floor=_integer(_text(obj, "FloorNumber")),
                floors_total=_integer(_text(obj, "Building/FloorsCount")),
                apartment=_text(obj, "Apartment") or None,
                address=_text(obj, "Address") or None,
                description=description,
                image_count=len(obj.findall("Photos/PhotoSchema")),
            )
        )
    return lots, {"feed_version": _text(root, "feed_version")}


def parse_avito(path: Path) -> tuple[list[Lot], dict[str, Any]]:
    root = _xml_root(path)
    lots: list[Lot] = []
    dates: list[str] = []
    for ad in root.findall("Ad"):
        description = _text(ad, "Description")
        date_begin = _text(ad, "DateBegin")
        if date_begin:
            dates.append(date_begin)
        lots.append(
            Lot(
                source="avito",
                external_id=_text(ad, "Id"),
                price=_integer(_text(ad, "Price")),
                base_price=None,
                area=_decimal(_text(ad, "Square")),
                rooms=_rooms(_text(ad, "Rooms"), description),
                floor=_integer(_text(ad, "Floor")),
                floors_total=_integer(_text(ad, "Floors")),
                section=_section(_text(ad, "DevelopmentsBuildingName")),
                apartment=_text(ad, "ApartmentNumber") or None,
                address=_text(ad, "Address") or None,
                description=description,
                image_count=len(ad.findall("Images/*")),
            )
        )
    return lots, {
        "format_version": root.attrib.get("formatVersion"),
        "date_begin_min": min(dates) if dates else None,
        "date_begin_max": max(dates) if dates else None,
    }


def parse_yrl(path: Path, source: str) -> tuple[list[Lot], dict[str, Any]]:
    root = _xml_root(path)
    lots: list[Lot] = []
    for offer in root.findall("offer"):
        description = _text(offer, "description")
        base_price = _integer(_text(offer, "price/value"))
        final_price = _integer(_text(offer, "discount/final-price"))
        lots.append(
            Lot(
                source=source,
                external_id=offer.attrib.get("internal-id", ""),
                price=final_price or base_price,
                base_price=base_price if final_price else None,
                area=_decimal(_text(offer, "area/value")),
                rooms=_rooms(
                    _text(offer, "rooms"), description, _text(offer, "studio")
                ),
                floor=_integer(_text(offer, "floor")),
                floors_total=_integer(_text(offer, "floors-total")),
                section=_section(_text(offer, "building-section")),
                apartment=_text(offer, "location/apartment") or None,
                address=_text(offer, "location/address") or None,
                description=description,
                image_count=len(offer.findall("image")) + len(offer.findall("photo")),
            )
        )
    return lots, {"generation_date": _text(root, "generation-date") or None}


def parse_domclick(path: Path) -> tuple[list[Lot], dict[str, Any]]:
    root = _xml_root(path)
    complex_node = root.find("complex")
    address = _text(complex_node, "address") or None
    lots: list[Lot] = []
    for building in root.findall("complex/buildings/building"):
        floors_total = _integer(_text(building, "floors"))
        section = _section(_text(building, "name"))
        for flat in building.findall("flats/flat"):
            description = _text(flat, "description")
            lots.append(
                Lot(
                    source="domclick",
                    external_id=_text(flat, "flat_id"),
                    price=_integer(_text(flat, "price")),
                    base_price=None,
                    area=_decimal(_text(flat, "area")),
                    rooms=_rooms(_text(flat, "room"), description),
                    floor=_integer(_text(flat, "floor")),
                    floors_total=floors_total,
                    section=section,
                    apartment=_text(flat, "apartment") or None,
                    address=address,
                    description=description,
                    image_count=len(flat.findall("plans/plan")),
                )
            )
    return lots, {
        "complex_id": _text(complex_node, "id") or None,
        "complex_name": _text(complex_node, "name") or None,
    }


def parse_master(path: Path) -> tuple[list[Lot], dict[str, Any]]:
    """Parse the client XML and return only properties intended for export.

    ``mcdsoft_high_floor`` is a highest numbered floor, while marketplace
    schemas expose a storey count.  The project numbers its first residential
    floor as 2, so a known high floor of 15 corresponds to 14 storeys.  Other
    sections currently omit this optional value and are simply not compared on
    total storeys.
    """
    root = _xml_root(path)
    lots: list[Lot] = []
    status_counts: collections.Counter[str] = collections.Counter()
    total_property_count = 0
    modified_dates: list[str] = []
    for section_node in root.findall(".//mcdsoft_section"):
        raw_high_floor = _integer(_text(section_node, "mcdsoft_high_floor"))
        floors_total = raw_high_floor - 1 if raw_high_floor else None
        section_number = _section(_text(section_node, "mcdsoft_number"))
        for prop in section_node.findall("mcdsoft_property"):
            total_property_count += 1
            status = _text(prop, "mcdsoft_status_chessmatches")
            status_counts[status or "missing"] += 1
            modified = _text(prop, "mcdsoft_modifiedon")
            if modified:
                modified_dates.append(modified)
            if status != MASTER_AVAILABLE_STATUS:
                continue
            lots.append(
                Lot(
                    source="master",
                    external_id=_text(prop, "mcdsoft_propertyid"),
                    price=_integer(_text(prop, "mcdsoft_price")),
                    base_price=None,
                    area=_decimal(_text(prop, "mcdsoft_whole_project_area")),
                    rooms=_rooms(
                        _text(prop, "cg_number_rooms_site")
                        or _text(prop, "mcdsoft_number_rooms"),
                        studio_value=_text(prop, "mcdsoft_isstudio"),
                    ),
                    floor=_integer(_text(prop, "mcdsoft_floor_height")),
                    floors_total=floors_total,
                    section=_section(_text(prop, "mcdsoft_section_number"))
                    or section_number,
                    apartment=_text(prop, "mcdsoft_relevant_number") or None,
                    description="",
                    image_count=len(prop.findall(".//mcdsoft_photo")),
                )
            )
    return lots, {
        "total_property_count": total_property_count,
        "available_status": MASTER_AVAILABLE_STATUS,
        "excluded_property_count": total_property_count - len(lots),
        "status_counts": dict(status_counts),
        "modified_at_max": max(modified_dates) if modified_dates else None,
    }


def parse_site(path: Path) -> tuple[list[Lot], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload,dict) or not isinstance(payload.get('results'),list):
        raise ValueError('Site inventory schema changed')
    if any(not isinstance(row,dict) or row.get('id') is None for row in payload['results']):
        raise ValueError('Site inventory rows invalid')
    if payload.get('count') is not None and _integer(payload['count'])!=len(payload['results']):
        raise ValueError('Site inventory is incomplete')
    lots: list[Lot] = []
    for item in payload.get("results", []):
        section = item.get("section") or {}
        floor = item.get("floor") or {}
        lots.append(
            Lot(
                source="site",
                external_id=f"site:{item.get('id')}",
                site_id=_integer(item.get("id")),
                price=_integer(item.get("price")),
                base_price=_integer(item.get("original_price")),
                area=_decimal(item.get("area")),
                rooms=_integer(item.get("rooms")),
                floor=_integer(floor.get("number")),
                floors_total=_integer(section.get("floors_count")),
                section=_section(str(section.get("number", ""))),
                description=str(item.get("discount_description") or ""),
                image_count=1 if (item.get("plan") or {}).get("plan_with_furniture") else 0,
            )
        )
    return lots, {
        "declared_count": _integer(payload.get("count")),
        "result_count": len(lots),
    }


PARSERS = {
    "cian": parse_cian,
    "avito": parse_avito,
    "domclick": parse_domclick,
}


def parse_feed(source: str, path: Path) -> tuple[list[Lot], dict[str, Any]]:
    if source in PARSERS:
        return PARSERS[source](path)
    return parse_yrl(path, source)


def _duplicates(lots: Iterable[Lot]) -> list[str]:
    counts = collections.Counter(lot.external_id for lot in lots)
    return sorted(key for key, count in counts.items() if key and count > 1)


def _room_counts(lots: Iterable[Lot]) -> dict[str, int]:
    counts = collections.Counter(lot.rooms for lot in lots)
    return {
        ("studio" if rooms == 0 else str(rooms) if rooms is not None else "missing"): count
        for rooms, count in sorted(counts.items(), key=lambda item: (item[0] is None, item[0]))
    }


def _feed_summary(lots: list[Lot], meta: dict[str, Any]) -> dict[str, Any]:
    prices = [lot.price for lot in lots if lot.price is not None]
    areas = [lot.area for lot in lots if lot.area is not None]
    ids = [lot.external_id for lot in lots if lot.external_id]
    stale = {
        name: sum(bool(pattern.search(lot.description)) for lot in lots)
        for name, pattern in STALE_PATTERNS.items()
    }
    return {
        **meta,
        "lot_count": len(lots),
        "unique_id_count": len(set(ids)),
        "duplicate_ids": _duplicates(lots),
        "missing_id_count": sum(not lot.external_id for lot in lots),
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "min_area": str(min(areas)) if areas else None,
        "max_area": str(max(areas)) if areas else None,
        "rooms": _room_counts(lots),
        "stale_content": stale,
    }


def compare_offer_feeds(feed_lots: dict[str, list[Lot]]) -> dict[str, Any]:
    available_sources = [source for source in OFFER_FEEDS if source in feed_lots]
    unavailable_sources = [
        source for source in OFFER_FEEDS if source not in feed_lots
    ]
    if not available_sources:
        return {
            "status": "PARTIAL",
            "available_sources": [],
            "unavailable_sources": unavailable_sources,
            "common_id_count": 0,
            "id_differences": {},
            "field_mismatch_counts": {},
            "source_outlier_counts": {},
            "field_mismatch_samples": [],
        }

    baseline = "cian" if "cian" in feed_lots else available_sources[0]
    baseline_ids = {lot.external_id for lot in feed_lots[baseline]}
    id_diffs: dict[str, Any] = {}
    for source in available_sources:
        ids = {lot.external_id for lot in feed_lots[source]}
        id_diffs[source] = {
            "baseline": baseline,
            "missing_vs_baseline": sorted(baseline_ids - ids),
            "extra_vs_baseline": sorted(ids - baseline_ids),
            # Backward-compatible keys retained while Cian is available.
            "missing_vs_cian": sorted(baseline_ids - ids),
            "extra_vs_cian": sorted(ids - baseline_ids),
        }

    indexed = {
        source: {lot.external_id: lot for lot in lots}
        for source, lots in feed_lots.items()
        if source in OFFER_FEEDS
    }
    common_ids = set.intersection(*(set(rows) for rows in indexed.values()))
    mismatch_counts: collections.Counter[str] = collections.Counter()
    source_outlier_counts: collections.Counter[str] = collections.Counter()
    mismatch_samples: list[dict[str, Any]] = []
    # Total storeys is deliberately excluded here.  Domclick exposes one value
    # at building level, while the other schemas expose it per section.  That
    # field is checked only against the client source where it is comparable.
    fields = ("price", "area", "rooms", "floor", "section", "apartment")
    for external_id in sorted(common_ids):
        mismatches: dict[str, dict[str, Any]] = {}
        for field in fields:
            values = {
                source: getattr(rows[external_id], field)
                for source, rows in indexed.items()
            }
            comparable = {str(value) for value in values.values() if value is not None}
            if len(comparable) > 1:
                mismatch_counts[field] += 1
                mismatches[field] = {
                    source: (str(value) if isinstance(value, decimal.Decimal) else value)
                    for source, value in values.items()
                }
                non_null_values = [value for value in values.values() if value is not None]
                if non_null_values:
                    value_counts = collections.Counter(map(str, non_null_values))
                    majority_value, majority_count = value_counts.most_common(1)[0]
                    if list(value_counts.values()).count(majority_count) == 1:
                        for source, value in values.items():
                            if value is not None and str(value) != majority_value:
                                source_outlier_counts[f"{source}.{field}"] += 1
        if mismatches and len(mismatch_samples) < 20:
            mismatch_samples.append({"external_id": external_id, "fields": mismatches})

    return {
        "status": "PARTIAL" if unavailable_sources else "OK",
        "available_sources": available_sources,
        "unavailable_sources": unavailable_sources,
        "common_id_count": len(common_ids),
        "id_differences": id_diffs,
        "field_mismatch_counts": dict(mismatch_counts),
        "source_outlier_counts": dict(source_outlier_counts),
        "field_mismatch_samples": mismatch_samples,
    }


def compare_master_to_feeds(
    master_lots: list[Lot], feed_lots: dict[str, list[Lot]]
) -> dict[str, Any]:
    """Compare every outbound XML directly with the client source by UUID."""
    master_by_id = {lot.external_id: lot for lot in master_lots}
    source_results: dict[str, Any] = {}
    aggregate_field_mismatches: collections.Counter[str] = collections.Counter()
    total_id_difference_count = 0

    for source in OFFER_FEEDS:
        if source not in feed_lots:
            source_results[source] = {
                "status": SOURCE_UNAVAILABLE,
                "feed_count": None,
                "missing_from_feed": [],
                "extra_in_feed": [],
                "field_mismatch_counts": {},
                "field_mismatch_samples": [],
            }
            continue
        rows = {lot.external_id: lot for lot in feed_lots[source]}
        missing = sorted(set(master_by_id) - set(rows))
        extra = sorted(set(rows) - set(master_by_id))
        total_id_difference_count += len(missing) + len(extra)
        mismatch_counts: collections.Counter[str] = collections.Counter()
        mismatch_samples: list[dict[str, Any]] = []

        for external_id in sorted(set(master_by_id) & set(rows)):
            master_row = master_by_id[external_id]
            feed_row = rows[external_id]
            mismatches: dict[str, Any] = {}
            fields = ["area", "rooms", "floor", "apartment"]
            if source == "yandex":
                fields.append("section")
            for field in fields:
                master_value = getattr(master_row, field)
                feed_value = getattr(feed_row, field)
                if master_value != feed_value:
                    mismatch_counts[field] += 1
                    mismatches[field] = {
                        "master": str(master_value)
                        if isinstance(master_value, decimal.Decimal)
                        else master_value,
                        "feed": str(feed_value)
                        if isinstance(feed_value, decimal.Decimal)
                        else feed_value,
                    }

            # The source carries a known storey count only for some sections.
            # Missing source values are not evidence of an outbound error.
            if (
                master_row.floors_total is not None
                and feed_row.floors_total is not None
                and master_row.floors_total != feed_row.floors_total
            ):
                mismatch_counts["floors_total"] += 1
                mismatches["floors_total"] = {
                    "master": master_row.floors_total,
                    "feed": feed_row.floors_total,
                }

            # Yandex preserves the undiscounted source price separately.  The
            # other schemas only carry the promotional final price, whose rule
            # is external to this source and is checked consistently between
            # outbound feeds instead.
            if source == "yandex":
                yandex_source_price = feed_row.base_price or feed_row.price
                if master_row.price != yandex_source_price:
                    mismatch_counts["base_price"] += 1
                    mismatches["base_price"] = {
                        "master": master_row.price,
                        "feed": yandex_source_price,
                    }

            if mismatches and len(mismatch_samples) < 20:
                mismatch_samples.append(
                    {"external_id": external_id, "fields": mismatches}
                )

        aggregate_field_mismatches.update(mismatch_counts)
        source_results[source] = {
            "status": "OK",
            "feed_count": len(rows),
            "missing_from_feed": missing,
            "extra_in_feed": extra,
            "field_mismatch_counts": dict(mismatch_counts),
            "field_mismatch_samples": mismatch_samples,
        }

    return {
        "status": (
            "PARTIAL"
            if any(source not in feed_lots for source in OFFER_FEEDS)
            else "OK"
        ),
        "unavailable_sources": [
            source for source in OFFER_FEEDS if source not in feed_lots
        ],
        "master_count": len(master_lots),
        "sources": source_results,
        "total_id_difference_count": total_id_difference_count,
        "field_mismatch_counts": dict(aggregate_field_mismatches),
    }


def compare_site_to_master(
    site_lots: list[Lot], master_lots: list[Lot]
) -> dict[str, Any]:
    """Compare site inventory composition with the source XML for context only.

    The site does not expose the source UUID, so rows are cross-walked by the
    source/base price, area, room type, and floor.  Site attributes are not
    validated here: the client XML is authoritative, and site-specific section
    or storey representations must not create feed errors.
    """

    def key(lot: Lot) -> tuple[Any, ...]:
        price = lot.base_price or lot.price if lot.source == "site" else lot.price
        return (
            price,
            lot.area.quantize(decimal.Decimal("0.01"))
            if lot.area is not None
            else None,
            lot.rooms,
            lot.floor,
        )

    site_by_key: dict[tuple[Any, ...], list[Lot]] = collections.defaultdict(list)
    master_by_key: dict[tuple[Any, ...], list[Lot]] = collections.defaultdict(list)
    for lot in site_lots:
        site_by_key[key(lot)].append(lot)
    for lot in master_lots:
        master_by_key[key(lot)].append(lot)

    site_counter = collections.Counter(
        {match_key: len(rows) for match_key, rows in site_by_key.items()}
    )
    master_counter = collections.Counter(
        {match_key: len(rows) for match_key, rows in master_by_key.items()}
    )
    site_only_counter = site_counter - master_counter
    master_only_counter = master_counter - site_counter
    site_only: list[dict[str, Any]] = []
    master_only: list[dict[str, Any]] = []
    for match_key, count in site_only_counter.items():
        site_only.extend(row.compact() for row in site_by_key[match_key][:count])
    for match_key, count in master_only_counter.items():
        master_only.extend(
            row.compact() for row in master_by_key[match_key][:count]
        )

    unique_matches = 0
    crosswalk: list[dict[str, Any]] = []
    for match_key in site_by_key.keys() & master_by_key.keys():
        site_rows = site_by_key[match_key]
        master_rows = master_by_key[match_key]
        if len(site_rows) != 1 or len(master_rows) != 1:
            continue
        unique_matches += 1
        site_row, master_row = site_rows[0], master_rows[0]
        crosswalk.append(
            {"site_id": site_row.site_id, "external_id": master_row.external_id}
        )

    return {
        "site_count": len(site_lots),
        "master_count": len(master_lots),
        "unique_one_to_one_matches": unique_matches,
        "ambiguous_site_match_keys": sum(
            len(rows) > 1 for rows in site_by_key.values()
        ),
        "ambiguous_master_match_keys": sum(
            len(rows) > 1 for rows in master_by_key.values()
        ),
        "matched_field_mismatch_counts": {},
        "matched_field_mismatch_samples": [],
        "site_only": site_only,
        "master_only": master_only,
        "crosswalk": sorted(crosswalk, key=lambda row: row["site_id"] or 0),
    }


def _platform_status_path(cache_dir: Path, source: str) -> Path:
    return cache_dir / f"masters-platform-{source}.status.json"


def _source_status_path(cache_dir: Path) -> Path:
    return cache_dir / SOURCE_STATUS_FILENAME


def _read_source_statuses(cache_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(_source_status_path(cache_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(source): status
        for source, status in payload.items()
        if isinstance(status, dict)
    }


def _platform_html_path(cache_dir: Path, source: str) -> Path:
    return cache_dir / f"masters-platform-{source}.html"


def _page_title(document: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", document, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = html_module.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
    return re.sub(r"\s+", " ", title).strip() or None


def _page_contains_project(document: str) -> bool:
    lowered = document.lower()
    escaped_name = "".join(f"\\u{ord(char):04x}" for char in "МАСТЕРС").lower()
    return "мастерс" in lowered or escaped_name in lowered or "masters" in lowered


def parse_platform_page(
    source: str,
    document: str,
    download_status: dict[str, Any],
    feed_count: int,
) -> dict[str, Any]:
    config = PLATFORM_PAGES[source]
    final_url = str(download_status.get("final_url") or config["url"])
    result: dict[str, Any] = {
        "label": config["label"],
        "url": config["url"],
        "final_url": final_url,
        "feed": config["feed"],
        "feed_count": feed_count,
        "card_found": False,
        "offer_count": None,
        "status": "unavailable",
        "reason": download_status.get("error"),
        "title": _page_title(document),
        "checked_at": download_status.get("checked_at"),
    }
    if not download_status.get("downloaded"):
        return result
    if "captcha" in final_url.lower() or "showcaptcha" in document.lower():
        result["status"] = "blocked"
        result["reason"] = "площадка запросила CAPTCHA"
        return result

    result["card_found"] = _page_contains_project(document)
    if not result["card_found"]:
        result["status"] = "missing"
        result["reason"] = "название МАСТЕРС не найдено на странице"
        return result

    from publication_control import extract_count
    result["offer_count"] = extract_count("yandex" if source == "yandex_realty" else source, document)
    searchable = html_module.unescape(document).replace("&nbsp;", " ")
    for pattern in config.get("count_patterns", ()):
        if source not in {"cian", "avito"}:
            break
        match = re.search(pattern, searchable, re.IGNORECASE | re.DOTALL)
        if match:
            result["offer_count"] = int(match.group(1))
            break

    if result["offer_count"] is None:
        result["status"] = "present"
        result["reason"] = "карточка найдена, число предложений публично не извлечено"
    elif result["offer_count"] == feed_count:
        result["status"] = "ok"
        result["reason"] = None
    else:
        result["status"] = "mismatch"
        result["reason"] = "число предложений отличается от XML-фида"
    return result


def _marketplace_api_path(cache_dir: Path, source: str) -> Path:
    return cache_dir / f"masters-platform-{source}.api.json"


def _read_marketplace_api_snapshot(cache_dir: Path, source: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(
            _marketplace_api_path(cache_dir, source).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _apply_marketplace_api_snapshot(
    source: str,
    card: dict[str, Any],
    snapshot: dict[str, Any],
    feed_lots: list[Lot],
) -> dict[str, Any]:
    """Replace a public-page result with stronger authenticated API evidence."""
    result = {
        **card,
        "checked_at": snapshot.get("checked_at"),
        "check_method": snapshot.get("check_method", "api"),
        "api_status": snapshot.get("status"),
    }
    if snapshot.get("status") != "ok":
        result.update(
            {
                "status": "unavailable",
                "offer_count": None,
                "reason": snapshot.get("reason") or "API площадки недоступен",
            }
        )
        return result

    feed_count = len(feed_lots)
    result["feed_count"] = feed_count
    if source == "cian":
        published_count = int(snapshot.get("published_count") or 0)
        import_status_counts = snapshot.get("import_status_counts", {})
        import_error_count = int(snapshot.get("import_error_count") or 0)
        non_published_count = sum(
            int(count)
            for status, count in import_status_counts.items()
            if str(status).casefold() != "published"
        )
        result.update(
            {
                "offer_count": published_count,
                "match_quality": "published_count",
                "import_status_counts": import_status_counts,
                "import_error_count": import_error_count,
                "import_warning_count": snapshot.get("import_warning_count", 0),
            }
        )
        if published_count != feed_count or import_error_count or non_published_count:
            result["status"] = "mismatch"
            result["reason"] = "количество или статусы публикации отличаются от XML"
        else:
            result["status"] = "ok"
            result["reason"] = None
        return result

    project_count = int(snapshot.get("project_active_count") or 0)
    result.update(
        {
            "offer_count": project_count,
            "account_active_count": int(snapshot.get("account_active_count") or 0),
            "match_quality": "address_count",
            "project_match": snapshot.get(
                "project_match", f"адрес содержит «{AVITO_PROJECT_ADDRESS_MARKER}»"
            ),
        }
    )
    if project_count == feed_count:
        result["status"] = "ok"
        result["reason"] = None
    else:
        result["status"] = "mismatch"
        result["reason"] = "число активных объявлений проекта отличается от XML"
    return result


def _apply_domclick_feed_control(
    card: dict[str, Any], feed_summaries: dict[str, Any]
) -> dict[str, Any]:
    """Use the Domclick XML as the default platform signal requested by the owner."""
    unavailable = [
        source
        for source in OFFER_FEEDS
        if feed_summaries[source].get("status") == SOURCE_UNAVAILABLE
    ]
    if unavailable:
        return {
            **card,
            "status": "source_unavailable",
            "check_method": "feed_assumption",
            "offer_count": None,
            "feed_count": feed_summaries["domclick"].get("lot_count"),
            "unavailable_sources": unavailable,
            "reason": (
                "proxy-проверка невозможна: SOURCE_UNAVAILABLE — "
                + ", ".join(FEED_LABELS[source] for source in unavailable)
            ),
        }
    domclick_count = int(feed_summaries["domclick"]["lot_count"])
    other_counts = {
        source: int(feed_summaries[source]["lot_count"])
        for source in OFFER_FEEDS
        if source != "domclick"
    }
    distinct_other_counts = set(other_counts.values())
    result = {
        **card,
        "check_method": "feed_assumption",
        "offer_count": domclick_count,
        "feed_count": domclick_count,
        "other_feed_counts": other_counts,
        "checked_at": feed_summaries["domclick"].get("generation_date"),
    }
    if distinct_other_counts == {domclick_count}:
        result.update(
            {
                "status": "ok",
                "reference_count": domclick_count,
                "reason": None,
            }
        )
    else:
        result.update(
            {
                "status": "mismatch",
                "reference_count": (
                    next(iter(distinct_other_counts))
                    if len(distinct_other_counts) == 1
                    else None
                ),
                "reason": "фид Домклик отличается от остальных товарных XML",
            }
        )
    return result


def analyze_platform_pages(
    cache_dir: Path,
    feed_summaries: dict[str, Any],
    feed_lots: dict[str, list[Lot]],
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for source, config in PLATFORM_PAGES.items():
        feed_source = str(config["feed"])
        feed_summary = feed_summaries[feed_source]
        if feed_summary.get("status") == SOURCE_UNAVAILABLE:
            results[source] = {
                "label": config["label"],
                "url": config["url"],
                "feed": feed_source,
                "feed_count": None,
                "offer_count": None,
                "status": "source_unavailable",
                "reason": "XML-фид имеет статус SOURCE_UNAVAILABLE",
                "checked_at": feed_summary.get("checked_at"),
            }
            continue
        status_path = _platform_status_path(cache_dir, source)
        html_path = _platform_html_path(cache_dir, source)
        try:
            download_status = json.loads(status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            download_status = {
                "downloaded": False,
                "error": "публичная карточка ещё не проверялась",
            }
        document = ""
        if download_status.get("downloaded"):
            try:
                document = html_path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                download_status = {
                    "downloaded": False,
                    "error": "файл публичной карточки отсутствует",
                }
        feed_count = int(feed_summary["lot_count"])
        results[source] = parse_platform_page(
            source, document, download_status, feed_count
        )
        snapshot = _read_marketplace_api_snapshot(cache_dir, source)
        if snapshot is not None and source in {"cian", "avito"}:
            results[source] = _apply_marketplace_api_snapshot(
                source, results[source], snapshot, feed_lots[config["feed"]]
            )
    return results


def analyze_directory(cache_dir: Path) -> dict[str, Any]:
    source_statuses = _read_source_statuses(cache_dir)
    master_lots, master_meta = parse_master(cache_dir / "masters-master.xml")
    feed_lots: dict[str, list[Lot]] = {}
    feed_summaries: dict[str, Any] = {}
    for source in FEED_URLS:
        download_status = source_statuses.get(source, {})
        if download_status.get("status") == SOURCE_UNAVAILABLE:
            feed_summaries[source] = {
                **download_status,
                "lot_count": None,
                "unique_id_count": None,
                "duplicate_ids": [],
                "missing_id_count": None,
                "stale_content": {},
            }
            continue
        path = cache_dir / f"masters-{source}.xml"
        lots, meta = parse_feed(source, path)
        feed_lots[source] = lots
        feed_summaries[source] = {
            **_feed_summary(lots, meta),
            "status": "OK",
            "download": download_status,
        }

    site_download = source_statuses.get("site", {})
    site_lots=[]
    site_meta={}
    if site_download.get('status')!=SOURCE_UNAVAILABLE:
        try:
            site_lots,site_meta=parse_site(cache_dir/'masters-site.json')
        except (OSError,ValueError):
            site_download={**site_download,'status':SOURCE_UNAVAILABLE,
                           'reason':'Сайт вернул невалидный или неполный каталог; проверка сайта недоступна'}
            source_statuses['site']=site_download
    if site_download.get("status") == SOURCE_UNAVAILABLE:
        site_lots: list[Lot] = []
        site_meta: dict[str, Any] = {
            **site_download,
            "lot_count": None,
        }
        site_comparison: dict[str, Any] = {
            "status": SOURCE_UNAVAILABLE,
            "site_count": None,
            "master_count": len(master_lots),
            "unique_one_to_one_matches": 0,
            "ambiguous_site_match_keys": 0,
            "ambiguous_master_match_keys": 0,
            "matched_field_mismatch_counts": {},
            "matched_field_mismatch_samples": [],
            "site_only": [],
            "master_only": [],
            "crosswalk": [],
        }
    else:
        site_comparison = compare_site_to_master(site_lots, master_lots)
        site_comparison["status"] = "OK"
    report = {
        "checked_at": dt.datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
        "source_status": source_statuses,
        "master": _feed_summary(master_lots, master_meta),
        "site": ({**site_meta,'lot_count':None} if site_download.get('status')==SOURCE_UNAVAILABLE else _feed_summary(site_lots, site_meta)),
        "feeds": feed_summaries,
        "master_vs_feeds": compare_master_to_feeds(master_lots, feed_lots),
        "cross_feed": compare_offer_feeds(feed_lots),
        "site_vs_master": site_comparison,
        "platform_cards": analyze_platform_pages(
            cache_dir, feed_summaries, feed_lots
        ),
    }
    return report


def _download(
    url: str,
    destination: Path,
    *,
    attempts: int = DOWNLOAD_ATTEMPTS,
    retry_delay: float = DOWNLOAD_RETRY_DELAY_SECONDS,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Download one source with bounded retries and no GitHub API calls."""
    last_error: BaseException | None = None
    maximum_attempts = max(1, attempts)
    for attempt in range(1, maximum_attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "MastersFeedMonitor/0.4 "
                    "(+https://github.com/avilov0600-lab/masters-feed-monitor)"
                )
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(response.read())
                return {
                    "status": "OK",
                    "downloaded": True,
                    "attempts": attempt,
                    "http_status": response.status,
                    "final_url": response.geturl(),
                    "content_type": response.headers.get("Content-Type"),
                    "checked_at": dt.datetime.now(MOSCOW_TZ).isoformat(
                        timespec="seconds"
                    ),
                }
        except urllib.error.HTTPError as error:
            last_error = error
            retryable = error.code == 429 or error.code >= 500
            if not retryable or attempt >= maximum_attempts:
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
        if retry_delay > 0:
            time.sleep(retry_delay)

    assert last_error is not None
    try:
        setattr(last_error, "masters_attempts", attempt)
    except (AttributeError, TypeError):
        pass
    raise last_error


def _download_failure(error: BaseException, attempts: int) -> dict[str, Any]:
    actual_attempts = int(getattr(error, "masters_attempts", attempts))
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
        "checked_at": dt.datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
    }


def _request_json(
    url: str,
    *,
    token: str | None = None,
    data: dict[str, str] | None = None,
    attempts: int = 3,
) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8") if data else None
    headers = {"Accept": "application/json", "User-Agent": "MastersFeedMonitor/0.3"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    payload: Any = None
    for attempt in range(1, max(1, attempts) + 1):
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if attempt >= attempts or (error.code < 500 and error.code != 429):
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


def _safe_api_failure(error: BaseException) -> str:
    """Return a sanitized reason that can never contain credentials or response bodies."""
    if isinstance(error, urllib.error.HTTPError):
        return f"API вернул HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return f"сетевая ошибка API ({type(error.reason).__name__})"
    return f"ошибка API ({type(error).__name__})"


def _issue_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return int(bool(value))


def _download_cian_api_snapshot() -> dict[str, Any]:
    checked_at = dt.datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    token = os.environ.get("CIAN_API_KEY")
    if not token:
        return {
            "status": "not_configured",
            "check_method": "api_published_count",
            "checked_at": checked_at,
            "reason": "ключ CIAN_API_KEY не настроен",
        }
    try:
        query_values = {
            "source": "upload",
            "statuses": "published",
            "page": 1,
            "pageSize": 100,
        }
        first = _request_json(
            f"{CIAN_API_BASE}/v2/get-my-offers?"
            + urllib.parse.urlencode(query_values),
            token=token,
        )
        first_result = first.get("result") or {}
        announcements = list(first_result.get("announcements") or [])
        api_total = int(first_result.get("totalCount") or len(announcements))
        snapshot: dict[str, Any] = {
            "status": "ok",
            "check_method": "api_published_count",
            "checked_at": checked_at,
            "published_count": api_total,
        }
        try:
            order = _request_json(f"{CIAN_API_BASE}/v1/get-order", token=token)
            order_offers = list((order.get("result") or {}).get("offers") or [])
            statuses = collections.Counter(
                str(row.get("status") or "unknown") for row in order_offers
            )
            snapshot.update(
                {
                    "import_offer_count": len(order_offers),
                    "import_status_counts": dict(statuses),
                    "import_error_count": sum(
                        _issue_count(row.get("errors")) for row in order_offers
                    ),
                    "import_warning_count": sum(
                        _issue_count(row.get("warnings")) for row in order_offers
                    ),
                }
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            ValueError,
            http.client.HTTPException,
        ) as error:
            snapshot["import_report_reason"] = _safe_api_failure(error)
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
            "reason": _safe_api_failure(error),
        }


def _download_avito_api_snapshot() -> dict[str, Any]:
    checked_at = dt.datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    client_id = os.environ.get("AVITO_CLIENT_ID")
    client_secret = os.environ.get("AVITO_CLIENT_SECRET")
    if not client_id or not client_secret:
        return {
            "status": "not_configured",
            "check_method": "api_address_count",
            "checked_at": checked_at,
            "reason": "ключи AVITO_CLIENT_ID/AVITO_CLIENT_SECRET не настроены",
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
        access_token = str(auth["access_token"])
        items_by_id: dict[str, dict[str, Any]] = {}
        for page in range(1, 101):
            payload = _request_json(
                f"{AVITO_API_BASE}/core/v1/items?"
                + urllib.parse.urlencode(
                    {"status": "active", "page": page, "per_page": 100}
                ),
                token=access_token,
            )
            page_items = list(payload.get("resources") or payload.get("items") or [])
            new_items = 0
            for row in page_items:
                item_id = str(row.get("id") or "")
                if item_id and item_id not in items_by_id:
                    items_by_id[item_id] = row
                    new_items += 1
            if len(page_items) < 100 or new_items == 0:
                break

        items = list(items_by_id.values())
        project_items = [
            row
            for row in items
            if AVITO_PROJECT_ADDRESS_MARKER
            in str(row.get("address") or "").casefold()
        ]
        return {
            "status": "ok",
            "check_method": "api_address_count",
            "checked_at": checked_at,
            "account_active_count": len(items),
            "project_active_count": len(project_items),
            "project_match": "адрес содержит «Викторенко»",
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
            "check_method": "api_address_count",
            "checked_at": checked_at,
            "reason": _safe_api_failure(error),
        }


def download_marketplace_api_snapshots(cache_dir: Path) -> None:
    snapshots = {
        "cian": _download_cian_api_snapshot(),
        "avito": _download_avito_api_snapshot(),
    }
    for source, snapshot in snapshots.items():
        _marketplace_api_path(cache_dir, source).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def download_platform_pages(cache_dir: Path) -> None:
    for source, config in PLATFORM_PAGES.items():
        status: dict[str, Any]
        if source in {"cian", "avito"} or not config.get("automated", True):
            status = {
                "downloaded": False,
                "error": "автоматическая публичная проверка отключена",
            }
        else:
            try:
                status = _download(
                    str(config["url"]),
                    _platform_html_path(cache_dir, source),
                    attempts=1,
                )
            except urllib.error.HTTPError as error:
                status = {
                    "downloaded": False,
                    "error": f"площадка вернула HTTP {error.code}",
                    "http_status": error.code,
                }
            except (
                urllib.error.URLError,
                OSError,
                http.client.HTTPException,
            ) as error:
                status = {
                    "downloaded": False,
                    "error": f"сетевая ошибка ({type(error).__name__})",
                }
        status["checked_at"] = dt.datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
        _platform_status_path(cache_dir, source).write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def download_sources(cache_dir: Path) -> dict[str, dict[str, Any]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    independent_sources = {
        "master": (MASTER_FEED_URL, cache_dir / "masters-master.xml"),
        "site": (SITE_API_URL, cache_dir / "masters-site.json"),
    }
    statuses: dict[str, dict[str, Any]] = {}

    def capture(source: str, url: str, destination: Path) -> dict[str, Any]:
        try:
            return _download(url, destination)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as error:
            destination.unlink(missing_ok=True)
            return _download_failure(error, DOWNLOAD_ATTEMPTS)

    # The master XML and the site are independent hosts and can be fetched in
    # parallel. The five outbound XMLs share one 6feeds host, so they are kept
    # sequential to avoid connection bursts and false SSL failures.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(independent_sources), thread_name_prefix="masters-source"
    ) as executor:
        futures = {
            executor.submit(capture, source, url, destination): source
            for source, (url, destination) in independent_sources.items()
        }
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            statuses[source] = future.result()

    for source, url in FEED_URLS.items():
        statuses[source] = capture(
            source, url, cache_dir / f"masters-{source}.xml"
        )

    status_path = cache_dir / SOURCE_STATUS_FILENAME
    status_path.write_text(
        json.dumps(statuses, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if statuses["master"]["status"] == SOURCE_UNAVAILABLE:
        raise RuntimeError(
            "Материнский XML недоступен после трех попыток; сравнение невозможно"
        )

    download_marketplace_api_snapshots(cache_dir)
    download_platform_pages(cache_dir)
    return statuses


def _format_money(value: int | None) -> str:
    return "—" if value is None else f"{value:,}".replace(",", " ") + " ₽"


def _format_platform_line(card: dict[str, Any]) -> str:
    label = card["label"]
    status = card["status"]
    offer_count = card.get("offer_count")
    feed_count = card.get("feed_count")
    check_method = card.get("check_method")
    if status == "source_unavailable":
        return f"🔴 {label}: {card.get('reason') or SOURCE_UNAVAILABLE}"
    if check_method == "feed_assumption":
        if status == "ok":
            return (
                f"✅ {label}: XML {offer_count} совпадает с остальными фидами — "
                "площадка ОК"
            )
        reference_count = card.get("reference_count")
        if reference_count is not None:
            return (
                f"🔴 {label}: XML {offer_count}, остальные XML {reference_count} — "
                "площадку требуется проверить"
            )
        other_counts = card.get("other_feed_counts") or {}
        details = ", ".join(
            f"{source} {count}" for source, count in other_counts.items()
        )
        return (
            f"🔴 {label}: XML-фиды расходятся ({details}) — "
            "площадку требуется проверить"
        )
    if check_method == "api_published_count":
        import_counts = card.get("import_status_counts") or {}
        import_summary = ", ".join(
            f"{name} {count}" for name, count in sorted(import_counts.items())
        )
        import_suffix = f"; импорт: {import_summary}" if import_summary else ""
        import_errors = int(card.get("import_error_count") or 0)
        import_warnings = int(card.get("import_warning_count") or 0)
        if import_errors:
            import_suffix += f"; ошибок импорта {import_errors}"
        if import_warnings:
            import_suffix += f"; предупреждений {import_warnings}"
        if status == "ok":
            return (
                f"✅ {label} API: опубликовано {offer_count} = XML {feed_count} "
                f"(по количеству){import_suffix}"
            )
        if status == "mismatch":
            delta = int(offer_count) - int(feed_count)
            return (
                f"🔴 {label} API: опубликовано {offer_count}, XML {feed_count}; "
                f"разница {delta:+d}{import_suffix}"
            )
        return f"⚪ {label} API: проверка сейчас недоступна"
    if check_method == "api_address_count":
        scope = "по адресу Викторенко"
        if status == "ok":
            return f"✅ {label} API: активно {offer_count} = XML {feed_count} ({scope})"
        if status == "mismatch":
            delta = int(offer_count) - int(feed_count)
            return (
                f"🔴 {label} API: активно {offer_count}, XML {feed_count} "
                f"({delta:+d}; {scope})"
            )
        return f"⚪ {label} API: проверка сейчас недоступна"
    if status == "ok":
        return f"✅ {label}: {offer_count} предложений = XML {feed_count}"
    if status == "present":
        return f"⚪ {label}: публикация не подтверждена — счётчик предложений не найден"
    if status == "mismatch":
        delta = int(offer_count) - int(feed_count)
        return (
            f"🔴 {label}: на площадке {offer_count}, в XML {feed_count} "
            f"({delta:+d})"
        )
    if status == "missing":
        return f"🔴 {label}: карточка ЖК не найдена"
    if status == "blocked":
        return f"⚪ {label}: ссылка подключена, число не читается автоматически"
    return f"⚪ {label}: ссылка подключена, автопроверка недоступна"


def format_telegram(report: dict[str, Any]) -> str:
    master_comparison = report["master_vs_feeds"]
    site_comparison = report["site_vs_master"]
    feeds = report["feeds"]
    cross_feed = report["cross_feed"]
    platform_cards = report.get("platform_cards", {})
    stale_by_source = {
        source: feeds[source].get("stale_content", {}).get("july_discount", 0)
        for source in OFFER_FEEDS
    }
    platform_problem = any(
        card.get("status") != "ok"
        for card in platform_cards.values()
    )
    unavailable_sources = [
        source
        for source in OFFER_FEEDS
        if feeds[source].get("status") == SOURCE_UNAVAILABLE
    ]
    available_sources = [
        source for source in OFFER_FEEDS if source not in unavailable_sources
    ]
    master_source_results = master_comparison["sources"]
    primary_problem = bool(
        unavailable_sources
        or master_comparison["total_id_difference_count"]
        or master_comparison["field_mismatch_counts"]
        or cross_feed["field_mismatch_counts"]
        or any(stale_by_source.values())
        or platform_problem
    )
    site_difference = bool(
        site_comparison["site_only"]
        or site_comparison["master_only"]
    )
    icon = "🔴" if primary_problem else "🟢"
    feed_counts = [feeds[source]["lot_count"] for source in available_sources]
    same_feed_count = bool(feed_counts) and len(set(feed_counts)) == 1
    total_master_properties = report["master"].get("total_property_count")
    lines = [
        f"{icon} МАСТЕРС — контроль выгрузок и площадок",
        "",
        "Основной источник:",
        (
            "Материнский XML Capital Group (эталон): "
            f"квартир к выгрузке — {master_comparison['master_count']}"
            + (
                f" ({total_master_properties} записей всего)"
                if total_master_properties is not None
                else ""
            )
        ),
    ]
    if unavailable_sources:
        lines.append(
            f"🔴 Наши XML: доступны {len(available_sources)} из {len(OFFER_FEEDS)}; "
            "SOURCE_UNAVAILABLE — "
            + ", ".join(FEED_LABELS[source] for source in unavailable_sources)
        )
        for source in unavailable_sources:
            reason = feeds[source].get("reason") or "источник недоступен"
            attempts = feeds[source].get("attempts")
            suffix = f" после {attempts} попыток" if attempts else ""
            lines.append(f"🔴 {FEED_LABELS[source]}: {reason}{suffix}")
    elif same_feed_count:
        lines.append(
            f"Наши XML: {len(OFFER_FEEDS)} фидов по {feed_counts[0]} квартир"
        )
    else:
        lines.append(
            "🔴 Наши XML расходятся по количеству: "
            + ", ".join(
                f"{FEED_LABELS[source]} {feeds[source]['lot_count']}"
                for source in OFFER_FEEDS
            )
        )

    if not unavailable_sources and not (
        master_comparison["total_id_difference_count"]
        or non_height_counts(master_comparison)
    ):
        lines.append(
            f"✅ Все {len(OFFER_FEEDS)} XML совпадают с эталоном по составу и "
            "параметрам квартир; этажность здания — отдельно"
        )
    else:
        for source in OFFER_FEEDS:
            result = master_source_results[source]
            if result.get("status") == SOURCE_UNAVAILABLE:
                continue
            missing = len(result["missing_from_feed"])
            extra = len(result["extra_in_feed"])
            field_mismatches = sum(non_height_counts(result).values())
            if missing or extra or field_mismatches:
                lines.append(
                    f"🔴 {FEED_LABELS[source]} ↔ эталон: "
                    f"не выгружено {missing}, лишних {extra}, "
                    f"ошибок полей {field_mismatches}"
                )

    lines.extend(height_lines(master_source_results, FEED_LABELS))
    lines.extend(["", "Сайт (справочно, не эталон):"])
    if site_comparison.get("status") == SOURCE_UNAVAILABLE:
        lines.append("⚪ SOURCE_UNAVAILABLE — сайт сейчас не проверен")
    else:
        lines.append(
            f"Квартир на сайте Capital Group: {site_comparison['site_count']}"
        )
    if site_comparison.get("status") == SOURCE_UNAVAILABLE:
        pass
    elif not site_difference:
        lines.append("ℹ️ Состав сайта совпадает с материнским XML")
    else:
        lines.append(
            "ℹ️ Сайт ↔ материнский XML (не влияет на статус фидов): "
            f"на сайте сверх эталона {len(site_comparison['site_only'])}; "
            f"в эталоне без карточки сайта {len(site_comparison['master_only'])}"
        )
        if site_comparison["site_only"]:
            lot = site_comparison["site_only"][0]
            lines.append(
                "Пример дополнительной квартиры на сайте: "
                f"site_id={lot.get('site_id')}, {lot.get('area')} м², "
                f"базовая цена {_format_money(lot.get('base_price') or lot.get('price'))}"
            )

    if platform_cards:
        lines.extend(["", "Площадки (API, карточки и контроль по фиду):"])
        lines.extend(
            _format_platform_line(platform_cards[source])
            for source in PLATFORM_PAGES
            if source in platform_cards
        )

    detail_issues: list[str] = []
    if non_height_counts(master_comparison):
        detail_issues.append(
            "🔴 Наши XML ↔ материнский XML, ошибки полей: "
            + json.dumps(
                non_height_counts(master_comparison), ensure_ascii=False
            )
        )
    if cross_feed["field_mismatch_counts"]:
        detail_issues.append(
            "🔴 Наши XML между собой, ошибки полей: "
            + json.dumps(
                cross_feed["field_mismatch_counts"], ensure_ascii=False
            )
        )
    if any(stale_by_source.values()):
        detail_issues.append(
            "🔴 Устаревшая фраза «скидка 10% в июле» в XML: "
            + ", ".join(f"{source} {count}" for source, count in stale_by_source.items())
        )
    if detail_issues:
        lines.extend(["", "Проверки полей и рекламных текстов:", *detail_issues])
    lines.extend(["", f"Проверено: {report['checked_at']}"])
    return "\n".join(lines)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def send_telegram(message: str, *, chat_id: str | None = None) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    target_chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not target_chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and a target chat ID")
    relay_url = os.environ.get("TELEGRAM_RELAY_URL")
    relay_key = os.environ.get("TELEGRAM_RELAY_KEY")
    if relay_url and relay_key:
        relay_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "MASTERS-feed-monitor/1.0",
            "X-Masters-Relay-Key": relay_key,
        }
        relay_auth_token = os.environ.get("TELEGRAM_RELAY_AUTH_TOKEN")
        if relay_auth_token:
            relay_headers["OAI-Sites-Authorization"] = (
                f"Bearer {relay_auth_token}"
            )
        payload = json.dumps(
            {
                "bot_token": token,
                "chat_id": str(target_chat_id),
                "text": message,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            relay_url,
            data=payload,
            headers=relay_headers,
            method="POST",
        )
    else:
        payload = urllib.parse.urlencode(
            {
                "chat_id": target_chat_id,
                "text": message,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            method="POST",
        )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("Telegram sendMessage returned an invalid response")
    telegram_result = result.get("result")
    return telegram_result if isinstance(telegram_result, dict) else {}


def save_latest_telegram_message(
    state_path: Path, message: str, checked_at: str | None = None
) -> None:
    """Persist only the last message text so /start can replay it unchanged."""
    state = _read_json_object(state_path)
    state.update(
        {
            "latest_message": message,
            "latest_checked_at": checked_at,
            "saved_at": dt.datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
        }
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def sync_latest_telegram_message(
    message: str, checked_at: str | None = None
) -> dict[str, Any]:
    """Copy the last sent digest to the webhook's private persistent storage."""
    endpoint = os.environ.get("TELEGRAM_LATEST_MESSAGE_URL")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not endpoint:
        return {}
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN to sync the latest message")
    sync_payload = {
        "action": "sync_latest",
        "message": message,
        "checked_at": checked_at,
    }
    owner = os.environ.get("TELEGRAM_CHAT_ID")
    if owner:
        sync_payload["owner_chat_id"] = owner
    body = json.dumps(sync_payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "X-Masters-Bot-Token": token,
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError("Latest Telegram message sync failed")
    return payload


def broadcast_telegram_message(message: str, checked_at: str | None = None) -> dict[str, int]:
    """Sync the digest, then deliver it to every active private subscriber."""
    endpoint = os.environ.get("TELEGRAM_LATEST_MESSAGE_URL")
    owner = os.environ.get("TELEGRAM_CHAT_ID")
    recipients: list[str] = []
    if endpoint:
        try:
            sync = sync_latest_telegram_message(message, checked_at)
            raw_recipients = sync.get("recipients")
            if not isinstance(raw_recipients, list):
                raise RuntimeError("Subscriber registry returned an invalid response")
            recipients = [str(value) for value in raw_recipients]
        except (OSError, RuntimeError, ValueError) as error:
            # The registry must never become a single point of failure.  The
            # configured owner still receives the status directly.
            print(f"Telegram registry unavailable; owner fallback: {type(error).__name__}")
            recipients = [owner] if owner else []
    elif owner:
        recipients = [owner]
    recipients = list(dict.fromkeys(value for value in recipients if value))
    if not recipients and owner:
        recipients = [owner]
    if not recipients:
        raise RuntimeError("No active Telegram subscribers")
    failed = 0
    for position, chat_id in enumerate(recipients):
        try:
            send_telegram(message, chat_id=chat_id)
        except (OSError, RuntimeError, ValueError):
            failed += 1
        if position + 1 < len(recipients):
            # Telegram documents a free broadcast ceiling of about 30 messages/s.
            time.sleep(0.05)
    delivered = len(recipients) - failed
    print(f"Telegram broadcast: subscribers={len(recipients)} delivered={delivered} failed={failed}")
    if failed:
        raise RuntimeError(f"Telegram broadcast failed for {failed} subscriber(s)")
    return {"subscribers": len(recipients), "delivered": delivered, "failed": failed}


def telegram_start_reply(
    update: dict[str, Any], state_path: Path
) -> bool:
    """Send the exact cached digest for a valid private Telegram /start update."""
    reply = telegram_start_payload(update, state_path)
    if reply is None:
        return False
    send_telegram(reply["text"], chat_id=reply["chat_id"])
    return True


def telegram_start_payload(
    update: dict[str, Any], state_path: Path
) -> dict[str, str] | None:
    """Build the cached /start reply without making an outbound API request."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = str(message.get("text") or "")
    chat = message.get("chat")
    if not (
        re.match(r"^/start(?:@\w+)?(?:\s|$)", text, re.IGNORECASE)
        and isinstance(chat, dict)
        and chat.get("type") == "private"
        and chat.get("id") is not None
    ):
        return None
    state = _read_json_object(state_path)
    latest_message = state.get("latest_message")
    if not isinstance(latest_message, str) or not latest_message.strip():
        return None
    return {"chat_id": str(chat["id"]), "text": latest_message}


def problem_payload(report: dict[str, Any]) -> dict[str, Any]:
    master_comparison = report["master_vs_feeds"]
    return {
        "source_unavailable": sorted(
            source
            for source, summary in report["feeds"].items()
            if summary.get("status") == SOURCE_UNAVAILABLE
        ),
        "master_to_feeds": {
            source: {
                "status": result.get("status", "OK"),
                "missing_from_feed": result["missing_from_feed"],
                "extra_in_feed": result["extra_in_feed"],
                "field_mismatch_counts": result["field_mismatch_counts"],
            }
            for source, result in master_comparison["sources"].items()
        },
        "feed_field_mismatches": report["cross_feed"]["field_mismatch_counts"],
        "feed_source_outliers": report["cross_feed"]["source_outlier_counts"],
        "stale_content": {
            source: summary.get("stale_content", {})
            for source, summary in report["feeds"].items()
            if source in OFFER_FEEDS
        },
        "platform_cards": {
            source: {
                "status": card.get("status"),
                "offer_count": card.get("offer_count"),
                "feed_count": card.get("feed_count"),
                "import_status_counts": card.get("import_status_counts"),
                "import_error_count": card.get("import_error_count"),
            }
            for source, card in report.get("platform_cards", {}).items()
            if card.get("status") in {"mismatch", "missing"}
        },
    }


def _payload_has_problem(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("source_unavailable")
        or any(
            result["missing_from_feed"]
            or result["extra_in_feed"]
            or result["field_mismatch_counts"]
            for result in payload["master_to_feeds"].values()
        )
        or payload["feed_field_mismatches"]
        or payload["platform_cards"]
        or any(
            any(count for count in issues.values())
            for issues in payload["stale_content"].values()
        )
    )


def should_notify(report: dict[str, Any], state_path: Path, threshold: int) -> bool:
    """Suppress transient and repeated alerts using a small persistent state file."""
    payload = problem_payload(report)
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    has_problem = _payload_has_problem(payload)
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}

    if has_problem and previous.get("last_fingerprint") == fingerprint:
        consecutive = int(previous.get("consecutive", 0)) + 1
    elif has_problem:
        consecutive = 1
    else:
        consecutive = 0

    notify = False
    active_alert = bool(previous.get("active_alert"))
    if has_problem and consecutive >= max(1, threshold):
        notify = previous.get("last_notified_fingerprint") != fingerprint
        if notify:
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
        "updated_at": report["checked_at"],
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return notify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache"),
        help="Directory for downloaded or already downloaded source files",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Analyze existing files in --cache-dir without downloading",
    )
    parser.add_argument("--output", type=Path, help="Write the full JSON report")
    parser.add_argument(
        "--send-telegram", action="store_true", help="Send the summary to Telegram"
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(".state.json"),
        help="Persistent alert state used to suppress transient and repeated messages",
    )
    parser.add_argument(
        "--notify-after",
        type=int,
        default=2,
        help="Notify after this many consecutive identical problem runs",
    )
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="Send Telegram immediately and bypass state suppression",
    )
    parser.add_argument(
        "--sync-telegram-report",
        type=Path,
        help="Sync an existing JSON report to /start storage without sending it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sync_telegram_report:
        report = json.loads(args.sync_telegram_report.read_text(encoding="utf-8"))
        message = format_telegram(report)
        sync_latest_telegram_message(message, report.get("checked_at"))
        print("Telegram /start: последнее сообщение синхронизировано без рассылки")
        return 0
    if not args.offline:
        download_sources(args.cache_dir)
    report = analyze_directory(args.cache_dir)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    message = format_telegram(report)
    print(message)
    if args.send_telegram:
        if args.force_notify or should_notify(report, args.state_file, args.notify_after):
            broadcast_telegram_message(message, report.get("checked_at"))
        else:
            print("Telegram: сообщение подавлено до подтверждения или изменения статуса")
    return 0


from publication_adapter import install as _install_publication_control
_install_publication_control(sys.modules[__name__])

if __name__ == "__main__":
    sys.exit(main())
