"""Offline checks for the isolated dependency and counter-validation tooling."""
import csv
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from prepare_spdiag import extract, relocate
from validate_spdiag import METHODS, verify_counts, verify_durations


class SpDiagToolsTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "test-results").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="spdiag-tools-", dir=ROOT / "test-results")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.cases = [{"method": method, "status": status} for method in METHODS
                      for status in ("ERROR_OK", "ERROR_INVALID_ARGUMENT")]
        self.rows = [{"Program": "agent-proxy", "Module": "service.cc::" + method,
                      "Point": method, "Lvl": "1", "Ticks": "2", "Good": "1", "Bad": "1",
                      "Not": "0", "Total(ns)": "100", "Max(ns)": "75"} for method in METHODS]

    def check_rows(self, *, success_only=False):
        path = self.directory / "summary.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=self.rows[0].keys())
            writer.writeheader()
            writer.writerows(self.rows)
        return verify_counts(path, self.cases, success_only=success_only)

    def test_exact_success_failure_counts(self):
        self.assertEqual(len(self.check_rows()), 6)

    def test_success_only_requires_one_good_per_rpc(self):
        self.cases = [case for case in self.cases if case["status"] == "ERROR_OK"]
        for row in self.rows:
            row.update(Ticks="1", Bad="0")
        self.assertEqual(len(self.check_rows(success_only=True)), 6)
        # The default full-suite validation must continue to require fault coverage.
        with self.assertRaisesRegex(AssertionError, "explicit failures"):
            self.check_rows()

    def test_success_only_rejects_even_matching_fault_counts(self):
        with self.assertRaisesRegex(AssertionError, "exactly one success"):
            self.check_rows(success_only=True)

    def test_success_only_still_checks_measured_durations(self):
        for row in self.rows:
            row.update({"Total(ns)": "2000000", "Max(ns)": "2000000"})
        self.check_rows()
        cases = [{"method": method, "proxy_elapsed_ms": 2} for method in METHODS]
        verify_durations(self.directory / "summary.csv", cases, require_long_sample=False)
        with self.assertRaisesRegex(AssertionError, "five-second"):
            verify_durations(self.directory / "summary.csv", cases)
        self.rows[0]["Total(ns)"] = "10"
        self.check_rows()
        with self.assertRaisesRegex(AssertionError, "below proxy timing"):
            verify_durations(self.directory / "summary.csv", cases, require_long_sample=False)

    def test_detect_abandon(self):
        self.rows[0]["Not"] = "1"
        with self.assertRaises(AssertionError):
            self.check_rows()

    def test_detect_double_end(self):
        self.rows[0]["Ticks"] = "3"
        self.rows[0]["Good"] = "2"
        with self.assertRaises(AssertionError):
            self.check_rows()

    def test_detect_wire_success_misclassified(self):
        self.rows[0].update(Good="0", Bad="2")
        with self.assertRaises(AssertionError):
            self.check_rows()

    def test_detect_missing_point(self):
        self.rows.pop()
        with self.assertRaises(AssertionError):
            self.check_rows()

    def test_detect_foreign_program(self):
        self.rows[0]["Program"] = "another-service"
        with self.assertRaises(AssertionError):
            self.check_rows()

    def test_long_duration(self):
        self.check_rows()
        for row in self.rows:
            if row["Point"] == "RunStep":
                row.update({"Total(ns)": "5000000000", "Max(ns)": "5000000000"})
        self.check_rows()
        verify_durations(self.directory / "summary.csv",
                         [{"method": "RunStep", "proxy_elapsed_ms": 5000}])

    def test_detect_long_duration_overflow(self):
        self.check_rows()
        for row in self.rows:
            if row["Point"] == "RunStep":
                row.update({"Total(ns)": "705032704", "Max(ns)": "705032704"})
        self.check_rows()
        with self.assertRaisesRegex(AssertionError, "overflow"):
            verify_durations(self.directory / "summary.csv",
                             [{"method": "RunStep", "proxy_elapsed_ms": 5000}])

    def test_reject_external_link(self):
        entries = [(PurePosixPath("usr/lib64/libspdiag.so"), stat.S_IFLNK | 0o777, b"../../elsewhere")]
        with self.assertRaises(ValueError):
            extract(entries, self.directory)

    def test_library_link_chain(self):
        entries = [(PurePosixPath("usr/lib64/libspdiag.so"), stat.S_IFLNK | 0o777, b"libspdiag.so.1"),
                   (PurePosixPath("usr/lib64/libspdiag.so.1"), stat.S_IFLNK | 0o777, b"libspdiag.so.1.0.0"),
                   (PurePosixPath("usr/lib64/libspdiag.so.1.0.0"), stat.S_IFREG | 0o755, b"library")]
        extract(entries, self.directory)
        self.assertEqual((self.directory / "usr/lib64/libspdiag.so").read_bytes(), b"library")

    def test_relocation_rejects_unexpected_export(self):
        package = self.directory / "usr/lib64/cmake/SpDiag"
        package.mkdir(parents=True)
        (package / "SpDiagTargets.cmake").write_text('set(_IMPORT_PREFIX "/different")', encoding="utf-8")
        with self.assertRaises(ValueError):
            relocate(self.directory)


if __name__ == "__main__":
    unittest.main()
