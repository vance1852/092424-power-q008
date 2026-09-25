from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from power_dispatch.api import JsonApplication
from power_dispatch.clock import FrozenClock
from power_dispatch.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from power_dispatch.service import SupplyService
from power_dispatch.tariff import (
    MeterInterval,
    TariffDefinition,
    TouPeriod,
    compute_bill,
    coverage_map,
    local_instant,
    preview_day,
)
from zoneinfo import ZoneInfo


def make_definition(**overrides: object) -> TariffDefinition:
    payload: dict[str, object] = {
        "timezone": "Asia/Shanghai",
        "periods": [
            {"kind": "valley", "start": "23:00", "end": "07:00", "price_cny_per_mwh": "60"},
            {"kind": "flat", "start": "07:00", "end": "08:00", "price_cny_per_mwh": "100"},
            {"kind": "peak", "start": "08:00", "end": "12:00", "price_cny_per_mwh": "150"},
            {"kind": "flat", "start": "12:00", "end": "17:00", "price_cny_per_mwh": "100"},
            {"kind": "peak", "start": "17:00", "end": "21:00", "price_cny_per_mwh": "150"},
            {"kind": "flat", "start": "21:00", "end": "23:00", "price_cny_per_mwh": "100"},
        ],
        "holiday_surcharge_percent": "20",
        "holidays": ["2026-10-01", "2026-10-02"],
    }
    payload.update(overrides)
    return TariffDefinition.from_dict(payload)


def reading(start_utc: str, end_utc: str, mwh: str) -> MeterInterval:
    parse = lambda text: datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    return MeterInterval(parse(start_utc), parse(end_utc), Decimal(mwh))


class TariffDefinitionTests(unittest.TestCase):
    def test_cross_midnight_period_expands_to_two_intervals(self) -> None:
        period = TouPeriod("valley", 23 * 60, 7 * 60, Decimal("60"))
        self.assertEqual(period.intervals(), ((1380, 1440), (0, 420)))

    def test_periods_must_cover_full_day_without_overlap_or_gap(self) -> None:
        with self.assertRaisesRegex(ValueError, "未覆盖"):
            coverage_map([TouPeriod("flat", 0, 720, Decimal("1"))])
        with self.assertRaisesRegex(ValueError, "重叠"):
            coverage_map([
                TouPeriod("flat", 0, 800, Decimal("1")),
                TouPeriod("peak", 700, 1440, Decimal("2")),
            ])
        slots = coverage_map([
            TouPeriod("valley", 1380, 420, Decimal("1")),
            TouPeriod("flat", 420, 1380, Decimal("2")),
        ])
        self.assertEqual(len(slots), 1440)
        self.assertEqual(slots[0].kind, "valley")
        self.assertEqual(slots[420].kind, "flat")
        self.assertEqual(slots[1379].kind, "flat")
        self.assertEqual(slots[1380].kind, "valley")

    def test_invalid_definition_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "IANA"):
            make_definition(timezone="Mars/Olympus")
        with self.assertRaisesRegex(ValueError, "起止时间不能相同"):
            make_definition(periods=[{"kind": "flat", "start": "08:00", "end": "08:00", "price_cny_per_mwh": "1"}])
        with self.assertRaisesRegex(ValueError, "peak、flat 或 valley"):
            make_definition(periods=[{"kind": "shoulder", "start": "00:00", "end": "24:00", "price_cny_per_mwh": "1"}])
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            make_definition(holidays=["2026-13-01"])

    def test_local_instant_maps_shanghai_midnight(self) -> None:
        from datetime import date

        instant = local_instant(ZoneInfo("Asia/Shanghai"), date(2026, 10, 1), 0)
        self.assertEqual(instant.isoformat(), "2026-09-30T16:00:00+00:00")


