"""Run with: python3 -m unittest discover -s tests."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/fpga-project-analyzer/scripts"))
from fpalib import model as M, summary


class ShowParamsTests(unittest.TestCase):
    def design(self, count, overrides=None):
        design = M.new_design("params")
        design["top"] = "top"
        design["defs"] = {
            "top": M.new_def("top", "rtl", insts=[{"name": "dut", "def": "dut", "params": overrides or {}}]),
            "dut": M.new_def("dut", "rtl", params={"P%d" % i: str(i) for i in range(count)}),
        }
        return design

    def params_line(self, design, path="/dut"):
        return next(line for line in summary.show(design, path).splitlines() if line.startswith("params "))

    def test_overrides_precede_defaults_and_report_hidden_count(self):
        design = self.design(30, {"p29": "true", "EXTRA": "42"})
        before = copy.deepcopy(design)
        line = self.params_line(design)
        self.assertTrue(line.startswith("params p29=true EXTRA=42 P0=0 "), line)
        self.assertTrue(line.endswith(" +7 more"), line)
        self.assertNotIn("P29=29", line)
        self.assertEqual(design, before)

    def test_exactly_24_parameters_are_not_truncated(self):
        line = self.params_line(self.design(24))
        self.assertIn("P23=23", line)
        self.assertNotIn("more", line)

    def test_defaults_only_report_truncation(self):
        line = self.params_line(self.design(25))
        self.assertTrue(line.endswith("P23=23 +1 more"), line)
        self.assertNotIn("P24=24", line)

    def test_more_than_24_overrides_still_report_truncation(self):
        line = self.params_line(self.design(0, {"O%d" % i: str(i) for i in range(25)}))
        self.assertTrue(line.endswith("O23=23 +1 more"), line)

    def test_top_level_defaults_report_truncation(self):
        design = self.design(25)
        design["top"] = "dut"
        self.assertTrue(self.params_line(design, "/").endswith("P23=23 +1 more"))

    def test_no_parameters_omit_the_line(self):
        self.assertFalse(any(line.startswith("params ") for line in summary.show(self.design(0), "/dut").splitlines()))


if __name__ == "__main__":
    unittest.main()
