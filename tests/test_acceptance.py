from __future__ import annotations

import unittest
from pathlib import Path

from plant_science.acceptance import run
from power_dispatch.acceptance import run as run_dispatch


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observation_count"], 6)
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["conclusion"], "pass")
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(len(result["input_sha256"]), 64)


class PowerDispatchAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run_dispatch(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["audit"]["valid"])
        tariff = result["tariff"]
        self.assertTrue(tariff["preview"]["holiday"])
        self.assertEqual(tariff["preview"]["segments"][0]["effective_price_cny_per_mwh"], "72.0000")
        self.assertEqual(tariff["bill"]["total_amount_cny"], "5952.00")
        self.assertFalse(tariff["bill"]["replayed"])
        self.assertTrue(tariff["replay"]["replayed"])
        self.assertEqual(
            {k: v for k, v in tariff["bill"].items() if k != "replayed"},
            {k: v for k, v in tariff["replay"].items() if k != "replayed"},
        )
        self.assertEqual(tariff["revision"]["recalculation_suggestions"], 1)
        suggestion = tariff["recalculations"]["suggestions"][0]
        self.assertEqual(suggestion["old_total_cny"], "5952.00")
        self.assertEqual(suggestion["new_total_cny"], "6048.00")
        self.assertEqual(suggestion["delta_cny"], "96.00")


if __name__ == "__main__":
    unittest.main()
