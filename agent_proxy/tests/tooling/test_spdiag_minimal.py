"""Offline checks of the SDK-only probe's measurement acceptance criteria."""
import csv
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from check_spdiag_minimal import compare, read_samples


class MinimalProbeTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "test-results").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="minimal-tools-", dir=ROOT / "test-results")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "summary.csv"
        self.samples = {"Sleep1s": 1_002_000_000, "Sleep5s": 5_015_000_000}
        self.rows = []
        for point, elapsed in self.samples.items():
            self.rows.append({"Program": "spdiag-minimal", "Module": "main.cc::measure",
                              "Point": point, "Lvl": "1", "Ticks": "1", "Good": "1",
                              "Bad": "0", "Not": "0", **{key: str(elapsed) for key in
                              ("Total(ns)", "Avg(ns)", "Min(ns)", "Max(ns)")}})

    def check(self):
        with self.path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=self.rows[0].keys())
            writer.writeheader()
            writer.writerows(self.rows)
        return compare(self.path, self.samples)

    def test_healthy_one_and_five_seconds(self):
        self.assertTrue(all(item["passed"] for item in self.check().values()))

    def test_wrapped_five_seconds_fails_but_one_second_passes(self):
        for key in ("Total(ns)", "Avg(ns)", "Min(ns)", "Max(ns)"):
            self.rows[1][key] = str(self.samples["Sleep5s"] % (2 ** 32))
        result = self.check()
        self.assertTrue(result["Sleep1s"]["passed"])
        self.assertFalse(result["Sleep5s"]["passed"])

    def test_double_count_fails_even_with_correct_time(self):
        self.rows[1].update(Ticks="2", Good="2")
        self.assertFalse(self.check()["Sleep5s"]["passed"])

    def test_abandon_fails(self):
        self.rows[0].update(Good="0", Not="1")
        self.assertFalse(self.check()["Sleep1s"]["passed"])

    def test_missing_or_foreign_points_fail(self):
        self.rows.pop()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.check()
        self.rows[0]["Program"] = "other-service"
        with self.assertRaisesRegex(ValueError, "unexpected"):
            self.check()

    def test_non_finite_duration_fails(self):
        self.rows[1]["Total(ns)"] = "nan"
        self.assertFalse(self.check()["Sleep5s"]["passed"])

    def test_read_independent_samples_and_reject_duplicates(self):
        output = "SDK log line\nSAMPLE\tSleep1s\t1000\t1002000000\nSAMPLE\tSleep5s\t5000\t5015000000\n"
        self.assertEqual(read_samples(output), self.samples)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            read_samples(output + "SAMPLE\tSleep5s\t5000\t5015000000\n")
        with self.assertRaisesRegex(ValueError, "missing"):
            read_samples("")


if __name__ == "__main__":
    unittest.main()
