"""Unit tests for copper-golem.

External effects are mocked: `claude -p` (golem.classify / subprocess), launchctl,
and desktop notifications. Filesystem work uses real temp dirs. The module's
state-file globals are redirected into the temp dir per test.

Run:  python3 -m unittest discover -s tests   (or: python3 -m pytest tests)
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import zipfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import golem  # noqa: E402


def cfg(**over) -> dict:
    c = dict(golem.DEFAULT_CONFIG)
    c["stability_seconds"] = 0  # treat everything as settled by default in tests
    c["notify"] = False
    c.update(over)
    return c


def make_xlsx(path: Path, sheets, strings) -> None:
    """Write a minimal but valid .xlsx (zip with the parts golem reads)."""
    with zipfile.ZipFile(path, "w") as z:
        sheet_xml = "".join(
            f'<sheet name="{n}" sheetId="{i+1}" r:id="rId{i+1}"/>'
            for i, n in enumerate(sheets)
        )
        z.writestr("xl/workbook.xml", f"<workbook><sheets>{sheet_xml}</sheets></workbook>")
        si = "".join(f"<si><t>{s}</t></si>" for s in strings)
        z.writestr("xl/sharedStrings.xml", f"<sst>{si}</sst>")


class StateDirMixin(unittest.TestCase):
    """Redirect golem's state-file globals into a temp dir for the test."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        state = self.tmp / "state"
        self._patches = [
            mock.patch.object(golem, "STATE_DIR", state),
            mock.patch.object(golem, "MOVES_LOG", state / "moves.jsonl"),
            mock.patch.object(golem, "UNDONE_LOG", state / "undone.txt"),
            mock.patch.object(golem, "LOCK_FILE", state / "golem.lock"),
            mock.patch.object(golem, "NOMATCH_CACHE", state / "nomatch.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


# --------------------------------------------------------------------------- #
# load_config
# --------------------------------------------------------------------------- #

class TestLoadConfig(unittest.TestCase):
    def test_defaults_when_no_file(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COPPER_GOLEM_CONFIG", None)
            with mock.patch.object(golem, "DEFAULT_CONFIG_PATH", Path("/no/such/file.toml")):
                c = golem.load_config(None)
        self.assertEqual(c["model"], golem.DEFAULT_CONFIG["model"])
        self.assertTrue(c["dry_run"])

    def test_explicit_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            golem.load_config("/definitely/not/here.toml")

    def test_explicit_merges_over_defaults(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text('model = "claude-sonnet-4-6"\ndry_run = false\n')
            c = golem.load_config(str(p))
        self.assertEqual(c["model"], "claude-sonnet-4-6")
        self.assertFalse(c["dry_run"])
        # untouched keys keep defaults
        self.assertEqual(c["confidence_threshold"], golem.DEFAULT_CONFIG["confidence_threshold"])

    def test_env_var_path(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "env.toml"
            p.write_text('max_chars = 123\n')
            with mock.patch.dict(os.environ, {"COPPER_GOLEM_CONFIG": str(p)}):
                c = golem.load_config(None)
        self.assertEqual(c["max_chars"], 123)


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #

class TestExtract(unittest.TestCase):
    def test_text_includes_filename_and_content(self):
        with TemporaryDirectory() as d:
            f = Path(d) / "note.txt"
            f.write_text("hello body", encoding="utf-8")
            out = golem.extract_text(f, cfg())
        self.assertIn("filename: note.txt", out)
        self.assertIn("hello body", out)

    def test_text_is_bounded_by_max_chars(self):
        with TemporaryDirectory() as d:
            f = Path(d) / "big.txt"
            f.write_text("x" * 100000, encoding="utf-8")
            out = golem.extract_text(f, cfg(max_chars=200))
        self.assertLessEqual(len(out), 200)

    def test_unknown_extension_has_no_content(self):
        with TemporaryDirectory() as d:
            f = Path(d) / "thing.bin"
            f.write_bytes(b"\x00\x01\x02")
            out = golem.extract_text(f, cfg())
        self.assertIn("filename: thing.bin", out)
        self.assertIn("no extractable text content", out)

    def test_no_extension(self):
        with TemporaryDirectory() as d:
            f = Path(d) / "READMEZ"
            f.write_text("", encoding="utf-8")
            # .suffix is '' -> not in TEXT_EXTS, body empty
            out = golem.extract_text(f, cfg())
        self.assertIn("extension: (none)", out)

    def test_xlsx_extraction(self):
        with TemporaryDirectory() as d:
            f = Path(d) / "book.xlsx"
            make_xlsx(f, ["売上", "明細"], ["請求書", "合計金額"])
            out = golem.extract_text(f, cfg())
        self.assertIn("sheet: 売上", out)
        self.assertIn("請求書", out)
        self.assertIn("合計金額", out)

    def test_xlsx_bad_zip_returns_filename_only(self):
        with TemporaryDirectory() as d:
            f = Path(d) / "broken.xlsx"
            f.write_bytes(b"not a zip")
            out = golem.extract_text(f, cfg())
        self.assertIn("filename: broken.xlsx", out)
        self.assertIn("no extractable text content", out)


# --------------------------------------------------------------------------- #
# candidate folders & profiles
# --------------------------------------------------------------------------- #

class TestFolders(unittest.TestCase):
    def test_candidates_skip_hidden_files_and_no_match_dir(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "A").mkdir()
            (root / "B").mkdir()
            (root / ".hidden").mkdir()
            (root / "_unsorted").mkdir()
            (root / "file.txt").write_text("x")
            got = {p.name for p in golem.candidate_folders(root, cfg(on_no_match="_unsorted"))}
        self.assertEqual(got, {"A", "B"})

    def test_folder_profile_uses_golem_md(self):
        with TemporaryDirectory() as d:
            folder = Path(d) / "請求書"
            folder.mkdir()
            (folder / ".golem.md").write_text("会社からの請求書PDF", encoding="utf-8")
            prof = golem.folder_profile(folder)
        self.assertIn("会社からの請求書", prof)

    def test_folder_profile_falls_back_to_listing(self):
        with TemporaryDirectory() as d:
            folder = Path(d) / "x"
            folder.mkdir()
            (folder / "sample.pdf").write_text("x")
            prof = golem.folder_profile(folder)
        self.assertIn("example contents", prof)
        self.assertIn("sample.pdf", prof)


# --------------------------------------------------------------------------- #
# safety helpers
# --------------------------------------------------------------------------- #

class TestSafety(unittest.TestCase):
    def test_is_excluded(self):
        with TemporaryDirectory() as d:
            self.assertTrue(golem.is_excluded(Path(d) / "a.crdownload", cfg()))
            self.assertTrue(golem.is_excluded(Path(d) / ".DS_Store", cfg()))
            self.assertFalse(golem.is_excluded(Path(d) / "a.pdf", cfg()))

    def test_is_stable(self):
        with TemporaryDirectory() as d:
            f = Path(d) / "f.txt"
            f.write_text("x")
            old = f.stat().st_mtime - 1000
            os.utime(f, (old, old))
            self.assertTrue(golem.is_stable(f, cfg(stability_seconds=20)))
            now = golem.datetime.now().timestamp()
            os.utime(f, (now, now))
            self.assertFalse(golem.is_stable(f, cfg(stability_seconds=20)))

    def test_unique_dest_no_collision(self):
        with TemporaryDirectory() as d:
            dest = Path(d) / "a.txt"
            self.assertEqual(golem.unique_dest(dest), dest)

    def test_unique_dest_with_collision(self):
        with TemporaryDirectory() as d:
            dest = Path(d) / "a.txt"
            dest.write_text("x")
            self.assertEqual(golem.unique_dest(dest).name, "a (2).txt")
            (Path(d) / "a (2).txt").write_text("x")
            self.assertEqual(golem.unique_dest(dest).name, "a (3).txt")


# --------------------------------------------------------------------------- #
# classify parsing
# --------------------------------------------------------------------------- #

class TestParse(unittest.TestCase):
    def test_plain_json(self):
        d = golem._parse_classification('{"folder": "A", "confidence": 0.9}')
        self.assertEqual(d["folder"], "A")

    def test_fenced_json(self):
        d = golem._parse_classification('```json\n{"folder": "B", "confidence": 0.5}\n```')
        self.assertEqual(d["folder"], "B")

    def test_json_with_surrounding_text(self):
        d = golem._parse_classification('Here is the answer: {"folder": "C", "confidence": 1.0} done')
        self.assertEqual(d["folder"], "C")


class TestClassify(unittest.TestCase):
    def _run(self, *, returncode=0, stdout="", stderr="", side_effect=None):
        fake = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch.object(golem, "resolve_claude", return_value="/usr/bin/true"):
            with mock.patch.object(golem.subprocess, "run",
                                   side_effect=side_effect,
                                   return_value=fake) as run:
                return golem.classify("file", [], cfg()), run

    def test_success_unwraps_result(self):
        wrapped = json.dumps({"result": '{"folder": "A", "confidence": 0.95}'})
        d, _ = self._run(stdout=wrapped)
        self.assertEqual(d["folder"], "A")
        self.assertNotIn("error", d)

    def test_binary_not_found_is_error(self):
        with mock.patch.object(golem, "resolve_claude", return_value=None):
            d = golem.classify("file", [], cfg())
        self.assertTrue(d["error"])

    def test_nonzero_exit_is_error(self):
        d, _ = self._run(returncode=127, stderr="boom")
        self.assertTrue(d["error"])
        self.assertIn("127", d["reason"])

    def test_timeout_is_error(self):
        d, _ = self._run(side_effect=golem.subprocess.TimeoutExpired("claude", 1))
        self.assertTrue(d["error"])

    def test_unparseable_is_error(self):
        d, _ = self._run(stdout="not json at all")
        self.assertTrue(d["error"])

    def test_json_array_output_is_error_not_crash(self):
        d, _ = self._run(stdout="[1, 2, 3]")
        self.assertTrue(d["error"])

    def test_passes_model_and_disables_tools(self):
        wrapped = json.dumps({"result": '{"folder": null, "confidence": 0}'})
        _, run = self._run(stdout=wrapped)
        argv = run.call_args.args[0]
        self.assertIn("--model", argv)
        self.assertIn("claude-haiku-4-5", argv)
        self.assertIn("--allowedTools", argv)


# --------------------------------------------------------------------------- #
# process_root — the heart
# --------------------------------------------------------------------------- #

class TestProcessRoot(StateDirMixin):
    def _root(self, folders=("A", "B"), files=None):
        root = self.tmp / "root"
        root.mkdir()
        for f in folders:
            (root / f).mkdir()
        for name in (files or []):
            (root / name).write_text("content of " + name, encoding="utf-8")
        return root

    def _decider(self, mapping):
        """mapping: filename -> decision dict."""
        def fake_classify(file_block, folders, c):
            for name, dec in mapping.items():
                if f"filename: {name}" in file_block:
                    return dec
            return {"folder": None, "confidence": 0.0}
        return fake_classify

    def test_returns_four_tuple_when_not_a_dir(self):
        # regression: early returns must be 4-tuples (was (0,0) -> unpack crash)
        with redirect_stderr(io.StringIO()):
            r = golem.process_root(self.tmp / "missing", cfg(), "b")
        self.assertEqual(r, (0, 0, 0, 0))

    def test_returns_four_tuple_when_no_folders(self):
        root = self.tmp / "empty"
        root.mkdir()
        (root / "x.txt").write_text("x")
        with redirect_stdout(io.StringIO()):
            r = golem.process_root(root, cfg(), "b")
        self.assertEqual(r, (0, 0, 0, 0))

    def test_dry_run_counts_but_does_not_move(self):
        root = self._root(files=["a.txt", "b.txt"])
        dec = self._decider({
            "a.txt": {"folder": "A", "confidence": 0.9},
            "b.txt": {"folder": "B", "confidence": 0.9},
        })
        with mock.patch.object(golem, "classify", dec), redirect_stdout(io.StringIO()):
            considered, moved, kept, errors = golem.process_root(root, cfg(dry_run=True), "b")
        self.assertEqual((considered, moved, kept, errors), (2, 2, 0, 0))
        self.assertTrue((root / "a.txt").exists())  # not moved

    def test_live_moves_and_logs(self):
        root = self._root(files=["a.txt"])
        dec = self._decider({"a.txt": {"folder": "A", "confidence": 0.9}})
        with mock.patch.object(golem, "classify", dec), redirect_stdout(io.StringIO()):
            r = golem.process_root(root, cfg(dry_run=False), "batch1")
        self.assertEqual(r, (1, 1, 0, 0))
        self.assertFalse((root / "a.txt").exists())
        self.assertTrue((root / "A" / "a.txt").exists())
        self.assertTrue(golem.MOVES_LOG.is_file())

    def test_low_confidence_is_kept_not_moved(self):
        root = self._root(files=["a.txt"])
        dec = self._decider({"a.txt": {"folder": "A", "confidence": 0.3}})
        with mock.patch.object(golem, "classify", dec), redirect_stdout(io.StringIO()):
            r = golem.process_root(root, cfg(dry_run=False, confidence_threshold=0.6), "b")
        self.assertEqual(r, (1, 0, 1, 0))
        self.assertTrue((root / "a.txt").exists())  # kept in place

    def test_unknown_folder_name_is_kept(self):
        root = self._root(files=["a.txt"])
        dec = self._decider({"a.txt": {"folder": "Nonexistent", "confidence": 0.99}})
        with mock.patch.object(golem, "classify", dec), redirect_stdout(io.StringIO()):
            r = golem.process_root(root, cfg(dry_run=False), "b")
        self.assertEqual(r, (1, 0, 1, 0))
        self.assertTrue((root / "a.txt").exists())

    def test_classifier_error_is_counted_as_error(self):
        root = self._root(files=["a.txt"])
        dec = self._decider({"a.txt": {"folder": None, "confidence": 0.0, "error": True, "reason": "x"}})
        with mock.patch.object(golem, "classify", dec), redirect_stdout(io.StringIO()):
            r = golem.process_root(root, cfg(dry_run=False), "b")
        self.assertEqual(r, (1, 0, 0, 1))

    def test_string_confidence_does_not_crash(self):
        # regression: confidence as a string must not raise TypeError
        root = self._root(files=["a.txt"])
        dec = self._decider({"a.txt": {"folder": "A", "confidence": "0.9"}})
        with mock.patch.object(golem, "classify", dec), redirect_stdout(io.StringIO()):
            r = golem.process_root(root, cfg(dry_run=False), "b")
        self.assertEqual(r, (1, 1, 0, 0))

    def test_no_match_to_unsorted_counts_once_and_is_logged(self):
        # regression: must NOT double-count as both moved and kept
        root = self._root(files=["a.txt"])
        dec = self._decider({"a.txt": {"folder": None, "confidence": 0.0}})
        with mock.patch.object(golem, "classify", dec), redirect_stdout(io.StringIO()):
            considered, moved, kept, errors = golem.process_root(
                root, cfg(dry_run=False, on_no_match="_unsorted"), "b")
        self.assertEqual((considered, moved, kept, errors), (1, 0, 1, 0))
        self.assertEqual(moved + kept + errors, considered)  # buckets exclusive
        self.assertTrue((root / "_unsorted" / "a.txt").exists())  # relocated
        self.assertTrue(golem.MOVES_LOG.is_file())  # still undoable

    def test_move_failure_counted_as_error_not_fatal(self):
        # regression: a failing move must not abort the whole run
        root = self._root(files=["a.txt", "b.txt"])
        dec = self._decider({
            "a.txt": {"folder": "A", "confidence": 0.9},
            "b.txt": {"folder": "B", "confidence": 0.9},
        })
        real_move = golem.shutil.move
        calls = {"n": 0}

        def flaky_move(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_move(src, dst)

        with mock.patch.object(golem, "classify", dec), \
             mock.patch.object(golem.shutil, "move", flaky_move), \
             redirect_stdout(io.StringIO()):
            considered, moved, kept, errors = golem.process_root(root, cfg(dry_run=False), "b")
        self.assertEqual(considered, 2)
        self.assertEqual(errors, 1)
        self.assertEqual(moved, 1)  # the second file still processed

    def test_unstable_file_is_skipped(self):
        root = self._root(files=["a.txt"])
        now = golem.datetime.now().timestamp()
        os.utime(root / "a.txt", (now, now))
        dec = self._decider({"a.txt": {"folder": "A", "confidence": 0.9}})
        with mock.patch.object(golem, "classify", dec), redirect_stdout(io.StringIO()):
            r = golem.process_root(root, cfg(dry_run=False, stability_seconds=9999), "b")
        self.assertEqual(r, (0, 0, 0, 0))  # not considered
        self.assertTrue((root / "a.txt").exists())

    def test_excluded_files_ignored(self):
        root = self._root(files=["a.txt", "x.crdownload"])
        dec = self._decider({"a.txt": {"folder": "A", "confidence": 0.9}})
        with mock.patch.object(golem, "classify", dec), redirect_stdout(io.StringIO()):
            considered, moved, _, _ = golem.process_root(root, cfg(dry_run=False), "b")
        self.assertEqual(considered, 1)  # crdownload skipped
        self.assertTrue((root / "x.crdownload").exists())

    # ---- no-match cache (regression suite for the "stop re-checking" feature) ----

    def _counting(self, mapping):
        dec = self._decider(mapping)
        calls = {"n": 0}

        def counting(file_block, folders, c):
            calls["n"] += 1
            return dec(file_block, folders, c)
        return counting, calls

    def test_no_match_cached_and_skipped_next_run(self):
        root = self._root(files=["m.txt"])
        counting, calls = self._counting({"m.txt": {"folder": None, "confidence": 0.0}})
        cache = {}
        with mock.patch.object(golem, "classify", counting), redirect_stdout(io.StringIO()):
            r1 = golem.process_root(root, cfg(dry_run=False), "b1", cache)
            r2 = golem.process_root(root, cfg(dry_run=False), "b2", cache)
        self.assertEqual(r1, (1, 0, 1, 0))     # first run: classified, kept
        self.assertEqual(r2, (0, 0, 0, 0))     # second run: skipped silently
        self.assertEqual(calls["n"], 1)        # Claude called only once

    def test_new_folder_invalidates_cache(self):
        root = self._root(files=["m.txt"])
        counting, calls = self._counting({"m.txt": {"folder": None, "confidence": 0.0}})
        cache = {}
        with mock.patch.object(golem, "classify", counting), redirect_stdout(io.StringIO()):
            golem.process_root(root, cfg(dry_run=False), "b1", cache)
            (root / "C").mkdir()               # a new candidate folder appears
            r = golem.process_root(root, cfg(dry_run=False), "b2", cache)
        self.assertEqual(calls["n"], 2)        # re-evaluated after folder added
        self.assertEqual(r, (1, 0, 1, 0))

    def test_modified_kept_file_is_rechecked(self):
        root = self._root(files=["m.txt"])
        counting, calls = self._counting({"m.txt": {"folder": None, "confidence": 0.0}})
        cache = {}
        with mock.patch.object(golem, "classify", counting), redirect_stdout(io.StringIO()):
            golem.process_root(root, cfg(dry_run=False), "b1", cache)
            f = root / "m.txt"
            st = f.stat()
            # Different mtime (sig change), but in the past so it stays "stable".
            os.utime(f, (st.st_atime, st.st_mtime - 100))
            golem.process_root(root, cfg(dry_run=False), "b2", cache)
        self.assertEqual(calls["n"], 2)        # rechecked because it changed

    def test_dry_run_ignores_cache(self):
        root = self._root(files=["m.txt"])
        counting, calls = self._counting({"m.txt": {"folder": None, "confidence": 0.0}})
        cache = {}
        with mock.patch.object(golem, "classify", counting), redirect_stdout(io.StringIO()):
            golem.process_root(root, cfg(dry_run=True), "b1", cache)
            golem.process_root(root, cfg(dry_run=True), "b2", cache)
        self.assertEqual(calls["n"], 2)        # dry-run always shows full picture
        self.assertEqual(cache, {})            # and never writes the cache

    def test_matched_file_not_cached(self):
        root = self._root(files=["a.txt"])
        dec = self._decider({"a.txt": {"folder": "A", "confidence": 0.9}})
        cache = {}
        with mock.patch.object(golem, "classify", dec), redirect_stdout(io.StringIO()):
            golem.process_root(root, cfg(dry_run=False), "b1", cache)
        self.assertNotIn("a.txt", cache[str(root)]["kept"])

    def test_cache_disabled_rechecks_every_run(self):
        root = self._root(files=["m.txt"])
        counting, calls = self._counting({"m.txt": {"folder": None, "confidence": 0.0}})
        cache = {}
        with mock.patch.object(golem, "classify", counting), redirect_stdout(io.StringIO()):
            golem.process_root(root, cfg(dry_run=False, cache_no_match=False), "b1", cache)
            golem.process_root(root, cfg(dry_run=False, cache_no_match=False), "b2", cache)
        self.assertEqual(calls["n"], 2)


class TestDescribe(unittest.TestCase):
    """`describe` writes a .golem.md per category folder from Claude's summary."""

    def _root(self, folders=("請求書", "写真")):
        d = TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        for f in folders:
            (root / f).mkdir()
        return root

    def test_generate_profile_returns_text(self):
        root = self._root()
        (root / "請求書" / "invoice.txt").write_text("請求書 合計 1000円", encoding="utf-8")
        with mock.patch.object(golem, "_invoke_claude", return_value=("取引先からの請求書を入れる場所。", "")):
            text, err = golem.generate_profile(root / "請求書", cfg())
        self.assertEqual(err, "")
        self.assertEqual(text, "取引先からの請求書を入れる場所。")

    def test_generate_profile_strips_quotes_and_fences(self):
        root = self._root()
        (root / "請求書" / "a.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(golem, "_invoke_claude", return_value=('```\n「請求書の置き場」\n```', "")):
            text, _ = golem.generate_profile(root / "請求書", cfg())
        self.assertEqual(text, "請求書の置き場")

    def test_generate_profile_empty_folder(self):
        root = self._root()
        text, err = golem.generate_profile(root / "写真", cfg())  # no files
        self.assertIsNone(text)
        self.assertEqual(err, "empty")

    def test_generate_profile_claude_error(self):
        root = self._root()
        (root / "請求書" / "a.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(golem, "_invoke_claude", return_value=(None, "claude exit 1: boom")):
            text, err = golem.generate_profile(root / "請求書", cfg())
        self.assertIsNone(text)
        self.assertIn("boom", err)

    def test_describe_writes_files(self):
        root = self._root()
        (root / "請求書" / "a.txt").write_text("x", encoding="utf-8")
        (root / "写真" / "p.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(golem, "_invoke_claude", return_value=("説明文", "")):
            with redirect_stdout(io.StringIO()):
                golem.cmd_describe(cfg(dry_run=False), [root], force=False)
        self.assertEqual((root / "請求書" / ".golem.md").read_text(encoding="utf-8").strip(), "説明文")
        self.assertEqual((root / "写真" / ".golem.md").read_text(encoding="utf-8").strip(), "説明文")

    def test_describe_skips_existing_without_force(self):
        root = self._root(folders=("請求書",))
        (root / "請求書" / "a.txt").write_text("x", encoding="utf-8")
        (root / "請求書" / ".golem.md").write_text("既存の説明", encoding="utf-8")
        with mock.patch.object(golem, "_invoke_claude", return_value=("新しい説明", "")) as inv:
            with redirect_stdout(io.StringIO()):
                golem.cmd_describe(cfg(dry_run=False), [root], force=False)
        inv.assert_not_called()  # didn't even ask Claude
        self.assertEqual((root / "請求書" / ".golem.md").read_text(encoding="utf-8"), "既存の説明")

    def test_describe_overwrites_with_force(self):
        root = self._root(folders=("請求書",))
        (root / "請求書" / "a.txt").write_text("x", encoding="utf-8")
        (root / "請求書" / ".golem.md").write_text("古い説明", encoding="utf-8")
        with mock.patch.object(golem, "_invoke_claude", return_value=("新しい説明", "")):
            with redirect_stdout(io.StringIO()):
                golem.cmd_describe(cfg(dry_run=False), [root], force=True)
        self.assertEqual((root / "請求書" / ".golem.md").read_text(encoding="utf-8").strip(), "新しい説明")

    def test_describe_dry_run_does_not_write(self):
        root = self._root(folders=("請求書",))
        (root / "請求書" / "a.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(golem, "_invoke_claude", return_value=("説明文", "")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                golem.cmd_describe(cfg(dry_run=True), [root], force=False)
        self.assertFalse((root / "請求書" / ".golem.md").exists())
        self.assertIn("dry-run", buf.getvalue())

    def test_describe_skips_empty_folder(self):
        root = self._root(folders=("空",))
        with mock.patch.object(golem, "_invoke_claude") as inv:
            with redirect_stdout(io.StringIO()):
                golem.cmd_describe(cfg(dry_run=False), [root], force=False)
        inv.assert_not_called()
        self.assertFalse((root / "空" / ".golem.md").exists())


class TestNoMatchCacheEndToEnd(StateDirMixin):
    """cmd_once persists the cache across runs: a steady sweep stays silent."""

    def test_second_sweep_is_silent_and_does_not_renotify(self):
        root = self.tmp / "root"
        (root / "A").mkdir(parents=True)
        (root / "m.txt").write_text("unsortable", encoding="utf-8")

        def no_match(file_block, folders, c):
            return {"folder": None, "confidence": 0.0, "reason": "n/a"}

        c = dict(golem.DEFAULT_CONFIG)
        c.update(dry_run=False, stability_seconds=0, notify=True)

        with mock.patch.object(golem, "classify", side_effect=no_match) as classify:
            with mock.patch.object(golem, "_notify") as notify:
                with redirect_stdout(io.StringIO()):
                    golem.cmd_once(c, [root])   # 1st: kept + notify
                    golem.cmd_once(c, [root])   # 2nd: cached -> silent
        self.assertEqual(classify.call_count, 1)  # classified only once total
        self.assertEqual(notify.call_count, 1)    # notified only once total
        self.assertTrue(golem.NOMATCH_CACHE.is_file())


# --------------------------------------------------------------------------- #
# move log / undo
# --------------------------------------------------------------------------- #

class TestUndo(StateDirMixin):
    def test_roundtrip_move_and_undo(self):
        root = self.tmp / "root"
        (root / "A").mkdir(parents=True)
        src = root / "a.txt"
        src.write_text("hi")
        dst = root / "A" / "a.txt"
        golem.shutil.move(str(src), str(dst))
        golem.append_move("batch1", src, dst, {"confidence": 0.9, "reason": "r"})

        buf = io.StringIO()
        with redirect_stdout(buf):
            golem.cmd_undo(cfg())
        self.assertTrue(src.exists())       # restored
        self.assertFalse(dst.exists())

    def test_nothing_to_undo(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = golem.cmd_undo(cfg())
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to undo", buf.getvalue())

    def test_undo_tolerates_malformed_log_line(self):
        # regression: a corrupt JSONL line must not crash undo
        golem.STATE_DIR.mkdir(parents=True, exist_ok=True)
        root = self.tmp / "root"
        (root / "A").mkdir(parents=True)
        dst = root / "A" / "a.txt"
        dst.write_text("hi")
        with open(golem.MOVES_LOG, "w", encoding="utf-8") as f:
            f.write("THIS IS NOT JSON\n")
            f.write(json.dumps({"batch": "b1", "src": str(root / "a.txt"), "dst": str(dst)}) + "\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            golem.cmd_undo(cfg())
        self.assertTrue((root / "a.txt").exists())

    def test_undo_skips_already_undone_batch(self):
        golem.STATE_DIR.mkdir(parents=True, exist_ok=True)
        root = self.tmp / "root"
        (root / "A").mkdir(parents=True)
        dst = root / "A" / "a.txt"
        dst.write_text("hi")
        golem.append_move("b1", root / "a.txt", dst, {})
        with redirect_stdout(io.StringIO()):
            golem.cmd_undo(cfg())   # undoes b1
        buf = io.StringIO()
        with redirect_stdout(buf):
            golem.cmd_undo(cfg())   # nothing left
        self.assertIn("Nothing to undo", buf.getvalue())


# --------------------------------------------------------------------------- #
# cmd_once aggregation & notification
# --------------------------------------------------------------------------- #

class TestCmdOnce(StateDirMixin):
    def test_aggregates_and_notifies_on_live(self):
        roots = [self.tmp / "r1", self.tmp / "r2"]
        with mock.patch.object(golem, "process_root", side_effect=[(2, 1, 1, 0), (1, 1, 0, 0)]):
            with mock.patch.object(golem, "_notify") as notify:
                with redirect_stdout(io.StringIO()):
                    rc = golem.cmd_once(cfg(dry_run=False, notify=True), roots)
        self.assertEqual(rc, 0)
        notify.assert_called_once()

    def test_no_notification_in_dry_run(self):
        with mock.patch.object(golem, "process_root", return_value=(1, 1, 0, 0)):
            with mock.patch.object(golem, "_notify") as notify:
                with redirect_stdout(io.StringIO()):
                    golem.cmd_once(cfg(dry_run=True, notify=True), [self.tmp])
        notify.assert_not_called()

    def test_silent_on_empty_sweep(self):
        with mock.patch.object(golem, "process_root", return_value=(0, 0, 0, 0)):
            with mock.patch.object(golem, "_notify") as notify:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    golem.cmd_once(cfg(dry_run=False, notify=True), [self.tmp])
        self.assertEqual(buf.getvalue().strip(), "")
        notify.assert_not_called()


# --------------------------------------------------------------------------- #
# resolve_claude
# --------------------------------------------------------------------------- #

class TestResolveClaude(unittest.TestCase):
    def test_explicit_claude_bin(self):
        with TemporaryDirectory() as d:
            fake = Path(d) / "claude"
            fake.write_text("#!/bin/sh\n")
            self.assertEqual(golem.resolve_claude(cfg(claude_bin=str(fake))), str(fake))

    def test_explicit_missing_falls_through_to_which(self):
        with mock.patch.object(golem.shutil, "which", return_value="/usr/local/bin/claude"):
            got = golem.resolve_claude(cfg(claude_bin="/nope/claude"))
        self.assertEqual(got, "/usr/local/bin/claude")

    def test_none_when_nothing_found(self):
        with mock.patch.object(golem.shutil, "which", return_value=None):
            with mock.patch.object(golem.Path, "exists", return_value=False):
                with mock.patch.object(golem.Path, "glob", return_value=iter([])):
                    self.assertIsNone(golem.resolve_claude(cfg()))


# --------------------------------------------------------------------------- #
# install plist generation
# --------------------------------------------------------------------------- #

class TestInstall(unittest.TestCase):
    def _install(self, conf):
        with TemporaryDirectory() as d:
            plist = Path(d) / "agent.plist"
            with mock.patch.object(golem, "_plist_path", return_value=plist):
                with mock.patch.object(golem.subprocess, "run",
                                       return_value=mock.Mock(returncode=0, stderr="")):
                    with mock.patch.object(golem.Path, "mkdir", return_value=None):
                        with redirect_stdout(io.StringIO()):
                            golem.cmd_install(conf, ["/Users/x/Downloads/inbox"], None)
            return plist.read_text()

    def test_plist_has_watchpaths(self):
        xml = self._install(cfg(interval_seconds=0))
        self.assertIn("<key>WatchPaths</key>", xml)
        self.assertIn("/Users/x/Downloads/inbox", xml)

    def test_plist_omits_interval_when_zero(self):
        xml = self._install(cfg(interval_seconds=0))
        self.assertNotIn("StartInterval", xml)

    def test_plist_includes_interval_when_set(self):
        xml = self._install(cfg(interval_seconds=300))
        self.assertIn("<key>StartInterval</key>", xml)
        self.assertIn("<integer>300</integer>", xml)

    def test_plist_no_process_type(self):
        # ProcessType=Background suppressed notifications; must be gone
        xml = self._install(cfg())
        self.assertNotIn("ProcessType", xml)

    def test_interval_non_numeric_does_not_crash(self):
        xml = self._install(cfg(interval_seconds="oops"))
        self.assertNotIn("StartInterval", xml)

    def test_plist_xml_escapes_special_chars_in_paths(self):
        # regression: a watch root with &/<> must not corrupt the plist XML
        with TemporaryDirectory() as d:
            plist = Path(d) / "agent.plist"
            with mock.patch.object(golem, "_plist_path", return_value=plist):
                with mock.patch.object(golem.subprocess, "run",
                                       return_value=mock.Mock(returncode=0, stderr="")):
                    with mock.patch.object(golem.Path, "mkdir", return_value=None):
                        with redirect_stdout(io.StringIO()):
                            golem.cmd_install(cfg(), ["/Users/a & b/Downloads/inbox"], None)
            xml = plist.read_text()
        self.assertIn("&amp;", xml)
        self.assertNotIn("a & b", xml)  # raw ampersand must not appear
        # must still parse as valid XML
        import xml.dom.minidom as _m
        _m.parseString(xml)  # raises if malformed


# --------------------------------------------------------------------------- #
# main argument handling
# --------------------------------------------------------------------------- #

class TestMain(unittest.TestCase):
    def test_apply_flag_forces_live(self):
        captured = {}

        def fake_once(c, roots):
            captured["dry_run"] = c["dry_run"]
            return 0

        with mock.patch.object(golem, "load_config", return_value=cfg(dry_run=True)):
            with mock.patch.object(golem, "_acquire_lock"):
                with mock.patch.object(golem, "cmd_once", fake_once):
                    golem.main(["once", "--apply", "--root", "/tmp"])
        self.assertFalse(captured["dry_run"])

    def test_dry_run_flag_forces_dry(self):
        captured = {}

        def fake_once(c, roots):
            captured["dry_run"] = c["dry_run"]
            return 0

        with mock.patch.object(golem, "load_config", return_value=cfg(dry_run=False)):
            with mock.patch.object(golem, "_acquire_lock"):
                with mock.patch.object(golem, "cmd_once", fake_once):
                    golem.main(["once", "--dry-run", "--root", "/tmp"])
        self.assertTrue(captured["dry_run"])

    def test_missing_config_returns_2(self):
        with redirect_stderr(io.StringIO()):
            rc = golem.main(["once", "--config", "/no/such.toml"])
        self.assertEqual(rc, 2)

    def test_undo_dispatch(self):
        with mock.patch.object(golem, "load_config", return_value=cfg()):
            with mock.patch.object(golem, "cmd_undo", return_value=0) as undo:
                golem.main(["undo"])
        undo.assert_called_once()

    def test_describe_dispatch_passes_force(self):
        captured = {}

        def fake_describe(c, roots, force):
            captured["force"] = force
            return 0

        with mock.patch.object(golem, "load_config", return_value=cfg()):
            with mock.patch.object(golem, "cmd_describe", fake_describe):
                golem.main(["describe", "--force", "--root", "/tmp"])
        self.assertTrue(captured["force"])


if __name__ == "__main__":
    unittest.main()
