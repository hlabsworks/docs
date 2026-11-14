#!/usr/bin/env python3
"""Validate the Intervalian public catalog (index.json + entries/*.json).

main pushes publish this directory live, and the app's CatalogIndex.fromJson
rejects the WHOLE index when a single entry is malformed. This lint mirrors
the app's own parsers so a bad entry is caught before it is pushed:

  lib/domain/entities/catalog_index.dart        top-level index fields
  lib/domain/entities/catalog_entry.dart        per-entry index fields
  lib/data/services/catalog_entry_parser.dart   entry document envelope
  lib/data/services/backup_document_parser.dart menu / segment / routine rules
  lib/services/catalog/http_catalog_gateway.dart entryPath rules, byte limits
  lib/domain/logic/*_duration_calculator.dart   estimatedSeconds formula

Usage (stdlib only, Python 3.9+):
  python3 intervalian/catalog/tools/lint_catalog.py [--root intervalian/catalog/v1]

Exit 0 = no errors (warnings allowed), 1 = at least one error.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = TOOLS_DIR.parent / "v1"
DEFAULT_POSE_IDS = TOOLS_DIR / "known-pose-ids.txt"

CATALOG_VERSION = 1

# AppDatabase.schemaVersion of the latest RELEASED app (lib/data/database/app_database.dart
# on origin/main in the app repo). An entry whose minSchemaVersion is higher is
# unreachable for every installed app, so it only gets a WARN. Bump on app release.
RELEASED_APP_SCHEMA_VERSION = 16

# HttpCatalogGateway.maxIndexBytes / maxEntryBytes. The app rejects a body
# strictly larger than the limit; the WARN threshold leaves headroom.
MAX_INDEX_BYTES = 512 * 1024
MAX_ENTRY_BYTES = 256 * 1024
WARN_RATIO = 0.75

ENTRY_TYPES = ("menu", "routine")

SEGMENT_WORK = 1
SEGMENT_REST = 2
# SegmentType.fromValue accepts 0..3, but prepare/cooldown are menu-level
# fields (SegmentType.segmentChoices) and no catalog entry uses them.
APP_SEGMENT_TYPES = (0, 1, 2, 3)
CATALOG_SEGMENT_TYPES = (SEGMENT_WORK, SEGMENT_REST)

# BackupDocumentParser.illustrationIdOrNull rejects longer ids.
MAX_ILLUSTRATION_ID_LENGTH = 40

# The running view clips a work-segment description to about two lines
# (~44 full-width characters); stay under that.
JA_WORK_DESCRIPTION_MAX = 40

# Tags used by the entries published so far; a new tag is only a WARN.
TAG_VOCABULARY = frozenset({"cardio", "hiit", "strength", "mobility", "cycling"})

# BackupDocumentParser.parseMenu defaults for omitted keys.
MENU_INT_DEFAULTS = {
    "sets": 3,
    "prepareSeconds": 10,
    "prepareCountdownSeconds": 5,
    "restBetweenSetsSeconds": 30,
    "workCountdownSeconds": 10,
    "restCountdownSeconds": 5,
    "cooldownSeconds": 0,
    "cooldownCountdownSeconds": 5,
}
MENU_SETTING_KEYS = tuple(MENU_INT_DEFAULTS) + ("restBetweenSetsCountdownSeconds",)

# Dart's DateTime.parse grammar (dart:core, DateTime._parseFormat).
_DART_DATETIME = re.compile(
    r"^([+-]?\d{4,6})-?(\d\d)-?(\d\d)"
    r"(?:[ T](\d\d)(?::?(\d\d)(?::?(\d\d)(?:[.,](\d+))?)?)?"
    r"( ?[zZ]| ?([-+])(\d\d)(?::?(\d\d))?)?)?",
    re.ASCII,  # Dart accepts ASCII digits only; Python's \d also matches e.g. full-width ones
)
# Uri.parse(...).hasScheme: letters first, then scheme chars, then ':'.
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_ENTRY_PATH_UNSAFE = re.compile(r"[^A-Za-z0-9._\-/]")
_TITLE_MINUTES = re.compile(r"(\d+)\s*(?:分|min)")


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.errors = 0
        self.warnings = 0

    def error(self, file: str, rule: str, message: str) -> None:
        self.errors += 1
        self.lines.append(f"ERROR {file}: [{rule}] {message}")

    def warn(self, file: str, rule: str, message: str) -> None:
        self.warnings += 1
        self.lines.append(f"WARN {file}: [{rule}] {message}")


def is_int(value) -> bool:
    # Dart's jsonDecode yields a double for integers outside the signed 64-bit
    # range, which `is int` then rejects.
    return isinstance(value, int) and not isinstance(value, bool) and -(2**63) <= value < 2**63


def is_nonempty_str(value) -> bool:
    return isinstance(value, str) and value != ""


def is_dart_datetime(value: str) -> bool:
    match = _DART_DATETIME.fullmatch(value)
    if not match:
        return False
    year, month, day = int(match[1]), int(match[2]), int(match[3])
    if not 1 <= month <= 12:
        return False
    if not 1 <= day <= calendar.monthrange(abs(year) or 1, month)[1]:
        return False
    for group, upper in ((4, 23), (5, 59), (6, 59)):
        if match[group] is not None and int(match[group]) > upper:
            return False
    return True


def trimmed_or_none(value):
    """Mirrors BackupDocumentParser.descriptionOrNull's trim/blank -> null."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate object key {key!r}")
        seen[key] = value
    return seen


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def load_json(path: Path, label: str, max_bytes: int, report: Report):
    """Reads like the app does: size limit, strict UTF-8, strict JSON."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        report.error(label, "read", f"cannot read file: {exc}")
        return None
    if len(raw) > max_bytes:
        report.error(
            label,
            "size-limit",
            f"{len(raw)} bytes exceeds the app's {max_bytes}-byte limit; "
            "the app refuses to load it",
        )
    elif len(raw) > max_bytes * WARN_RATIO:
        report.warn(
            label,
            "size-limit",
            f"{len(raw)} bytes is over {int(WARN_RATIO * 100)}% of the app's "
            f"{max_bytes}-byte limit",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        report.error(label, "utf8", f"not valid UTF-8: {exc}")
        return None
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError) as exc:
        report.error(label, "json", f"not valid JSON: {exc}")
        return None


def load_known_pose_ids(path: Path, report: Report):
    label = path.name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report.error(label, "pose-ids", f"cannot read known pose ids: {exc}")
        return None
    ids = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not ids:
        report.error(label, "pose-ids", "no pose ids listed")
        return None
    if len(set(ids)) != len(ids):
        report.warn(label, "pose-ids", "contains duplicate ids")
    return set(ids)


# ---------------------------------------------------------------- index.json


def check_entry_path(root: Path, entry_path: str) -> list[str]:
    """Returns problems; mirrors HttpCatalogGateway._resolveEntryUri, then
    requires a case-exact existing file under entries/."""
    if entry_path.strip() == "":
        return ["must not be empty or whitespace-only"]
    if _URI_SCHEME.match(entry_path) or entry_path.startswith("//"):
        return ["must be relative to the catalog base (no scheme or host)"]
    parts = entry_path.split("/")
    if ".." in parts:
        return ['must not contain ".." segments']
    if _ENTRY_PATH_UNSAFE.search(entry_path):
        return [
            "may only contain letters, digits, '.', '_', '-' and '/' "
            "(encoded or special characters can resolve to a different URL)"
        ]
    if entry_path.startswith("/") or "" in parts or "." in parts:
        return ["must not start with '/' or contain empty or '.' segments"]
    if parts[0] != "entries" or len(parts) < 2:
        return ["must live inside entries/"]

    current = root
    for part in parts:
        try:
            names = os.listdir(current)
        except OSError:
            return [f"file does not exist: {entry_path}"]
        if part not in names:
            # GitHub Pages is case-sensitive; a case-insensitive local
            # filesystem would hide a wrong-case path.
            hint = " (differs only by letter case)" if part.lower() in (
                n.lower() for n in names
            ) else ""
            return [f"file does not exist: {entry_path}{hint}"]
        current = current / part
    if not current.is_file():
        return [f"not a regular file: {entry_path}"]
    entries_dir = (root / "entries").resolve()
    try:
        current.resolve().relative_to(entries_dir)
    except ValueError:
        return ["resolves outside entries/"]
    return []


def parse_index_entry(raw, position: int, report: Report):
    """Validates one entries[] element like CatalogEntry.fromJson (plus
    catalog conventions). Returns the normalised entry, or None."""
    label_id = raw.get("id") if isinstance(raw, dict) else None
    where = f"entries[{position}]" + (f" ({label_id})" if is_nonempty_str(label_id) else "")
    if not isinstance(raw, dict):
        report.error("index.json", "index-field", f"{where} is not an object")
        return None

    ok = True

    def fail(rule: str, message: str) -> None:
        nonlocal ok
        ok = False
        report.error("index.json", rule, f"{where}: {message}")

    for key in ("id", "author", "entryPath"):
        if not is_nonempty_str(raw.get(key)):
            fail("index-field", f"{key} is missing or not a non-empty string")
    for key in ("minSchemaVersion", "estimatedSeconds"):
        if not is_int(raw.get(key)):
            fail("index-field", f"{key} is missing or not an int")
    if raw.get("type") not in ENTRY_TYPES:
        fail("index-field", f"type must be one of {ENTRY_TYPES}, got {raw.get('type')!r}")
    published = raw.get("publishedAt")
    if not isinstance(published, str):
        fail("index-field", "publishedAt is missing or not a string")
    elif not is_dart_datetime(published):
        fail("index-field", f"publishedAt is not an ISO-8601 date the app can parse: {published!r}")

    tags = raw.get("tags")
    if not isinstance(tags, list):
        fail("index-field", "tags is missing or not an array")
    elif not all(isinstance(tag, str) for tag in tags):
        fail("index-field", "tags contains a non-string element")

    if raw.get("menuCount") is not None and not is_int(raw.get("menuCount")):
        fail("index-field", "menuCount must be an int or null")
    for key in ("language", "translationOf"):
        value = raw.get(key)
        if value is not None and not is_nonempty_str(value):
            fail("index-field", f"{key} must be a non-empty string when present")
    menu_entry_ids = raw.get("menuEntryIds")
    if menu_entry_ids is not None and (
        not isinstance(menu_entry_ids, list)
        or not all(isinstance(item, str) for item in menu_entry_ids)
    ):
        fail("index-field", "menuEntryIds must be an array of strings when present")

    language = raw.get("language") if is_nonempty_str(raw.get("language")) else "en"
    locale_maps = {}
    for key in ("title", "description"):
        value = raw.get(key)
        if not isinstance(value, dict):
            fail("index-field", f"{key} is missing or not an object")
            continue
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            fail("index-field", f"{key} has a non-string key or value")
            continue
        if "en" not in value:
            fail("index-field", f'{key} is missing its required "en" key')
            continue
        required = ["en"] + (["ja"] if language == "ja" else [])
        for locale in required:
            if not value.get(locale, "").strip():
                fail(
                    "locale-text",
                    f"{key}.{locale} must be non-empty"
                    + (" (language is ja)" if locale == "ja" else ""),
                )
        locale_maps[key] = value

    if not ok:
        return None
    return {
        "id": raw["id"],
        "type": raw["type"],
        "minSchemaVersion": raw["minSchemaVersion"],
        "title": locale_maps["title"],
        "description": locale_maps["description"],
        "tags": tags,
        "estimatedSeconds": raw["estimatedSeconds"],
        "menuCount": raw.get("menuCount"),
        "entryPath": raw["entryPath"],
        "language": language,
        "translationOf": raw.get("translationOf"),
        "menuEntryIds": menu_entry_ids or [],
    }


def lint_index(root: Path, report: Report):
    path = root / "index.json"
    if not path.is_file():
        report.error("index.json", "index-missing", f"not found under {root}")
        return None
    data = load_json(path, "index.json", MAX_INDEX_BYTES, report)
    if data is None:
        return None
    if not isinstance(data, dict):
        report.error("index.json", "index-field", "top-level value is not an object")
        return None

    version = data.get("catalogVersion")
    if not is_int(version):
        report.error("index.json", "index-field", "catalogVersion is missing or not an int")
    elif version != CATALOG_VERSION:
        report.error(
            "index.json",
            "catalog-version",
            f"catalogVersion is {version}, expected {CATALOG_VERSION}",
        )
    updated = data.get("updatedAt")
    if not isinstance(updated, str):
        report.error("index.json", "index-field", "updatedAt is missing or not a string")
    elif not is_dart_datetime(updated):
        report.error(
            "index.json",
            "index-field",
            f"updatedAt is not an ISO-8601 date-time the app can parse: {updated!r}",
        )
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        report.error("index.json", "index-field", "entries is missing or not an array")
        return None

    entries = []
    for position, raw in enumerate(raw_entries):
        parsed = parse_index_entry(raw, position, report)
        if parsed is not None:
            entries.append(parsed)
    return entries, len(raw_entries)


# ------------------------------------------------------------- entry documents


def parse_menu(raw, label: str, report: Report, rule_prefix: str = "menu"):
    """Validates one menu like BackupDocumentParser.parseMenu (plus catalog
    conventions). Returns a normalised menu dict or None."""
    if not isinstance(raw, dict):
        report.error(label, f"{rule_prefix}-field", "menu is not an object")
        return None
    ok = True
    menu_id = raw.get("id")
    where = f"menu {menu_id}" if is_nonempty_str(menu_id) else "menu"

    def fail(rule: str, message: str) -> None:
        nonlocal ok
        ok = False
        report.error(label, rule, f"{where}: {message}")

    for key in ("id", "name"):
        if not is_nonempty_str(raw.get(key)):
            fail("menu-field", f"{key} is missing or not a non-empty string")

    settings = {}
    for key, default in MENU_INT_DEFAULTS.items():
        value = raw.get(key)
        if value is None:
            settings[key] = default
        elif not is_int(value):
            fail("menu-field", f"{key} must be an int")
        elif value < 0 or (key == "sets" and value < 1):
            fail("menu-field", f"{key} must be {'>= 1' if key == 'sets' else 'not negative'}, got {value}")
        else:
            settings[key] = value
    between_countdown = raw.get("restBetweenSetsCountdownSeconds")
    if between_countdown is None:
        settings["restBetweenSetsCountdownSeconds"] = settings.get("restCountdownSeconds")
    elif not is_int(between_countdown) or between_countdown < 0:
        fail("menu-field", "restBetweenSetsCountdownSeconds must be a non-negative int")
    else:
        settings["restBetweenSetsCountdownSeconds"] = between_countdown

    for key in ("createdAt", "updatedAt"):
        value = raw.get(key)
        if not isinstance(value, str):
            fail("menu-field", f"{key} is missing or not a string")
        elif not is_dart_datetime(value):
            fail("menu-field", f"{key} is not an ISO-8601 date-time: {value!r}")
    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        fail("menu-field", "description must be a string or null")

    raw_segments = raw.get("segments")
    segments = []
    if not isinstance(raw_segments, list) or not raw_segments:
        fail("segments", "segments must be a non-empty array")
    else:
        for position, raw_segment in enumerate(raw_segments):
            segment = parse_segment(raw_segment, position, where, label, report)
            if segment is None:
                ok = False
            else:
                segments.append(segment)

    if segments:
        ids = [segment["id"] for segment in segments]
        for duplicate in sorted({i for i in ids if ids.count(i) > 1}):
            fail("segment-id", f"segment id {duplicate!r} is used more than once in this menu")
        orders = [segment["orderIndex"] for segment in segments]
        if len(set(orders)) != len(orders):
            fail("order-index", "segments has duplicate orderIndex values")
        elif sorted(orders) != list(range(len(orders))):
            fail(
                "order-index",
                f"segment orderIndex values must be contiguous from 0, got {sorted(orders)}",
            )
        elif orders != sorted(orders):
            report.warn(label, "order-index", f"{where}: segments are not listed in orderIndex order")

    if not ok:
        return None
    menu = dict(settings)
    menu.update(
        id=raw["id"],
        name=raw["name"],
        description=trimmed_or_none(description),
        segments=sorted(segments, key=lambda segment: segment["orderIndex"]),
    )
    return menu


def parse_segment(raw, position: int, menu_where: str, label: str, report: Report):
    if not isinstance(raw, dict):
        report.error(label, "segment-field", f"{menu_where}: segments[{position}] is not an object")
        return None
    seg_id = raw.get("id")
    where = f"{menu_where}, segment {seg_id}" if is_nonempty_str(seg_id) else f"{menu_where}, segments[{position}]"
    ok = True

    def fail(rule: str, message: str) -> None:
        nonlocal ok
        ok = False
        report.error(label, rule, f"{where}: {message}")

    for key in ("id", "name"):
        if not is_nonempty_str(raw.get(key)):
            fail("segment-field", f"{key} is missing or not a non-empty string")
    order = raw.get("orderIndex")
    if not is_int(order) or order < 0:
        fail("segment-field", "orderIndex is missing or not a non-negative int")
    duration = raw.get("durationSeconds")
    if not is_int(duration):
        fail("segment-field", "durationSeconds is missing or not an int")
    elif duration <= 0:
        fail("duration", f"durationSeconds must be positive, got {duration}")
    seg_type = raw.get("type")
    if not is_int(seg_type) or seg_type not in APP_SEGMENT_TYPES:
        fail("segment-type", f"type must be {CATALOG_SEGMENT_TYPES[0]} (work) or {CATALOG_SEGMENT_TYPES[1]} (rest), got {seg_type!r}")
    elif seg_type not in CATALOG_SEGMENT_TYPES:
        fail("segment-type", f"type {seg_type} is accepted by the app but prepare/cooldown are menu-level; use 1 (work) or 2 (rest)")
    for key in ("sourceTemplateId", "description", "illustrationId"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            fail("segment-field", f"{key} must be a string or null")

    raw_illustration = raw.get("illustrationId")
    illustration = trimmed_or_none(raw_illustration) if isinstance(raw_illustration, str) else None
    if illustration is not None and len(illustration) > MAX_ILLUSTRATION_ID_LENGTH:
        fail("segment-field", f"illustrationId must be at most {MAX_ILLUSTRATION_ID_LENGTH} characters, got {len(illustration)}")

    if not ok:
        return None
    return {
        "id": raw["id"],
        "orderIndex": order,
        "sourceTemplateId": raw.get("sourceTemplateId"),
        "name": raw["name"],
        "durationSeconds": duration,
        "type": seg_type,
        "description": trimmed_or_none(raw.get("description")),
        "illustrationId": illustration,
    }


def menu_total_seconds(menu: dict, sets=None) -> int:
    """MenuDurationCalculator.totalDuration: prepare once + sets copies of the
    segment sum + restBetweenSets between sets only + cooldown once."""
    sets = menu["sets"] if sets is None else sets
    if sets < 1:
        return 0
    segment_sum = sum(segment["durationSeconds"] for segment in menu["segments"])
    return (
        menu["prepareSeconds"]
        + sets * segment_sum
        + (sets - 1) * menu["restBetweenSetsSeconds"]
        + menu["cooldownSeconds"]
    )


def check_segment_content(menu: dict, entry: dict, label: str, known_poses, report: Report) -> None:
    where = f"menu {menu['id']}"
    for segment in menu["segments"]:
        seg_where = f"{where}, segment {segment['id']}"
        illustration = segment["illustrationId"]
        if segment["type"] == SEGMENT_WORK:
            if illustration is None:
                report.error(label, "illustration-id", f"{seg_where}: work segment has no illustrationId")
            elif known_poses is not None and illustration not in known_poses:
                report.error(
                    label,
                    "illustration-id",
                    f"{seg_where}: illustrationId {illustration!r} is not in known-pose-ids.txt",
                )
            description = segment["description"] or ""
            if entry["language"] == "ja" and len(description) > JA_WORK_DESCRIPTION_MAX:
                report.warn(
                    label,
                    "ja-description-length",
                    f"{seg_where}: description is {len(description)} characters "
                    f"(over {JA_WORK_DESCRIPTION_MAX}; the running view clips ~44)",
                )
        elif illustration is not None:
            report.error(label, "illustration-on-rest", f"{seg_where}: rest segment must not carry an illustrationId")

    last = menu["segments"][-1]
    if menu["sets"] == 1 and last["type"] != SEGMENT_WORK:
        report.warn(label, "last-segment", f"{where}: a single-set menu ends on a non-work segment ({last['id']})")


def parse_routine(raw, label: str, report: Report):
    if not isinstance(raw, dict):
        report.error(label, "routine-field", "routine is not an object")
        return None
    ok = True
    where = f"routine {raw.get('id')}" if is_nonempty_str(raw.get("id")) else "routine"

    def fail(rule: str, message: str) -> None:
        nonlocal ok
        ok = False
        report.error(label, rule, f"{where}: {message}")

    for key in ("id", "name"):
        if not is_nonempty_str(raw.get(key)):
            fail("routine-field", f"{key} is missing or not a non-empty string")
    for key in ("createdAt", "updatedAt"):
        value = raw.get(key)
        if not isinstance(value, str):
            fail("routine-field", f"{key} is missing or not a string")
        elif not is_dart_datetime(value):
            fail("routine-field", f"{key} is not an ISO-8601 date-time: {value!r}")
    raw_items = raw.get("items")
    items = []
    if not isinstance(raw_items, list) or not raw_items:
        fail("routine-items", "items must be a non-empty array")
    else:
        for position, item in enumerate(raw_items):
            if not isinstance(item, dict):
                fail("routine-items", f"items[{position}] is not an object")
                continue
            item_ok = True
            for key in ("id", "menuId"):
                if not is_nonempty_str(item.get(key)):
                    fail("routine-items", f"items[{position}].{key} is missing or not a non-empty string")
                    item_ok = False
            order = item.get("orderIndex")
            if not is_int(order) or order < 0:
                fail("routine-items", f"items[{position}].orderIndex is missing or not a non-negative int")
                item_ok = False
            override = item.get("setsOverride")
            if override is not None and (not is_int(override) or override < 1):
                fail("routine-items", f"items[{position}].setsOverride must be an int >= 1 or null")
                item_ok = False
            if item_ok:
                items.append(
                    {"id": item["id"], "orderIndex": order, "menuId": item["menuId"], "setsOverride": override}
                )
    if items:
        orders = [item["orderIndex"] for item in items]
        if len(set(orders)) != len(orders):
            fail("order-index", "items has duplicate orderIndex values")
        elif sorted(orders) != list(range(len(orders))):
            fail("order-index", f"item orderIndex values must be contiguous from 0, got {sorted(orders)}")
        ids = [item["id"] for item in items]
        for duplicate in sorted({i for i in ids if ids.count(i) > 1}):
            fail("routine-items", f"item id {duplicate!r} is used more than once")
    if not ok:
        return None
    return {
        "id": raw["id"],
        "items": sorted(items, key=lambda item: item["orderIndex"]),
    }


def lint_entry_document(root: Path, entry: dict, known_poses, report: Report):
    """Returns {"menus": [...], "routine": {...}|None} or None when unusable."""
    label = entry["entryPath"]
    data = load_json(root / entry["entryPath"], label, MAX_ENTRY_BYTES, report)
    if data is None:
        return None
    if not isinstance(data, dict):
        report.error(label, "entry-document", "top-level value is not an object")
        return None

    schema = data.get("schemaVersion")
    if not is_int(schema):
        report.error(label, "schema-version", "schemaVersion is missing or not an int")
        return None
    if schema > entry["minSchemaVersion"]:
        report.error(
            label,
            "schema-version",
            f"schemaVersion {schema} is above the index minSchemaVersion {entry['minSchemaVersion']}; "
            "a build the index says can fetch this entry would reject it as unsupportedVersion",
        )

    if entry["type"] == "menu":
        if data.get("menu") is None:
            report.error(label, "entry-document", 'menu entry document is missing its "menu" key')
            return None
        menu = parse_menu(data["menu"], label, report)
        if menu is None:
            return None
        check_segment_content(menu, entry, label, known_poses, report)
        if menu_total_seconds(menu) != entry["estimatedSeconds"]:
            report.error(
                label,
                "estimated-seconds",
                f"index estimatedSeconds is {entry['estimatedSeconds']} but the menu totals {menu_total_seconds(menu)} "
                "(prepare + sets x segments + (sets-1) x restBetweenSets + cooldown)",
            )
        return {"menus": [menu], "routine": None}

    if data.get("routine") is None:
        report.error(label, "entry-document", 'routine entry document is missing its "routine" key')
        return None
    raw_menus = data.get("menus")
    if not isinstance(raw_menus, list) or not raw_menus:
        report.error(label, "entry-document", 'routine entry document needs a non-empty "menus" array')
        return None
    routine = parse_routine(data["routine"], label, report)
    menus = [parse_menu(raw, label, report) for raw in raw_menus]
    if routine is None or any(menu is None for menu in menus):
        return None

    by_id: dict = {}
    for menu in menus:
        if menu["id"] in by_id:
            report.error(label, "menu-id", f"menu id {menu['id']!r} is used more than once in this document")
        by_id[menu["id"]] = menu
        check_segment_content(menu, entry, label, known_poses, report)

    resolvable = True
    for item in routine["items"]:
        if item["menuId"] not in by_id:
            resolvable = False
            report.error(
                label,
                "routine-items",
                f"item {item['id']} references menu id {item['menuId']!r}, not present in this document's menus",
            )
    referenced = {item["menuId"] for item in routine["items"]}
    for menu in menus:
        if menu["id"] not in referenced:
            report.warn(label, "routine-items", f"menu {menu['id']} is embedded but no routine item references it")
    if resolvable:
        total = sum(
            menu_total_seconds(by_id[item["menuId"]], item["setsOverride"]) for item in routine["items"]
        )
        if total != entry["estimatedSeconds"]:
            report.error(
                label,
                "estimated-seconds",
                f"index estimatedSeconds is {entry['estimatedSeconds']} but the routine totals {total} "
                "(sum of its items' menu durations at their effective sets)",
            )
    return {"menus": menus, "routine": routine, "by_id": by_id}


# ------------------------------------------------------------ cross-file checks


def describe_diff(inline: dict, standalone: dict):
    for key in MENU_SETTING_KEYS + ("description",):
        if inline[key] != standalone[key]:
            return f"{key} differs (inline {inline[key]!r} vs standalone {standalone[key]!r})"
    if len(inline["segments"]) != len(standalone["segments"]):
        return (
            f"segment count differs (inline {len(inline['segments'])} vs "
            f"standalone {len(standalone['segments'])})"
        )
    for position, (a, b) in enumerate(zip(inline["segments"], standalone["segments"])):
        for key in ("orderIndex", "sourceTemplateId", "name", "durationSeconds", "type", "description", "illustrationId"):
            if a[key] != b[key]:
                return f"segment #{position + 1} {key} differs (inline {a[key]!r} vs standalone {b[key]!r})"
    return None


def check_routine_links(entry: dict, by_id: dict, docs: dict, report: Report) -> None:
    label = "index.json"
    where = f"entry {entry['id']}"
    ids = entry["menuEntryIds"]
    if not ids:
        report.error(label, "menu-entry-ids", f"{where}: routine has no menuEntryIds")
    if entry["menuCount"] != len(ids):
        report.error(
            label,
            "menu-count",
            f"{where}: menuCount is {entry['menuCount']!r} but menuEntryIds has {len(ids)} items",
        )
    referenced_entries = []
    for ref in ids:
        target = by_id.get(ref)
        if target is None:
            report.error(label, "menu-entry-ids", f"{where}: menuEntryIds references {ref!r}, which is not an index id")
        elif target["type"] != "menu":
            report.error(label, "routine-refs-menus", f"{where}: menuEntryIds references {ref!r}, which is a {target['type']}, not a menu")
        else:
            referenced_entries.append(target)
    doc = docs.get(entry["id"])
    if len(referenced_entries) != len(ids):
        return

    if doc is not None and doc["routine"] is not None:
        overrides = any(item["setsOverride"] is not None for item in doc["routine"]["items"])
        if not overrides:
            total = sum(target["estimatedSeconds"] for target in referenced_entries)
            if total != entry["estimatedSeconds"]:
                report.error(
                    label,
                    "estimated-seconds",
                    f"{where}: estimatedSeconds is {entry['estimatedSeconds']} but its menu entries total {total}",
                )
        items = doc["routine"]["items"]
        if len(items) != len(ids):
            report.error(
                entry["entryPath"],
                "routine-menu-mismatch",
                f"routine has {len(items)} items but index menuEntryIds has {len(ids)}",
            )
            return
        for item, target in zip(items, referenced_entries):
            standalone = docs.get(target["id"])
            inline = doc["by_id"].get(item["menuId"])
            if standalone is None or inline is None:
                continue
            diff = describe_diff(inline, standalone["menus"][0])
            if diff:
                report.error(
                    entry["entryPath"],
                    "inline-menu-mismatch",
                    f"inline menu {inline['id']} differs from standalone entry {target['id']}: {diff}",
                )


def translation_group_ok(entry_ids, by_id: dict) -> bool:
    return any(
        all(other == root or by_id[other]["translationOf"] == root for other in entry_ids)
        for root in entry_ids
    )


def check_global_ids(docs: dict, by_id: dict, report: Report) -> None:
    """Ids must not repeat across the catalog, except between an entry and its
    translations (translationOf), which share ids by construction."""
    occurrences: dict = {}
    for entry_id, doc in docs.items():
        for menu in doc["menus"]:
            occurrences.setdefault(("menu", menu["id"]), set()).add((entry_id, menu["id"]))
            for segment in menu["segments"]:
                occurrences.setdefault(("segment", segment["id"]), set()).add((entry_id, menu["id"]))
        routine = doc["routine"]
        if routine:
            occurrences.setdefault(("routine", routine["id"]), set()).add((entry_id, routine["id"]))
            for item in routine["items"]:
                occurrences.setdefault(("routine item", item["id"]), set()).add((entry_id, routine["id"]))
    for (kind, value), places in sorted(occurrences.items()):
        if len(places) < 2:
            continue
        entry_ids = {entry_id for entry_id, _ in places}
        if len(entry_ids) > 1 and translation_group_ok(entry_ids, by_id):
            continue
        files = ", ".join(sorted(by_id[e]["entryPath"] for e in entry_ids))
        report.error(
            "index.json",
            f"duplicate-{kind.replace(' ', '-')}-id",
            f"{kind} id {value!r} appears more than once across the catalog ({files})",
        )


def check_entry_metadata(entry: dict, by_id: dict, report: Report) -> None:
    label = "index.json"
    where = f"entry {entry['id']}"
    if entry["minSchemaVersion"] > RELEASED_APP_SCHEMA_VERSION:
        report.warn(
            label,
            "min-schema-version",
            f"{where}: minSchemaVersion {entry['minSchemaVersion']} is above the released app's "
            f"{RELEASED_APP_SCHEMA_VERSION}; no installed app can fetch it yet",
        )
    for tag in entry["tags"]:
        if tag not in TAG_VOCABULARY:
            report.warn(label, "tag-vocabulary", f"{where}: tag {tag!r} is not in {sorted(TAG_VOCABULARY)}")
    expected = (entry["estimatedSeconds"] + 30) // 60  # round half up, as the existing titles do
    for locale, title in sorted(entry["title"].items()):
        match = _TITLE_MINUTES.search(title)
        if match and int(match[1]) != expected:
            report.warn(
                label,
                "title-minutes",
                f"{where}: title.{locale} says {match[1]} min but estimatedSeconds {entry['estimatedSeconds']} rounds to {expected}",
            )
    if entry["type"] == "menu" and (entry["menuCount"] is not None or entry["menuEntryIds"]):
        report.warn(label, "menu-entry-ids", f"{where}: menu entries should not carry menuCount/menuEntryIds")
    source = entry["translationOf"]
    if source is not None and (source == entry["id"] or source not in by_id):
        report.warn(label, "translation-of", f"{where}: translationOf {source!r} is not another index id")


def lint(root: Path, pose_ids_path: Path):
    """Returns (report, number of index entries)."""
    report = Report()
    known_poses = load_known_pose_ids(pose_ids_path, report)
    loaded = lint_index(root, report)
    if loaded is None:
        return report, 0
    entries, raw_count = loaded

    by_id: dict = {}
    for entry in entries:
        if entry["id"] in by_id:
            report.error("index.json", "duplicate-id", f"id {entry['id']!r} appears more than once in the index")
        else:
            by_id[entry["id"]] = entry

    docs: dict = {}
    seen_paths: dict = {}
    for entry in entries:
        if by_id[entry["id"]] is not entry:
            continue
        path_problems = check_entry_path(root, entry["entryPath"])
        for problem in path_problems:
            report.error("index.json", "entry-path", f"entry {entry['id']}: entryPath {entry['entryPath']!r} {problem}")
        if path_problems:
            continue
        if not entry["entryPath"].endswith(".json"):
            report.warn("index.json", "entry-path", f"entry {entry['id']}: entryPath does not end with .json")
        if entry["entryPath"] in seen_paths:
            report.error(
                "index.json",
                "entry-path",
                f"entries {seen_paths[entry['entryPath']]!r} and {entry['id']!r} share entryPath {entry['entryPath']!r}",
            )
            continue
        seen_paths[entry["entryPath"]] = entry["id"]
        doc = lint_entry_document(root, entry, known_poses, report)
        if doc is not None:
            docs[entry["id"]] = doc

    for entry in by_id.values():
        check_entry_metadata(entry, by_id, report)
        if entry["type"] == "routine":
            check_routine_links(entry, by_id, docs, report)
    check_global_ids(docs, by_id, report)

    entries_dir = root / "entries"
    if entries_dir.is_dir():
        referenced = {e["entryPath"] for e in entries}
        for path in sorted(entries_dir.rglob("*.json")):
            relative = path.relative_to(root).as_posix()
            if relative not in referenced:
                report.warn(relative, "orphan-entry", "file is not referenced by index.json")
    return report, raw_count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Lint the Intervalian public catalog.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="catalog directory holding index.json and entries/")
    parser.add_argument("--known-pose-ids", default=str(DEFAULT_POSE_IDS), help="file listing the app's pose ids")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    report, entry_count = lint(Path(args.root), Path(args.known_pose_ids))
    for line in report.lines:
        print(line)
    print(
        f"lint_catalog: {entry_count} index entries checked, "
        f"{report.errors} error(s), {report.warnings} warning(s)"
    )
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
