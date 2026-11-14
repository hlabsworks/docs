"""Tests for lint_catalog.py.

Every error rule has a red case: copy the REAL catalog to a temp dir, apply
one targeted corruption, assert the CLI exits 1 with the expected message,
then undo it and assert the CLI is clean again. The repository is never
modified.

Run: python3 -m unittest discover -s intervalian/catalog/tools -p 'test_*.py' -v
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import lint_catalog  # noqa: E402

LINT = TOOLS_DIR / "lint_catalog.py"
REAL_ROOT = TOOLS_DIR.parent / "v1"

# Fixtures taken from the real catalog.
ROUTINE = "tue-fullbody-hiit"  # routine; its 4 menus are also standalone entries
STANDALONE = "tue-fullbody-hiit-warmup"  # menu, 11 segments, inline copy in ROUTINE
SINGLE = "sat-pm-recovery"  # menu, one work segment, no routine embeds it
INTERVAL_ROUTINE = "bike-training-30min"  # routine whose menus use sets > 1
INTERVAL_MENU = "bike-training-30min-main1"  # sets 8
TRANSLATION = "bike-training-30min-en"  # translationOf bike-training-30min


def dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class LintCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "v1"
        shutil.copytree(REAL_ROOT, self.root)
        self.pristine = self.tmp / "pristine"
        shutil.copytree(REAL_ROOT, self.pristine)

    # -- helpers -----------------------------------------------------------

    def run_lint(self, *args, cwd=None):
        proc = subprocess.run(
            [sys.executable, "-B", str(LINT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
        )
        self.assertEqual(proc.stderr, "", "lint wrote to stderr (crash?)")
        return proc.returncode, proc.stdout

    def lint(self):
        return self.run_lint("--root", str(self.root))

    def index(self):
        return json.loads((self.root / "index.json").read_text(encoding="utf-8"))

    def save_index(self, data):
        dump(self.root / "index.json", data)

    def index_entry(self, entry_id):
        return next(e for e in self.index()["entries"] if e["id"] == entry_id)

    def edit_index(self, fn):
        data = self.index()
        fn(data)
        self.save_index(data)

    def edit_index_entry(self, entry_id, fn):
        def apply(data):
            fn(next(e for e in data["entries"] if e["id"] == entry_id))

        self.edit_index(apply)

    def entry_path(self, entry_id):
        return self.root / self.index_entry(entry_id)["entryPath"]

    def edit_entry(self, entry_id, fn):
        path = self.entry_path(entry_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        fn(data)
        dump(path, data)

    def restore(self):
        shutil.rmtree(self.root)
        shutil.copytree(self.pristine, self.root)

    def error_lines(self, out):
        return [line for line in out.splitlines() if line.startswith("ERROR ")]

    def assertRed(self, out, rc, rule, contains=""):
        self.assertEqual(rc, 1, out)
        matches = [line for line in self.error_lines(out) if f"[{rule}]" in line and contains in line]
        self.assertTrue(matches, f"no ERROR [{rule}] containing {contains!r} in:\n{out}")

    def red_green(self, mutate, rule, contains=""):
        """mutate -> lint must fail with [rule]; restoring -> lint is clean."""
        mutate()
        rc, out = self.lint()
        self.assertRed(out, rc, rule, contains)
        self.restore()
        rc, out = self.lint()
        self.assertEqual((rc, self.error_lines(out)), (0, []), out)


class RealCatalogTest(LintCase):
    def test_real_catalog_has_no_errors(self):
        rc, out = self.run_lint("--root", str(REAL_ROOT))
        self.assertEqual((rc, self.error_lines(out)), (0, []), out)
        self.assertRegex(out, r"lint_catalog: \d+ index entries checked, 0 error\(s\)")

    def test_default_root_does_not_depend_on_cwd(self):
        rc, out = self.run_lint(cwd=str(self.tmp))
        self.assertEqual(rc, 0, out)

    def test_every_entry_file_is_referenced(self):
        _, out = self.run_lint("--root", str(REAL_ROOT))
        self.assertNotIn("orphan-entry", out)


class IndexFileTest(LintCase):
    def test_missing_index(self):
        self.red_green(lambda: (self.root / "index.json").unlink(), "index-missing")

    def test_index_not_json(self):
        def mutate():
            text = (self.root / "index.json").read_text(encoding="utf-8")
            (self.root / "index.json").write_text(text[: len(text) // 2], encoding="utf-8")

        self.red_green(mutate, "json", "index.json")

    def test_index_not_utf8(self):
        self.red_green(lambda: (self.root / "index.json").write_bytes(b'{"a": "\xff\xfe"}'), "utf8")

    def test_index_with_bom_is_rejected(self):
        raw = (self.root / "index.json").read_bytes()
        self.red_green(lambda: (self.root / "index.json").write_bytes(b"\xef\xbb\xbf" + raw), "json")

    def test_index_duplicate_key(self):
        raw = (self.root / "index.json").read_text(encoding="utf-8")
        self.red_green(
            lambda: (self.root / "index.json").write_text(raw.replace('"catalogVersion"', '"catalogVersion": 1, "catalogVersion"', 1), encoding="utf-8"),
            "json",
            "duplicate",
        )

    def test_index_nan_constant(self):
        raw = (self.root / "index.json").read_text(encoding="utf-8")
        self.red_green(
            lambda: (self.root / "index.json").write_text(raw.replace('"catalogVersion": 1', '"catalogVersion": NaN', 1), encoding="utf-8"),
            "json",
            "NaN",
        )

    def test_index_top_level_not_object(self):
        self.red_green(lambda: self.save_index([]), "index-field", "top-level")

    def test_catalog_version_must_be_1(self):
        self.red_green(lambda: self.edit_index(lambda d: d.update(catalogVersion=2)), "catalog-version")

    def test_catalog_version_must_be_int(self):
        self.red_green(lambda: self.edit_index(lambda d: d.update(catalogVersion="1")), "index-field", "catalogVersion")

    def test_catalog_version_bool_is_not_int(self):
        self.red_green(lambda: self.edit_index(lambda d: d.update(catalogVersion=True)), "index-field", "catalogVersion")

    def test_updated_at_must_parse(self):
        self.red_green(lambda: self.edit_index(lambda d: d.update(updatedAt="yesterday")), "index-field", "updatedAt")

    def test_updated_at_missing(self):
        self.red_green(lambda: self.edit_index(lambda d: d.pop("updatedAt")), "index-field", "updatedAt")

    def test_entries_must_be_array(self):
        self.red_green(lambda: self.edit_index(lambda d: d.update(entries={})), "index-field", "entries")

    def test_entry_element_must_be_object(self):
        self.red_green(lambda: self.edit_index(lambda d: d["entries"].append("oops")), "index-field", "not an object")


class IndexEntryFieldTest(LintCase):
    def red_entry(self, entry_id, fn, rule, contains=""):
        self.red_green(lambda: self.edit_index_entry(entry_id, fn), rule, contains)

    def test_missing_required_fields(self):
        for key in ("id", "type", "author", "entryPath", "publishedAt", "tags", "title", "description", "minSchemaVersion", "estimatedSeconds"):
            with self.subTest(key=key):
                self.red_entry(STANDALONE, lambda e, key=key: e.pop(key), "index-field", key)

    def test_empty_string_required_field(self):
        self.red_entry(STANDALONE, lambda e: e.update(author=""), "index-field", "author")

    def test_unknown_type(self):
        self.red_entry(STANDALONE, lambda e: e.update(type="playlist"), "index-field", "type")

    def test_min_schema_version_not_int(self):
        self.red_entry(STANDALONE, lambda e: e.update(minSchemaVersion="6"), "index-field", "minSchemaVersion")

    def test_estimated_seconds_not_int(self):
        self.red_entry(STANDALONE, lambda e: e.update(estimatedSeconds=390.0), "index-field", "estimatedSeconds")

    def test_published_at_unparseable(self):
        self.red_entry(STANDALONE, lambda e: e.update(publishedAt="2026/07/20"), "index-field", "publishedAt")

    def test_published_at_full_width_digits(self):
        self.red_entry(STANDALONE, lambda e: e.update(publishedAt="２０２６-07-20"), "index-field", "publishedAt")

    def test_published_at_trailing_newline(self):
        self.red_entry(STANDALONE, lambda e: e.update(publishedAt="2026-07-20\n"), "index-field", "publishedAt")

    def test_min_schema_version_beyond_64_bit(self):
        for value in (2**63, 10**20):
            with self.subTest(value=value):
                self.red_entry(STANDALONE, lambda e, value=value: e.update(minSchemaVersion=value), "index-field", "minSchemaVersion")

    def test_min_schema_version_float_exponent(self):
        raw = (self.root / "index.json").read_text(encoding="utf-8")
        self.assertIn('"minSchemaVersion": 6', raw)
        self.red_green(
            lambda: (self.root / "index.json").write_text(raw.replace('"minSchemaVersion": 6', '"minSchemaVersion": 1e20', 1), encoding="utf-8"),
            "index-field",
            "minSchemaVersion",
        )

    def test_tags_must_be_strings(self):
        self.red_entry(STANDALONE, lambda e: e.update(tags=["hiit", 3]), "index-field", "tags")

    def test_title_needs_en(self):
        self.red_entry(STANDALONE, lambda e: e["title"].pop("en"), "index-field", "en")

    def test_title_values_must_be_strings(self):
        self.red_entry(STANDALONE, lambda e: e["title"].update(ja=5), "index-field", "title")

    def test_empty_en_title(self):
        self.red_entry(STANDALONE, lambda e: e["title"].update(en=" "), "locale-text", "title.en")

    def test_ja_entry_needs_ja_description(self):
        self.red_entry(STANDALONE, lambda e: e["description"].pop("ja"), "locale-text", "description.ja")

    def test_ja_entry_needs_non_empty_ja_title(self):
        self.red_entry(STANDALONE, lambda e: e["title"].update(ja=""), "locale-text", "title.ja")

    def test_english_entry_does_not_need_ja(self):
        self.assertNotIn("ja", self.index_entry(TRANSLATION)["title"])
        rc, out = self.lint()
        self.assertEqual(rc, 0, out)

    def test_menu_count_must_be_int(self):
        self.red_entry(ROUTINE, lambda e: e.update(menuCount="4"), "index-field", "menuCount")

    def test_menu_entry_ids_must_be_strings(self):
        self.red_entry(ROUTINE, lambda e: e.update(menuEntryIds=[1]), "index-field", "menuEntryIds")

    def test_empty_language(self):
        self.red_entry(STANDALONE, lambda e: e.update(language=""), "index-field", "language")


class IndexCrossReferenceTest(LintCase):
    def test_duplicate_id(self):
        def mutate():
            self.edit_index(lambda d: d["entries"].append(dict(d["entries"][0], entryPath=d["entries"][1]["entryPath"])))

        self.red_green(mutate, "duplicate-id")

    def test_entry_file_deleted(self):
        self.red_green(lambda: self.entry_path(STANDALONE).unlink(), "entry-path", "does not exist")

    def test_entry_path_parent_segments(self):
        self.red_green(
            lambda: self.edit_index_entry(STANDALONE, lambda e: e.update(entryPath="entries/../index.json")),
            "entry-path",
            '".."',
        )

    def test_entry_path_absolute_url(self):
        self.red_green(
            lambda: self.edit_index_entry(STANDALONE, lambda e: e.update(entryPath="https://example.com/x.json")),
            "entry-path",
            "relative",
        )

    def test_entry_path_protocol_relative(self):
        self.red_green(
            lambda: self.edit_index_entry(STANDALONE, lambda e: e.update(entryPath="//example.com/x.json")),
            "entry-path",
            "relative",
        )

    def test_entry_path_whitespace(self):
        self.red_green(lambda: self.edit_index_entry(STANDALONE, lambda e: e.update(entryPath="  ")), "entry-path", "empty")

    def test_entry_path_percent_encoded_dots(self):
        self.red_green(
            lambda: self.edit_index_entry(STANDALONE, lambda e: e.update(entryPath="entries/%2e%2e/index.json")),
            "entry-path",
            "may only contain",
        )

    def test_entry_path_leading_slash(self):
        self.red_green(
            lambda: self.edit_index_entry(STANDALONE, lambda e: e.update(entryPath="/entries/x.json")),
            "entry-path",
            "must not start",
        )

    def test_entry_path_outside_entries_dir(self):
        self.red_green(
            lambda: self.edit_index_entry(STANDALONE, lambda e: e.update(entryPath="index.json")),
            "entry-path",
            "inside entries/",
        )

    def test_entry_path_wrong_letter_case(self):
        self.red_green(
            lambda: self.edit_index_entry(STANDALONE, lambda e: e.update(entryPath="entries/Tue-Fullbody-Hiit-Warmup.json")),
            "entry-path",
            "letter case",
        )

    def test_entry_path_shared_by_two_entries(self):
        def mutate():
            self.edit_index_entry(SINGLE, lambda e: e.update(entryPath=self.index_entry(STANDALONE)["entryPath"]))

        self.red_green(mutate, "entry-path", "share entryPath")

    def test_entry_path_symlink_escaping_entries(self):
        outside = self.tmp / "outside.json"
        outside.write_text("{}", encoding="utf-8")

        def mutate():
            path = self.entry_path(SINGLE)
            path.unlink()
            path.symlink_to(outside)

        self.red_green(mutate, "entry-path", "outside entries/")

    def test_menu_entry_ids_unknown_id(self):
        self.red_green(
            lambda: self.edit_index_entry(ROUTINE, lambda e: e["menuEntryIds"].__setitem__(0, "no-such-entry")),
            "menu-entry-ids",
            "no-such-entry",
        )

    def test_routine_may_only_reference_menus(self):
        self.red_green(
            lambda: self.edit_index_entry(ROUTINE, lambda e: e["menuEntryIds"].__setitem__(0, INTERVAL_ROUTINE)),
            "routine-refs-menus",
        )

    def test_routine_without_menu_entry_ids(self):
        def mutate():
            self.edit_index_entry(ROUTINE, lambda e: e.update(menuEntryIds=[], menuCount=0))

        self.red_green(mutate, "menu-entry-ids", "no menuEntryIds")

    def test_menu_count_must_equal_menu_entry_ids(self):
        self.red_green(lambda: self.edit_index_entry(ROUTINE, lambda e: e.update(menuCount=3)), "menu-count")

    def test_menu_count_missing_on_routine(self):
        self.red_green(lambda: self.edit_index_entry(ROUTINE, lambda e: e.pop("menuCount")), "menu-count")

    def test_routine_estimate_must_equal_sum_of_menus(self):
        self.red_green(
            lambda: self.edit_index_entry(ROUTINE, lambda e: e.update(estimatedSeconds=e["estimatedSeconds"] + 60)),
            "estimated-seconds",
            "menu entries total",
        )

    def test_routine_menu_estimate_drift_is_caught(self):
        self.red_green(
            lambda: self.edit_index_entry(STANDALONE, lambda e: e.update(estimatedSeconds=e["estimatedSeconds"] + 60)),
            "estimated-seconds",
            ROUTINE,
        )

    def test_index_over_size_limit(self):
        def mutate():
            self.edit_index_entry(STANDALONE, lambda e: e["description"].update(en="x" * (512 * 1024)))

        self.red_green(mutate, "size-limit", "index.json")

    def test_index_over_75_percent_only_warns(self):
        self.edit_index_entry(STANDALONE, lambda e: e["description"].update(en="x" * (400 * 1024)))
        rc, out = self.lint()
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"WARN index\.json: \[size-limit\]")

    def test_entry_over_size_limit(self):
        def mutate():
            self.edit_entry(STANDALONE, lambda d: d["menu"].update(description="x" * (256 * 1024)))

        self.red_green(mutate, "size-limit", "entries/")

    def test_entry_over_75_percent_only_warns(self):
        self.edit_entry(STANDALONE, lambda d: d["menu"].update(description="x" * (200 * 1024)))
        rc, out = self.lint()
        self.assertEqual(rc, 1, out)  # the inline copy in the routine no longer matches
        self.assertRegex(out, r"WARN entries/.*: \[size-limit\]")
        self.assertNotRegex(out, r"ERROR entries/[^:]*: \[size-limit\]")


class EntryDocumentTest(LintCase):
    def test_entry_not_json(self):
        self.red_green(lambda: self.entry_path(STANDALONE).write_text("{ nope", encoding="utf-8"), "json", "entries/")

    def test_entry_not_utf8(self):
        self.red_green(lambda: self.entry_path(STANDALONE).write_bytes(b"\xff\xff"), "utf8")

    def test_entry_top_level_not_object(self):
        self.red_green(lambda: self.entry_path(STANDALONE).write_text("[]", encoding="utf-8"), "entry-document")

    def test_schema_version_above_min_schema_version(self):
        self.red_green(
            lambda: self.edit_entry(STANDALONE, lambda d: d.update(schemaVersion=self.index_entry(STANDALONE)["minSchemaVersion"] + 1)),
            "schema-version",
            "unsupportedVersion",
        )

    def test_schema_version_missing(self):
        self.red_green(lambda: self.edit_entry(STANDALONE, lambda d: d.pop("schemaVersion")), "schema-version")

    def test_menu_entry_without_menu_key(self):
        self.red_green(lambda: self.edit_entry(STANDALONE, lambda d: d.pop("menu")), "entry-document", '"menu"')

    def test_routine_entry_without_routine_key(self):
        self.red_green(lambda: self.edit_entry(ROUTINE, lambda d: d.pop("routine")), "entry-document", '"routine"')

    def test_routine_entry_without_menus(self):
        self.red_green(lambda: self.edit_entry(ROUTINE, lambda d: d.pop("menus")), "entry-document", '"menus"')

    def test_routine_item_references_missing_menu(self):
        self.red_green(
            lambda: self.edit_entry(ROUTINE, lambda d: d["routine"]["items"][0].update(menuId="cat-nope")),
            "routine-items",
            "cat-nope",
        )

    def test_routine_item_order_gap(self):
        self.red_green(lambda: self.edit_entry(ROUTINE, lambda d: d["routine"]["items"][3].update(orderIndex=7)), "order-index", "item orderIndex")

    def test_routine_item_duplicate_order(self):
        self.red_green(lambda: self.edit_entry(ROUTINE, lambda d: d["routine"]["items"][3].update(orderIndex=0)), "order-index", "duplicate")

    def test_routine_items_empty(self):
        self.red_green(lambda: self.edit_entry(ROUTINE, lambda d: d["routine"].update(items=[])), "routine-items")

    def test_routine_fields(self):
        self.red_green(lambda: self.edit_entry(ROUTINE, lambda d: d["routine"].update(name="")), "routine-field", "name")

    def test_duplicate_menu_id_inside_routine_document(self):
        def mutate():
            self.edit_entry(ROUTINE, lambda d: d["menus"][1].update(id=d["menus"][0]["id"]))

        self.red_green(mutate, "menu-id", "more than once")

    def test_inline_menu_must_match_standalone(self):
        self.red_green(
            lambda: self.edit_entry(ROUTINE, lambda d: d["menus"][0]["segments"][0].update(name="changed")),
            "inline-menu-mismatch",
            "name differs",
        )

    def test_inline_menu_setting_must_match_standalone(self):
        self.red_green(
            lambda: self.edit_entry(ROUTINE, lambda d: d["menus"][0].update(prepareSeconds=61)),
            "inline-menu-mismatch",
            "prepareSeconds",
        )

    def test_inline_menu_segment_count_must_match_standalone(self):
        def mutate():
            self.edit_entry(STANDALONE, lambda d: d["menu"]["segments"].pop())

        self.red_green(mutate, "inline-menu-mismatch", "segment count")

    def test_inline_menu_name_and_timestamps_may_differ(self):
        self.edit_entry(ROUTINE, lambda d: d["menus"][0].update(name="renamed", createdAt="2020-01-01T00:00:00Z"))
        rc, out = self.lint()
        self.assertEqual(rc, 0, out)

    def test_routine_items_count_must_match_menu_entry_ids(self):
        def mutate():
            self.edit_index_entry(ROUTINE, lambda e: e.update(menuEntryIds=e["menuEntryIds"][:3], menuCount=3))

        self.red_green(mutate, "routine-menu-mismatch")


class MenuAndSegmentTest(LintCase):
    def segment(self, d, position=0):
        return d["menu"]["segments"][position]

    def test_menu_fields(self):
        for key in ("id", "name"):
            with self.subTest(key=key):
                self.red_green(lambda key=key: self.edit_entry(SINGLE, lambda d: d["menu"].update({key: ""})), "menu-field", key)

    def test_menu_sets_must_be_positive(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: d["menu"].update(sets=0)), "menu-field", "sets")

    def test_menu_numeric_setting_not_negative(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: d["menu"].update(prepareSeconds=-1)), "menu-field", "prepareSeconds")

    def test_menu_numeric_setting_beyond_64_bit(self):
        self.red_green(
            lambda: self.edit_entry(SINGLE, lambda d: d["menu"].update(cooldownCountdownSeconds=2**63)),
            "menu-field",
            "cooldownCountdownSeconds",
        )

    def test_menu_numeric_setting_must_be_int(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: d["menu"].update(cooldownSeconds="60")), "menu-field", "cooldownSeconds")

    def test_menu_timestamp_must_parse(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: d["menu"].update(createdAt="2026-13-40")), "menu-field", "createdAt")

    def test_menu_description_must_be_string(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: d["menu"].update(description=5)), "menu-field", "description")

    def test_menu_without_segments(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: d["menu"].update(segments=[])), "segments")

    def test_segment_name_required(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).pop("name")), "segment-field", "name")

    def test_segment_order_index_required(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).pop("orderIndex")), "segment-field", "orderIndex")

    def test_segment_not_object(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: d["menu"]["segments"].append(5)), "segment-field", "not an object")

    def test_segment_id_unique_within_menu(self):
        def mutate():
            self.edit_entry(STANDALONE, lambda d: self.segment(d, 1).update(id=self.segment(d, 0)["id"]))

        self.red_green(mutate, "segment-id", "more than once")

    def test_segment_id_unique_across_catalog(self):
        def mutate():
            other = json.loads(self.entry_path(STANDALONE).read_text(encoding="utf-8"))["menu"]["segments"][0]["id"]
            self.edit_entry(SINGLE, lambda d: self.segment(d).update(id=other))

        self.red_green(mutate, "duplicate-segment-id", "cat-tue-m0-s1-solo")

    def test_menu_id_unique_across_catalog(self):
        def mutate():
            other = json.loads(self.entry_path(STANDALONE).read_text(encoding="utf-8"))["menu"]["id"]
            self.edit_entry(SINGLE, lambda d: d["menu"].update(id=other))

        self.red_green(mutate, "duplicate-menu-id")

    def test_routine_id_unique_across_catalog(self):
        def mutate():
            other = json.loads(self.entry_path(ROUTINE).read_text(encoding="utf-8"))["routine"]["id"]
            self.edit_entry("wed-back", lambda d: d["routine"].update(id=other))

        self.red_green(mutate, "duplicate-routine-id")

    def test_translation_siblings_may_share_ids(self):
        ours = json.loads(self.entry_path(INTERVAL_MENU).read_text(encoding="utf-8"))
        theirs = json.loads(self.entry_path(INTERVAL_MENU + "-en").read_text(encoding="utf-8"))
        self.assertEqual(ours["menu"]["segments"][0]["id"], theirs["menu"]["segments"][0]["id"])
        rc, out = self.lint()
        self.assertEqual(rc, 0, out)

    def test_non_translation_may_not_share_ids_with_a_translation_pair(self):
        def mutate():
            self.edit_index_entry(TRANSLATION, lambda e: e.pop("translationOf"))

        self.red_green(mutate, "duplicate-segment-id")

    def test_order_index_gap(self):
        self.red_green(lambda: self.edit_entry(STANDALONE, lambda d: self.segment(d, 10).update(orderIndex=12)), "order-index", "contiguous")

    def test_order_index_must_start_at_zero(self):
        def mutate():
            def shift(d):
                for s in d["menu"]["segments"]:
                    s["orderIndex"] += 1

            self.edit_entry(STANDALONE, shift)

        self.red_green(mutate, "order-index", "contiguous from 0")

    def test_order_index_duplicate(self):
        self.red_green(lambda: self.edit_entry(STANDALONE, lambda d: self.segment(d, 1).update(orderIndex=0)), "order-index", "duplicate")

    def test_order_index_listing_order_only_warns(self):
        self.edit_entry(STANDALONE, lambda d: d["menu"]["segments"].reverse())
        _, out = self.lint()
        self.assertRegex(out, r"WARN entries/.*: \[order-index\].*orderIndex order")
        self.assertNotRegex(out, r"ERROR entries/[^:]*: \[order-index\]")

    def test_duration_must_be_positive(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).update(durationSeconds=0)), "duration")

    def test_duration_must_be_int(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).update(durationSeconds=1680.5)), "segment-field", "durationSeconds")

    def test_duration_negative(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).update(durationSeconds=-5)), "duration")

    def test_segment_type_unknown(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).update(type=9)), "segment-type")

    def test_segment_type_cooldown_is_not_a_catalog_type(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).update(type=3)), "segment-type", "menu-level")

    def test_segment_type_must_be_int(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).update(type="work")), "segment-type")

    def test_work_segment_needs_illustration_id(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).pop("illustrationId")), "illustration-id", "no illustrationId")

    def test_blank_illustration_id_counts_as_missing(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).update(illustrationId="  ")), "illustration-id", "no illustrationId")

    def test_unknown_illustration_id(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).update(illustrationId="not-a-pose")), "illustration-id", "not-a-pose")

    def test_illustration_id_too_long(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).update(illustrationId="x" * 41)), "segment-field", "at most 40")

    def test_rest_segment_must_not_carry_illustration_id(self):
        self.red_green(
            lambda: self.edit_entry(STANDALONE, lambda d: self.segment(d, 1).update(illustrationId="plank-forearm")),
            "illustration-on-rest",
        )

    def test_rest_segment_with_null_illustration_id_is_fine(self):
        self.edit_entry(STANDALONE, lambda d: self.segment(d, 1).update(illustrationId=None))
        _, out = self.lint()
        self.assertNotIn("illustration-on-rest", out)

    def test_estimated_seconds_menu(self):
        self.red_green(
            lambda: self.edit_index_entry(SINGLE, lambda e: e.update(estimatedSeconds=e["estimatedSeconds"] - 1)),
            "estimated-seconds",
            "totals 1800",
        )

    def test_estimated_seconds_follows_segment_edit(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: self.segment(d).update(durationSeconds=1700)), "estimated-seconds")

    def test_estimated_seconds_counts_prepare_and_cooldown(self):
        self.red_green(lambda: self.edit_entry(SINGLE, lambda d: d["menu"].update(cooldownSeconds=0)), "estimated-seconds")

    def test_estimated_seconds_counts_sets(self):
        self.red_green(lambda: self.edit_entry(INTERVAL_MENU, lambda d: d["menu"].update(sets=7)), "estimated-seconds", INTERVAL_MENU)

    def test_estimated_seconds_counts_rest_between_sets(self):
        def mutate():
            self.edit_entry(INTERVAL_MENU, lambda d: d["menu"].update(restBetweenSetsSeconds=5))

        self.red_green(mutate, "estimated-seconds", INTERVAL_MENU)

    def test_rest_between_sets_applies_between_sets_only(self):
        # 8 sets -> 7 gaps. Correct estimate in the index: the rule for this
        # file passes (other files still disagree because they embed a copy).
        self.edit_entry(INTERVAL_MENU, lambda d: d["menu"].update(restBetweenSetsSeconds=5))
        self.edit_index_entry(INTERVAL_MENU, lambda e: e.update(estimatedSeconds=e["estimatedSeconds"] + 7 * 5))
        _, out = self.lint()
        self.assertNotRegex(out, rf"ERROR entries/{INTERVAL_MENU}\.json: \[estimated-seconds\]")

    def test_default_menu_values_follow_the_app(self):
        menu = {"sets": 3, "prepareSeconds": 10, "restBetweenSetsSeconds": 30, "cooldownSeconds": 0}
        menu["segments"] = [{"durationSeconds": 20}]
        self.assertEqual(lint_catalog.menu_total_seconds(menu), 10 + 3 * 20 + 2 * 30 + 0)
        self.assertEqual(lint_catalog.menu_total_seconds(menu, sets=1), 10 + 20)
        self.assertEqual(lint_catalog.menu_total_seconds(menu, sets=0), 0)

    def test_routine_sets_override_is_honoured(self):
        def override():
            self.edit_entry(INTERVAL_ROUTINE, lambda d: d["routine"]["items"][1].update(setsOverride=4))

        override()
        rc, out = self.lint()
        self.assertRed(out, rc, "estimated-seconds", INTERVAL_ROUTINE)
        # 8 sets -> 4 sets removes 4 x (40 + 20) seconds; the app's
        # RoutineDurationCalculator would show exactly that total.
        self.edit_index_entry(INTERVAL_ROUTINE, lambda e: e.update(estimatedSeconds=e["estimatedSeconds"] - 240))
        rc, out = self.lint()
        self.assertEqual((rc, self.error_lines(out)), (0, []), out)

    def test_routine_sets_override_must_be_positive(self):
        self.red_green(
            lambda: self.edit_entry(INTERVAL_ROUTINE, lambda d: d["routine"]["items"][1].update(setsOverride=0)),
            "routine-items",
            "setsOverride",
        )


class WarningTest(LintCase):
    def warn_only(self, pattern):
        rc, out = self.lint()
        self.assertEqual((rc, self.error_lines(out)), (0, []), out)
        self.assertRegex(out, pattern)

    def test_single_set_menu_ending_on_rest_warns(self):
        def mutate(d):
            segment = d["menu"]["segments"][0]
            segment["type"] = 2
            segment.pop("illustrationId")

        self.edit_entry(SINGLE, mutate)
        self.warn_only(r"WARN entries/sat-pm-recovery\.json: \[last-segment\]")

    def test_interval_menu_ending_on_rest_is_accepted(self):
        _, out = self.lint()
        self.assertNotIn("[last-segment]", out)

    def test_japanese_work_description_over_40_characters_warns(self):
        self.edit_entry(STANDALONE, lambda d: d["menu"]["segments"][0].update(description="あ" * 41))
        self.edit_entry(ROUTINE, lambda d: d["menus"][0]["segments"][0].update(description="あ" * 41))
        self.warn_only(r"WARN entries/tue-fullbody-hiit-warmup\.json: \[ja-description-length\].*41 characters")

    def test_forty_characters_is_fine(self):
        self.edit_entry(STANDALONE, lambda d: d["menu"]["segments"][0].update(description="あ" * 40))
        self.edit_entry(ROUTINE, lambda d: d["menus"][0]["segments"][0].update(description="あ" * 40))
        _, out = self.lint()
        self.assertNotRegex(out, r"tue-fullbody-hiit-warmup\.json: \[ja-description-length\]")

    def test_english_entries_are_not_length_checked(self):
        _, out = self.lint()
        self.assertNotRegex(out, r"-en\.json: \[ja-description-length\]")

    def test_title_minutes_mismatch_warns(self):
        self.edit_index_entry(SINGLE, lambda e: e["title"].update(ja="回復ライド（45分）"))
        self.warn_only(r"WARN index\.json: \[title-minutes\].*sat-pm-recovery.*45 min")

    def test_title_minutes_round_half_up(self):
        _, out = self.lint()
        self.assertNotIn("[title-minutes]", out)

    def test_unknown_tag_warns(self):
        self.edit_index_entry(SINGLE, lambda e: e["tags"].append("yoga"))
        self.warn_only(r"WARN index\.json: \[tag-vocabulary\].*yoga")

    def test_orphan_entry_file_warns(self):
        (self.root / "entries" / "orphan.json").write_text("{}", encoding="utf-8")
        self.warn_only(r"WARN entries/orphan\.json: \[orphan-entry\]")

    def test_dangling_translation_of_warns(self):
        self.edit_index_entry(TRANSLATION, lambda e: e.update(translationOf="gone"))
        _, out = self.lint()
        self.assertRegex(out, r"WARN index\.json: \[translation-of\]")

    def test_menu_entry_with_routine_fields_warns(self):
        self.edit_index_entry(SINGLE, lambda e: e.update(menuCount=1))
        self.warn_only(r"WARN index\.json: \[menu-entry-ids\]")

    def test_embedded_but_unreferenced_menu_warns(self):
        def mutate(d):
            d["menus"].append(json.loads(json.dumps(d["menus"][0])))
            d["menus"][-1]["id"] = "cat-extra"
            for position, segment in enumerate(d["menus"][-1]["segments"]):
                segment["id"] = f"cat-extra-s{position}"

        self.edit_entry(ROUTINE, mutate)
        _, out = self.lint()
        self.assertRegex(out, r"WARN entries/tue-fullbody-hiit\.json: \[routine-items\].*cat-extra")

    def test_long_japanese_description_is_a_warning_not_an_error(self):
        self.edit_entry(SINGLE, lambda d: d["menu"]["segments"][0].update(description="あ" * 41))
        self.warn_only(r"WARN entries/sat-pm-recovery\.json: \[ja-description-length\]")
        self.edit_entry(SINGLE, lambda d: d["menu"]["segments"][0].update(description="あ" * 40))
        _, out = self.lint()
        self.assertNotRegex(out, r"sat-pm-recovery\.json: \[ja-description-length\]")

    def test_min_schema_version_above_released_app_warns(self):
        newer = lint_catalog.RELEASED_APP_SCHEMA_VERSION + 1
        self.edit_index_entry(SINGLE, lambda e: e.update(minSchemaVersion=newer))
        self.warn_only(r"WARN index\.json: \[min-schema-version\].*sat-pm-recovery")


class PoseIdsFileTest(LintCase):
    def pose_file(self, text):
        path = self.tmp / "poses.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_missing_pose_ids_file_is_an_error(self):
        rc, out = self.run_lint("--root", str(self.root), "--known-pose-ids", str(self.tmp / "absent.txt"))
        self.assertRed(out, rc, "pose-ids")

    def test_empty_pose_ids_file_is_an_error(self):
        rc, out = self.run_lint("--root", str(self.root), "--known-pose-ids", self.pose_file("# nothing\n\n"))
        self.assertRed(out, rc, "pose-ids", "no pose ids")

    def test_comments_and_blank_lines_are_ignored(self):
        ids = lint_catalog.load_known_pose_ids(lint_catalog.DEFAULT_POSE_IDS, lint_catalog.Report())
        lines = lint_catalog.DEFAULT_POSE_IDS.read_text(encoding="utf-8").splitlines()
        listed = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
        self.assertGreater(len(ids), 0)
        self.assertEqual(len(ids), len(listed))
        self.assertTrue(all(not i.startswith("#") for i in ids))

    def test_removing_a_used_pose_id_fails_the_catalog(self):
        ids = sorted(lint_catalog.load_known_pose_ids(lint_catalog.DEFAULT_POSE_IDS, lint_catalog.Report()) - {"cycling-riding"})
        rc, out = self.run_lint("--root", str(self.root), "--known-pose-ids", self.pose_file("\n".join(ids)))
        self.assertRed(out, rc, "illustration-id", "cycling-riding")


class DartDatetimeTest(unittest.TestCase):
    def test_accepts_what_dart_parses(self):
        for value in ("2026-07-20", "2026-07-20T09:15:00Z", "2026-07-20 09:15:00+09:00", "20260720", "2026-07-20T09:15:00.123Z"):
            with self.subTest(value=value):
                self.assertTrue(lint_catalog.is_dart_datetime(value))

    def test_rejects_what_dart_rejects(self):
        # "2026-02-30" and "T25:00:00Z" are stricter catalog conventions: Dart's
        # DateTime.parse accepts both (it rolls the date over).
        for value in (
            "2026/07/20", "yesterday", "2026-13-01", "2026-02-30", "2026-07-20T25:00:00Z", "", "7/20/2026",
            "２０２６-07-20",  # full-width digits
            "٢٠٢٦-07-20",  # Arabic-Indic digits
            "2026-07-20\n",  # trailing newline
        ):
            with self.subTest(value=value):
                self.assertFalse(lint_catalog.is_dart_datetime(value))


if __name__ == "__main__":
    unittest.main()