class ComputeBillTests(unittest.TestCase):
    def test_boundary_minute_belongs_to_period_starting_at_it(self) -> None:
        definition = make_definition(
            periods=[
                {"kind": "valley", "start": "00:00", "end": "08:00", "price_cny_per_mwh": "60"},
                {"kind": "peak", "start": "08:00", "end": "24:00", "price_cny_per_mwh": "150"},
            ],
            holiday_surcharge_percent="0",
            holidays=[],
        )
        # 本地 2026-10-09 07:59:00 - 08:01:00，跨 08:00 边界，临界分钟 08:00 归峰段
        bill = compute_bill(
            definition=definition,
            readings=[reading("2026-10-08T23:59:00Z", "2026-10-09T00:01:00Z", "2")],
            period_start=datetime(2026, 10, 9).date(),
            period_end=datetime(2026, 10, 9).date(),
        )
        self.assertEqual(len(bill["lines"]), 2)
        valley, peak = bill["lines"]
        self.assertEqual((valley["kind"], valley["energy_mwh"], valley["amount_cny"]), ("valley", "1.000", "60.00"))
        self.assertEqual((peak["kind"], peak["energy_mwh"], peak["amount_cny"]), ("peak", "1.000", "150.00"))
        self.assertEqual(peak["start_utc"], "2026-10-09T00:00:00Z")
        # 恰好从 08:00:00 开始的区间全部按峰段计价
        bill = compute_bill(
            definition=definition,
            readings=[reading("2026-10-09T00:00:00Z", "2026-10-09T00:01:00Z", "1")],
            period_start=datetime(2026, 10, 9).date(),
            period_end=datetime(2026, 10, 9).date(),
        )
        self.assertEqual(len(bill["lines"]), 1)
        self.assertEqual(bill["lines"][0]["kind"], "peak")

    def test_cross_midnight_valley_splits_at_local_midnight(self) -> None:
        definition = make_definition(
            periods=[
                {"kind": "valley", "start": "22:00", "end": "02:00", "price_cny_per_mwh": "60"},
                {"kind": "flat", "start": "02:00", "end": "22:00", "price_cny_per_mwh": "100"},
            ],
            holiday_surcharge_percent="0",
            holidays=[],
        )
        # 本地 2026-10-10 01:30 - 02:30，谷段跨午夜后 02:00 结束
        bill = compute_bill(
            definition=definition,
            readings=[reading("2026-10-09T17:30:00Z", "2026-10-09T18:30:00Z", "2")],
            period_start=datetime(2026, 10, 10).date(),
            period_end=datetime(2026, 10, 10).date(),
        )
        kinds = [(line["kind"], line["energy_mwh"]) for line in bill["lines"]]
        self.assertEqual(kinds, [("valley", "1.000"), ("flat", "1.000")])
        self.assertEqual(bill["totals"]["amount_cny"], "160.00")

    def test_holiday_surcharge_uses_rule_timezone_local_date(self) -> None:
        definition = make_definition(
            periods=[{"kind": "flat", "start": "00:00", "end": "24:00", "price_cny_per_mwh": "100"}],
            holiday_surcharge_percent="20",
            holidays=["2026-10-02"],
        )
        # UTC 2026-10-01 15:30-16:30 对应上海 10-01 23:30 至 10-02 00:30，跨本地午夜
        bill = compute_bill(
            definition=definition,
            readings=[reading("2026-10-01T15:30:00Z", "2026-10-01T16:30:00Z", "1")],
            period_start=datetime(2026, 10, 1).date(),
            period_end=datetime(2026, 10, 2).date(),
        )
        first, second = bill["lines"]
        self.assertEqual((first["local_date"], first["holiday"], first["amount_cny"]), ("2026-10-01", False, "50.00"))
        self.assertEqual((second["local_date"], second["holiday"], second["amount_cny"]), ("2026-10-02", True, "60.00"))
        self.assertEqual(second["effective_price_cny_per_mwh"], "120.0000")
        self.assertEqual(bill["totals"]["amount_cny"], "110.00")

    def test_rounding_trace_records_raw_and_rounded_values(self) -> None:
        definition = make_definition(
            periods=[
                {"kind": "valley", "start": "00:00", "end": "00:01", "price_cny_per_mwh": "100"},
                {"kind": "flat", "start": "00:01", "end": "24:00", "price_cny_per_mwh": "100"},
            ],
            holiday_surcharge_percent="0",
            holidays=[],
        )
        # 3 分钟 1 MWh，第 1 分钟谷段产生 1/3 无限循环电量
        bill = compute_bill(
            definition=definition,
            readings=[reading("2026-10-09T16:00:00Z", "2026-10-09T16:03:00Z", "1")],
            period_start=datetime(2026, 10, 10).date(),
            period_end=datetime(2026, 10, 10).date(),
        )
        first, second = bill["lines"]
        self.assertTrue(first["energy_raw_mwh"].startswith("0.333333"))
        self.assertEqual(first["energy_mwh"], "0.333")
        self.assertTrue(second["energy_raw_mwh"].startswith("0.666666"))
        self.assertEqual(second["energy_mwh"], "0.667")
        self.assertEqual(first["amount_cny"], "33.30")
        self.assertEqual(second["amount_cny"], "66.70")
        self.assertEqual(bill["totals"]["energy_mwh"], "1.000")
        self.assertEqual(bill["totals"]["amount_cny"], "100.00")

    def test_readings_outside_window_or_overlapping_rejected(self) -> None:
        definition = make_definition(holidays=[])
        day = datetime(2026, 10, 9).date()
        with self.assertRaisesRegex(ValueError, "超出结算期间"):
            compute_bill(
                definition=definition,
                readings=[reading("2026-10-08T15:00:00Z", "2026-10-08T16:30:00Z", "1")],
                period_start=day,
                period_end=day,
            )
        with self.assertRaisesRegex(ValueError, "重叠"):
            compute_bill(
                definition=definition,
                readings=[
                    reading("2026-10-09T01:00:00Z", "2026-10-09T03:00:00Z", "1"),
                    reading("2026-10-09T02:00:00Z", "2026-10-09T04:00:00Z", "1"),
                ],
                period_start=day,
                period_end=day,
            )
        with self.assertRaisesRegex(ValueError, "结束必须晚于开始"):
            compute_bill(
                definition=definition,
                readings=[reading("2026-10-09T03:00:00Z", "2026-10-09T03:00:00Z", "1")],
                period_start=day,
                period_end=day,
            )

    def test_preview_day_covers_24_hours_with_holiday_prices(self) -> None:
        definition = make_definition()
        holiday = preview_day(definition=definition, local_day=datetime(2026, 10, 1).date())
        self.assertTrue(holiday["holiday"])
        self.assertEqual(holiday["segments"][0]["start"], "00:00")
        self.assertEqual(holiday["segments"][-1]["end"], "24:00")
        valley = holiday["segments"][0]
        self.assertEqual((valley["kind"], valley["effective_price_cny_per_mwh"]), ("valley", "72.0000"))
        workday = preview_day(definition=definition, local_day=datetime(2026, 10, 9).date())
        self.assertFalse(workday["holiday"])
        self.assertEqual(workday["segments"][0]["effective_price_cny_per_mwh"], "60.0000")
        minutes = 0
        for segment in holiday["segments"]:
            start_h, start_m = segment["start"].split(":")
            end_h, end_m = segment["end"].split(":")
            minutes += (int(end_h) * 60 + int(end_m)) - (int(start_h) * 60 + int(start_m))
        self.assertEqual(minutes, 1440)

    def test_dst_transition_day_has_23_hours(self) -> None:
        definition = make_definition(
            timezone="America/New_York",
            periods=[{"kind": "flat", "start": "00:00", "end": "24:00", "price_cny_per_mwh": "100"}],
            holiday_surcharge_percent="0",
            holidays=[],
        )
        # 2026-03-08 美国夏令时开始，本地日只有 23 小时
        bill = compute_bill(
            definition=definition,
            readings=[reading("2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z", "23")],
            period_start=datetime(2026, 3, 8).date(),
            period_end=datetime(2026, 3, 8).date(),
        )
        self.assertEqual(bill["totals"]["energy_mwh"], "23.000")
        self.assertEqual(bill["totals"]["amount_cny"], "2300.00")
        with self.assertRaisesRegex(ValueError, "超出结算期间"):
            compute_bill(
                definition=definition,
                readings=[reading("2026-03-08T05:00:00Z", "2026-03-09T05:00:00Z", "24")],
                period_start=datetime(2026, 3, 8).date(),
                period_end=datetime(2026, 3, 8).date(),
            )


class TariffLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("plan2", "planner"),
            ("dispatch", "dispatcher"),
            ("risk", "risk"),
            ("risk2", "risk"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def rule_payload(self, rule_id: str = "tou-1", peak_price: str = "150", **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "rule_id": rule_id,
            "series": "tou-standard",
            "timezone": "Asia/Shanghai",
            "effective_from": "2026-10-01",
            "effective_to": "2026-10-31",
            "definition": {
                "periods": [
                    {"kind": "valley", "start": "23:00", "end": "07:00", "price_cny_per_mwh": "60"},
                    {"kind": "flat", "start": "07:00", "end": "08:00", "price_cny_per_mwh": "100"},
                    {"kind": "peak", "start": "08:00", "end": "12:00", "price_cny_per_mwh": peak_price},
                    {"kind": "flat", "start": "12:00", "end": "17:00", "price_cny_per_mwh": "100"},
                    {"kind": "peak", "start": "17:00", "end": "21:00", "price_cny_per_mwh": peak_price},
                    {"kind": "flat", "start": "21:00", "end": "23:00", "price_cny_per_mwh": "100"},
                ],
                "holiday_surcharge_percent": "20",
                "holidays": ["2026-10-01"],
            },
        }
        payload.update(overrides)
        return payload

    def publish(self, rule_id: str = "tou-1", **overrides: object) -> dict[str, object]:
        self.service.create_tariff_rule("plan", self.rule_payload(rule_id, **overrides))
        self.service.review_tariff_rule("risk", rule_id, 1)
        return self.service.publish_tariff_rule("risk2", rule_id, 2)

    def contract(self, contract_id: str = "ppa-1") -> dict[str, object]:
        return self.service.create_contract("plan", {
            "contract_id": contract_id,
            "counterparty": "华东售电公司",
            "series": "tou-standard",
            "service_start": "2026-10-01",
            "service_end": "2026-10-31",
        })

    def settle(self, contract_id: str, settlement_id: str, key: str, mwh: str = "10") -> dict[str, object]:
        return self.service.settle_contract("dispatch", {
            "settlement_id": settlement_id,
            "contract_id": contract_id,
            "period_start": "2026-10-02",
            "period_end": "2026-10-02",
            "idempotency_key": key,
            "readings": [{"start_utc": "2026-10-02T02:00:00Z", "end_utc": "2026-10-02T03:00:00Z", "mwh": mwh}],
        })

    def test_draft_review_publish_retire_flow(self) -> None:
        created = self.service.create_tariff_rule("plan", self.rule_payload())
        self.assertEqual((created["state"], created["version"], created["revision"]), ("draft", 1, 1))
        with self.assertRaises(InvalidState):
            self.service.publish_tariff_rule("risk2", "tou-1", 1)
        with self.assertRaises(Forbidden):
            self.service.review_tariff_rule("plan", "tou-1", 1)
        reviewed = self.service.review_tariff_rule("risk", "tou-1", 1)
        self.assertEqual((reviewed["state"], reviewed["revision"]), ("reviewed", 2))
        with self.assertRaises(Forbidden):
            self.service.publish_tariff_rule("risk", "tou-1", 2)
        published = self.service.publish_tariff_rule("risk2", "tou-1", 2)
        self.assertEqual((published["state"], published["revision"]), ("published", 3))
        with self.assertRaises(InvalidState):
            self.service.review_tariff_rule("risk2", "tou-1", 3)
        retired = self.service.retire_tariff_rule("risk", "tou-1", 3)
        self.assertEqual(retired["state"], "retired")
        with self.assertRaises(InvalidState):
            self.service.retire_tariff_rule("risk", "tou-1", 4)

    def test_published_content_cannot_be_overwritten(self) -> None:
        self.publish("tou-1")
        with self.assertRaises(Conflict):
            self.service.create_tariff_rule("plan", self.rule_payload("tou-1"))
        with self.assertRaises(Conflict):
            self.service.create_tariff_rule("plan2", self.rule_payload("tou-copy"))
        before = self.service.tariff_rule("tou-1")
        self.publish("tou-2", peak_price="155")
        after = self.service.tariff_rule("tou-1")
        self.assertEqual(before, after)
        self.assertEqual(after["state"], "published")

    def test_same_day_revision_resolves_highest_version(self) -> None:
        self.publish("tou-1")
        self.clock.advance(hours=2)
        published = self.publish("tou-2", peak_price="155")
        self.assertEqual([item["rule_id"] for item in published["overlaps"]], ["tou-1"])
        resolved = self.service.resolve_tariff("tou-standard", "2026-10-15")
        self.assertEqual((resolved["rule_id"], resolved["version"]), ("tou-2", 2))
        contract = self.contract()
        self.assertEqual((contract["rule_id"], contract["rule_version"]), ("tou-2", 2))

    def test_contract_pins_rule_at_signing_and_keeps_it_after_correction(self) -> None:
        self.publish("tou-1")
        old_contract = self.contract("ppa-old")
        self.assertEqual(old_contract["rule_version"], 1)
        self.publish("tou-2", peak_price="155")
        new_contract = self.contract("ppa-new")
        self.assertEqual(new_contract["rule_version"], 2)
        old_bill = self.settle("ppa-old", "set-old", "key-old")
        new_bill = self.settle("ppa-new", "set-new", "key-new")
        self.assertEqual((old_bill["rule_version"], old_bill["total_amount_cny"]), (1, "1500.00"))
        self.assertEqual((new_bill["rule_version"], new_bill["total_amount_cny"]), (2, "1550.00"))
        snapshot = self.service.settlement_bill("set-old")["rule_snapshot"]
        self.assertEqual(snapshot["version"], 1)
        peak_prices = {
            period["price_cny_per_mwh"]
            for period in snapshot["definition"]["periods"]
            if period["kind"] == "peak"
        }
        self.assertEqual(peak_prices, {"150"})

    def test_settlement_replay_returns_identical_bill(self) -> None:
        self.publish("tou-1")
        self.contract("ppa-1")
        first = self.settle("ppa-1", "set-1", "key-1")
        replay = self.settle("ppa-1", "set-1", "key-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual({k: v for k, v in first.items() if k != "replayed"}, {k: v for k, v in replay.items() if k != "replayed"})
        stored = self.service.settlement_bill("set-1")
        self.assertEqual(stored["totals"]["amount_cny"], first["total_amount_cny"])
        self.assertEqual(stored["rounding"]["mode"], "ROUND_HALF_UP")
        self.assertEqual(len(stored["inputs"]["input_sha256"]), 64)
        self.assertTrue(stored["lines"][0]["energy_raw_mwh"])
        with self.assertRaises(Conflict):
            self.settle("ppa-1", "set-2", "key-2")
        with self.assertRaises(Conflict):
            self.settle("ppa-1", "set-3", "key-1", mwh="11")

    def test_correction_generates_traceable_recalculation_suggestion(self) -> None:
        self.publish("tou-1")
        self.contract("ppa-1")
        self.settle("ppa-1", "set-1", "key-1")
        published = self.publish("tou-2", peak_price="155")
        self.assertEqual(published["recalculation_suggestions"], 1)
        result = self.service.recalculations("set-1")
        self.assertEqual(result["count"], 1)
        suggestion = result["suggestions"][0]
        self.assertEqual(suggestion["status"], "pending")
        self.assertEqual((suggestion["old_total_cny"], suggestion["new_total_cny"], suggestion["delta_cny"]), ("1500.00", "1550.00", "50.00"))
        self.assertEqual(suggestion["rule_id"], "tou-2")
        self.assertEqual(suggestion["detail"]["new_totals"]["amount_cny"], "1550.00")
        bill = self.service.settlement_bill("set-1")
        self.assertEqual(bill["totals"]["amount_cny"], "1500.00")
        self.assertEqual(bill["rule_snapshot"]["version"], 1)
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_partial_overlap_suggestion_marks_manual_review(self) -> None:
        self.publish("tou-1")
        self.service.create_contract("plan", {
            "contract_id": "ppa-1", "counterparty": "华东售电公司", "series": "tou-standard",
            "service_start": "2026-10-01", "service_end": "2026-10-15",
        })
        self.service.settle_contract("dispatch", {
            "settlement_id": "set-1", "contract_id": "ppa-1",
            "period_start": "2026-10-01", "period_end": "2026-10-15", "idempotency_key": "key-1",
            "readings": [{"start_utc": "2026-10-02T02:00:00Z", "end_utc": "2026-10-02T03:00:00Z", "mwh": "10"}],
        })
        published = self.publish("tou-2", peak_price="155", effective_from="2026-10-10", effective_to="2026-10-31")
        self.assertEqual(published["recalculation_suggestions"], 1)
        suggestion = self.service.recalculations("set-1")["suggestions"][0]
        self.assertIsNone(suggestion["new_total_cny"])
        self.assertIsNone(suggestion["delta_cny"])
        self.assertIn("未完整覆盖", suggestion["detail"]["note"])

    def test_preview_available_before_publish(self) -> None:
        self.service.create_tariff_rule("plan", self.rule_payload())
        preview = self.service.preview_tariff("tou-1", "2026-10-01")
        self.assertEqual(preview["state"], "draft")
        self.assertTrue(preview["holiday"])
        peak = [s for s in preview["segments"] if s["kind"] == "peak"][0]
        self.assertEqual(peak["effective_price_cny_per_mwh"], "180.0000")
        workday = self.service.preview_tariff("tou-1", "2026-10-09")
        self.assertFalse(workday["holiday"])
        peak = [s for s in workday["segments"] if s["kind"] == "peak"][0]
        self.assertEqual(peak["effective_price_cny_per_mwh"], "150.0000")

    def test_retired_rule_stops_resolution_but_pinned_contract_still_bills(self) -> None:
        self.publish("tou-1")
        self.contract("ppa-1")
        self.service.retire_tariff_rule("risk", "tou-1", 3)
        with self.assertRaises(NotFound):
            self.service.resolve_tariff("tou-standard", "2026-10-15")
        with self.assertRaises(InvalidState):
            self.contract("ppa-2")
        bill = self.settle("ppa-1", "set-1", "key-1")
        self.assertEqual(bill["total_amount_cny"], "1500.00")

    def test_settlement_validates_period_and_permissions(self) -> None:
        self.publish("tou-1")
        self.contract("ppa-1")
        with self.assertRaises(Forbidden):
            self.service.settle_contract("plan", {})
        with self.assertRaises(ValidationFailed):
            self.service.settle_contract("dispatch", {
                "settlement_id": "set-x",
                "contract_id": "ppa-1",
                "period_start": "2026-11-01",
                "period_end": "2026-11-02",
                "idempotency_key": "key-x",
                "readings": [{"start_utc": "2026-11-01T00:00:00Z", "end_utc": "2026-11-01T01:00:00Z", "mwh": "1"}],
            })
        with self.assertRaises(NotFound):
            self.service.settle_contract("dispatch", {
                "settlement_id": "set-y",
                "contract_id": "ppa-missing",
                "period_start": "2026-10-02",
                "period_end": "2026-10-02",
                "idempotency_key": "key-y",
                "readings": [{"start_utc": "2026-10-02T00:00:00Z", "end_utc": "2026-10-02T01:00:00Z", "mwh": "1"}],
            })

    def test_api_exposes_tariff_lifecycle(self) -> None:
        app = JsonApplication(self.service)
        headers = {"X-Actor-Id": "plan"}
        response = app.handle("POST", "/tariff/rules", headers, json_body(self.rule_payload()))
        self.assertEqual(response.status, 201)
        response = app.handle("GET", "/tariff/rules/tou-1/preview?date=2026-10-01", headers)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["holiday"])
        self.assertEqual(app.handle("POST", "/tariff/rules/tou-1/review", {"X-Actor-Id": "risk"}, json_body({"expected_revision": 1})).status, 200)
        self.assertEqual(app.handle("POST", "/tariff/rules/tou-1/publish", {"X-Actor-Id": "risk2"}, json_body({"expected_revision": 2})).status, 200)
        resolved = app.handle("GET", "/tariff/resolve?series=tou-standard&date=2026-10-15", headers)
        self.assertEqual(resolved.body["rule_id"], "tou-1")
        contract = app.handle("POST", "/contracts", headers, json_body({
            "contract_id": "ppa-1", "counterparty": "华东售电公司", "series": "tou-standard",
            "service_start": "2026-10-01", "service_end": "2026-10-31",
        }))
        self.assertEqual(contract.status, 201)
        settlement = app.handle("POST", "/settlements", {"X-Actor-Id": "dispatch"}, json_body({
            "settlement_id": "set-1", "contract_id": "ppa-1", "period_start": "2026-10-02",
            "period_end": "2026-10-02", "idempotency_key": "key-1",
            "readings": [{"start_utc": "2026-10-02T02:00:00Z", "end_utc": "2026-10-02T03:00:00Z", "mwh": "10"}],
        }))
        self.assertEqual(settlement.status, 201)
        self.assertEqual(settlement.body["total_amount_cny"], "1500.00")
        bill = app.handle("GET", "/settlements/set-1", headers)
        self.assertEqual(bill.body["rule_snapshot"]["version"], 1)
        suggestions = app.handle("GET", "/settlements/set-1/recalculations", headers)
        self.assertEqual(suggestions.body["count"], 0)
        missing = app.handle("GET", "/tariff/rules/tou-missing", headers)
        self.assertEqual(missing.status, 404)


def json_body(payload: dict[str, object]) -> bytes:
    import json

    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
