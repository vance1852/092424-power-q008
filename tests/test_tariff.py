"""分时电价规则生命周期、预览/正式结算与重算建议测试。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from power_dispatch.api import JsonApplication
from power_dispatch.clock import FrozenClock
from power_dispatch.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from power_dispatch.service import SupplyService
from power_dispatch.tariff import (
    TariffRule,
    TariffRuleError,
    day_intervals,
    parse_consumption,
    price_bill,
)


def simple_rule(tag: str = "v1", **overrides: object) -> dict[str, object]:
    rule: dict[str, object] = {
        "timezone": "Asia/Shanghai",
        "version_tag": tag,
        "holidays": ["2026-10-01"],
        "periods": [
            {"kind": "valley", "day_type": "all", "start": "00:00", "end": "06:00", "price_cny": "0.30"},
            {"kind": "flat", "day_type": "all", "start": "06:00", "end": "08:00", "price_cny": "0.70"},
            {"kind": "peak", "day_type": "all", "start": "08:00", "end": "22:00",
             "price_cny": "1.10", "holiday_add_cny": "0.20"},
            {"kind": "flat", "day_type": "all", "start": "22:00", "end": "24:00", "price_cny": "0.70"},
        ],
    }
    rule.update(overrides)
    return rule


class RuleValidationTests(unittest.TestCase):
    def test_overlapping_periods_rejected(self) -> None:
        raw = simple_rule(periods=[
            {"kind": "peak", "day_type": "all", "start": "08:00", "end": "22:00", "price_cny": "1"},
            {"kind": "flat", "day_type": "all", "start": "21:00", "end": "23:00", "price_cny": "0.7"},
        ])
        with self.assertRaises(TariffRuleError):
            TariffRule.from_dict(raw)

    def test_gap_rejected(self) -> None:
        raw = simple_rule(periods=[
            {"kind": "valley", "day_type": "all", "start": "00:00", "end": "06:00", "price_cny": "0.3"},
            {"kind": "peak", "day_type": "all", "start": "08:00", "end": "24:00", "price_cny": "1"},
        ])
        with self.assertRaises(TariffRuleError):
            TariffRule.from_dict(raw)

    def test_cross_midnight_period_covers_full_day(self) -> None:
        raw = simple_rule(periods=[
            {"kind": "valley", "day_type": "all", "start": "22:00", "end": "06:00", "price_cny": "0.3"},
            {"kind": "flat", "day_type": "all", "start": "06:00", "end": "08:00", "price_cny": "0.7"},
            {"kind": "peak", "day_type": "all", "start": "08:00", "end": "22:00", "price_cny": "1.1"},
        ])
        rule = TariffRule.from_dict(raw)
        intervals = day_intervals(rule, __import__("datetime").date(2026, 9, 24), "workday")
        starts = [item.start_utc.isoformat() for item in intervals]
        # 22:00-06:00 的谷段应翻卷到前一日 14:00Z 开始
        self.assertTrue(starts[0].endswith("2026-09-23T14:00:00+00:00"))
        covered = sum(item.minutes() for item in intervals)
        self.assertEqual(covered, 1440)

    def test_half_open_boundary_minutes(self) -> None:
        rule = TariffRule.from_dict(simple_rule())
        import datetime as dt
        intervals = day_intervals(rule, dt.date(2026, 9, 24), "workday")
        # 08:00 本地（00:00Z）整分钟属于峰段，而不是平段
        peak = next(i for i in intervals if i.period.kind == "peak")
        self.assertEqual(peak.start_utc, dt.datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc))

    def test_dst_transition_day_uses_real_wall_clock(self) -> None:
        # 纽约 2026-11-01 02:00 结束夏令时，当日本地 24 小时 = 1500 分钟 UTC
        raw = simple_rule(timezone="America/New_York",
                          periods=[{"kind": "flat", "day_type": "all", "start": "00:00",
                                    "end": "24:00", "price_cny": "0.5"}])
        rule = TariffRule.from_dict(raw)
        import datetime as dt
        intervals = day_intervals(rule, dt.date(2026, 11, 1), "workday")
        self.assertEqual(sum(i.minutes() for i in intervals), 1500)

    def test_invalid_timezone_rejected(self) -> None:
        with self.assertRaises(TariffRuleError):
            TariffRule.from_dict(simple_rule(timezone="Mars/Olympus"))

    def test_holiday_table_replaces_default(self) -> None:
        raw = simple_rule(periods=[
            {"kind": "valley", "day_type": "all", "start": "00:00", "end": "12:00", "price_cny": "0.3"},
            {"kind": "peak", "day_type": "all", "start": "12:00", "end": "24:00", "price_cny": "1"},
            {"kind": "valley", "day_type": "holiday", "start": "00:00", "end": "24:00", "price_cny": "0.2"},
        ])
        rule = TariffRule.from_dict(raw)
        holiday_periods = rule.periods_for("holiday")
        self.assertEqual(len(holiday_periods), 1)
        self.assertEqual(rule.periods_for("workday")[0].day_type, "all")


class PricingMathTests(unittest.TestCase):
    def test_holiday_surcharge_and_rounding_trace(self) -> None:
        import datetime as dt
        rule = TariffRule.from_dict(simple_rule())
        consumption = parse_consumption([
            {"start_utc": "2026-09-30T16:00:00Z", "end_utc": "2026-10-01T16:00:00Z",
             "meter_kwh": "2400"},
        ])
        result = price_bill(rule, dt.date(2026, 10, 1), consumption)
        self.assertEqual(result["day_class"], "holiday")
        # 谷 600*0.30=180，平 200*0.70*2=280，峰(节假日加价) 1400*1.30=1820
        self.assertEqual(result["total_amount_cny"], "2280.00")
        peak_line = next(line for line in result["lines"] if line["kind"] == "peak")
        self.assertEqual(peak_line["unit_price_cny_per_kwh"], "1.30")
        self.assertEqual(peak_line["amount_raw"], peak_line["amount_cny"])

    def test_partial_slice_allocation_by_minute_weight(self) -> None:
        import datetime as dt
        rule = TariffRule.from_dict(simple_rule())
        # 节假日规则日窗口 09-30T16:00Z–10-01T16:00Z；平/峰边界在 10-01T00:00Z。
        # 用零电量段补齐全天，只让跨越边界的 60 分钟有 60 kWh。
        consumption = parse_consumption([
            {"start_utc": "2026-09-30T16:00:00Z", "end_utc": "2026-09-30T23:30:00Z",
             "meter_kwh": "0"},
            {"start_utc": "2026-09-30T23:30:00Z", "end_utc": "2026-10-01T00:30:00Z",
             "meter_kwh": "60"},
            {"start_utc": "2026-10-01T00:30:00Z", "end_utc": "2026-10-01T16:00:00Z",
             "meter_kwh": "0"},
        ])
        result = price_bill(rule, dt.date(2026, 10, 1), consumption)
        priced = {(line["kind"], line["overlap_minutes"], line["amount_cny"])
                  for line in result["lines"] if Decimal(line["allocated_kwh"]) > 0}
        self.assertEqual(priced, {("flat", 30, "21.00"), ("peak", 30, "39.00")})
        self.assertEqual(result["total_amount_cny"], "60.00")

    def test_consumption_outside_rule_day_rejected(self) -> None:
        import datetime as dt
        rule = TariffRule.from_dict(simple_rule())
        consumption = parse_consumption([
            {"start_utc": "2026-10-01T16:00:00Z", "end_utc": "2026-10-02T16:00:00Z",
             "meter_kwh": "2400"},
        ])
        with self.assertRaises(ValidationFailed):
            price_bill(rule, dt.date(2026, 10, 1), consumption)

    def test_overlapping_consumption_rejected(self) -> None:
        with self.assertRaises(ValidationFailed):
            parse_consumption([
                {"start_utc": "2026-10-01T00:00:00Z", "end_utc": "2026-10-01T02:00:00Z",
                 "meter_kwh": "10"},
                {"start_utc": "2026-10-01T01:00:00Z", "end_utc": "2026-10-01T03:00:00Z",
                 "meter_kwh": "10"},
            ])


class LifecycleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for uid, role in (("plan", "planner"), ("disp", "dispatcher"),
                          ("risk", "risk"), ("aud", "auditor")):
            self.service.create_user(uid, uid, role)
        self.service.create_contract("plan", {
            "contract_id": "c-1", "counterparty": "某工厂", "signed_on": "2026-09-24",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def _activate(self, rule: dict[str, object], frm: str = "2026-10-01") -> int:
        draft = self.service.create_rule_draft("plan", "c-1", {**rule, "effective_from": frm})
        vid = draft["version_id"]
        self.service.submit_rule_for_review("plan", vid)
        self.service.review_rule("risk", vid, True)
        self.service.activate_rule("risk", vid)
        return vid

    def test_full_draft_review_activate_flow_and_role_separation(self) -> None:
        draft = self.service.create_rule_draft("plan", "c-1", simple_rule(effective_from="2026-10-01"))
        vid = draft["version_id"]
        self.assertEqual(draft["status"], "draft")
        with self.assertRaises(Forbidden):
            self.service.review_rule("plan", vid, True)
        with self.assertRaises(InvalidState):
            self.service.activate_rule("risk", vid)
        self.service.submit_rule_for_review("plan", vid)
        self.service.review_rule("risk", vid, True)
        active = self.service.activate_rule("risk", vid)
        self.assertEqual(active["status"], "active")

    def test_same_day_revision_is_new_version_never_overwrite(self) -> None:
        v1 = self._activate(simple_rule("v1"))
        # 同一天（冻结时钟 2026-09-24）再次修订：产生独立版本行，旧版本退役后新版本才能生效
        v2_draft = self.service.create_rule_draft(
            "plan", "c-1", {**simple_rule("v2", periods=[
                {"kind": "valley", "day_type": "all", "start": "00:00", "end": "24:00",
                 "price_cny": "0.45"}]), "effective_from": "2026-10-02"})
        # 生效区间与 v1 重叠 -> 拒绝
        self.service.submit_rule_for_review("plan", v2_draft["version_id"])
        self.service.review_rule("risk", v2_draft["version_id"], True)
        with self.assertRaises(Conflict):
            self.service.activate_rule("risk", v2_draft["version_id"])
        self.service.retire_rule("risk", v1, "2026-10-02")
        self.service.activate_rule("risk", v2_draft["version_id"])
        versions = self.service.list_rule_versions("c-1")
        self.assertEqual([v["revision_no"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["status"], "retired")
        self.assertNotEqual(versions[0]["content_sha256"], versions[1]["content_sha256"])

    def test_identical_draft_rejected(self) -> None:
        self.service.create_rule_draft("plan", "c-1", simple_rule(effective_from="2026-10-01"))
        with self.assertRaises(Conflict):
            self.service.create_rule_draft("plan", "c-1", simple_rule(effective_from="2026-10-02"))

    def test_preview_does_not_block_later_changes(self) -> None:
        vid = self._activate(simple_rule())
        consumption = [{"start_utc": "2026-09-30T16:00:00Z", "end_utc": "2026-10-01T16:00:00Z",
                        "meter_kwh": "2400"}]
        preview = self.service.preview_bill("disp", vid, "2026-10-01", consumption)
        self.assertEqual(preview["state"], "preview")
        self.assertEqual(preview["total_amount_cny"], "2280.00")
        # 规则仍可退役（预览不产生正式约束）
        self.service.retire_rule("risk", vid, "2026-10-05")

    def test_pinned_contract_keeps_old_rule_and_settlement_is_immutable_replayable(self) -> None:
        v1 = self._activate(simple_rule("v1"))
        self.service.pin_contract_rule("plan", "c-1", v1)
        with self.assertRaises(Conflict):
            self.service.pin_contract_rule("plan", "c-1", v1)
        consumption = [{"start_utc": "2026-09-30T16:00:00Z", "end_utc": "2026-10-01T16:00:00Z",
                        "meter_kwh": "2400"}]
        first = self.service.settle_bill("disp", "c-1", "2026-10-01", consumption, "key-1")
        self.assertFalse(first["replayed"])
        self.assertEqual(first["rule_version_id"], v1)
        # 完整快照与输入
        self.assertEqual(first["rule_snapshot"]["version_tag"], "v1")
        self.assertEqual(first["input"][0]["meter_kwh"], "2400")
        self.assertEqual(len(first["rule_sha256"]), 64)

        # 新版本次月生效，合同仍按旧版本
        v2_rule = simple_rule("v2", periods=[
            {"kind": "valley", "day_type": "all", "start": "00:00", "end": "24:00",
             "price_cny": "0.45"}])
        self.service.retire_rule("risk", v1, "2026-10-02")
        v2 = self._activate(v2_rule, frm="2026-10-02")
        second = self.service.settle_bill("disp", "c-1", "2026-10-01", consumption)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["bill_id"], first["bill_id"])
        self.assertEqual(second["total_amount_cny"], "2280.00")
        self.assertEqual(second["rule_version_id"], v1)

        # 带幂等键的重复结算也返回同一账单，即使输入写法不同（等价 Decimal）
        again = self.service.settle_bill(
            "disp", "c-1", "2026-10-01",
            [{"start_utc": "2026-09-30T16:00:00Z", "end_utc": "2026-10-01T16:00:00Z",
              "meter_kwh": "2400.0"}], "key-1")
        self.assertEqual(again["bill_id"], first["bill_id"])

        # 正式账单不可覆盖、删除
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE tariff_bills SET total_amount_cny='0.01' WHERE bill_id=?",
                (first["bill_id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM tariff_bills WHERE bill_id=?", (first["bill_id"],))

    def test_idempotency_key_cross_payload_conflict(self) -> None:
        v1 = self._activate(simple_rule())
        self.service.pin_contract_rule("plan", "c-1", v1)
        consumption = [{"start_utc": "2026-09-30T16:00:00Z", "end_utc": "2026-10-01T16:00:00Z",
                        "meter_kwh": "2400"}]
        self.service.settle_bill("disp", "c-1", "2026-10-01", consumption, "key-x")
        other = [{"start_utc": "2026-10-01T16:00:00Z", "end_utc": "2026-10-02T16:00:00Z",
                  "meter_kwh": "2400"}]
        # 不同指纹 + 不同日期；同日不同输入且复用键 -> 指纹查不到，插入时唯一键冲突
        with self.assertRaises(Conflict):
            self.service.settle_bill("disp", "c-1", "2026-10-01", [
                {"start_utc": "2026-09-30T16:00:00Z", "end_utc": "2026-10-01T16:00:00Z",
                 "meter_kwh": "100"}], "key-x")

    def test_retroactive_correction_creates_trackable_recalc_suggestion_only(self) -> None:
        v1 = self._activate(simple_rule("v1"), frm="2026-09-01")
        self.service.pin_contract_rule("plan", "c-1", v1)
        consumption = [{"start_utc": "2026-09-30T16:00:00Z", "end_utc": "2026-10-01T16:00:00Z",
                        "meter_kwh": "2400"}]
        bill = self.service.settle_bill("disp", "c-1", "2026-10-01", consumption)
        self.assertEqual(bill["total_amount_cny"], "2280.00")

        # 更正版：峰段价格应为 1.20（追溯生效到 2026-10-01）
        corrected = simple_rule("v1-corrected", periods=[
            {"kind": "valley", "day_type": "all", "start": "00:00", "end": "06:00", "price_cny": "0.30"},
            {"kind": "flat", "day_type": "all", "start": "06:00", "end": "08:00", "price_cny": "0.70"},
            {"kind": "peak", "day_type": "all", "start": "08:00", "end": "22:00",
             "price_cny": "1.00", "holiday_add_cny": "0.20"},
            {"kind": "flat", "day_type": "all", "start": "22:00", "end": "24:00", "price_cny": "0.70"},
        ])
        draft = self.service.create_rule_draft("plan", "c-1", {**corrected, "effective_from": "2026-10-01"})
        self.service.submit_rule_for_review("plan", draft["version_id"])
        self.service.review_rule("risk", draft["version_id"], True)
        # 旧版本仍在 10-01 生效，直接激活会因区间重叠被拒绝
        with self.assertRaises(Conflict):
            self.service.activate_rule("risk", draft["version_id"])
        # 运营更正：旧版本退役到 10-01（半开区间，10-01 当天起由更正版覆盖）
        self.service.retire_rule("risk", v1, "2026-10-01")
        activated = self.service.activate_rule("risk", draft["version_id"])
        self.assertEqual(len(activated["recalc_suggestions"]), 1)

        suggestions = self.service.list_recalc_suggestions("risk", "c-1")
        self.assertEqual(len(suggestions), 1)
        sug = suggestions[0]
        self.assertEqual(sug["status"], "open")
        affected = sug["affected_bills"]
        self.assertEqual(affected[0]["bill_id"], bill["bill_id"])
        # 峰段 1400 kWh 每度少 0.10（节假日 1.40 vs 1.30）→ 新账单应为 2140.00，差额 -140
        self.assertEqual(affected[0]["new_total_cny"], "2140.00")
        self.assertEqual(affected[0]["delta_cny"], "-140.00")
        self.assertEqual(sug["summary"]["delta_cny"], "-140.00")

        # 原账单保持不变
        self.assertEqual(self.service.bill(bill["bill_id"])["total_amount_cny"], "2280.00")
        decided = self.service.decide_recalc_suggestion("risk", sug["suggestion_id"], False, "本期不退补")
        self.assertEqual(decided["status"], "dismissed")
        with self.assertRaises(InvalidState):
            self.service.decide_recalc_suggestion("risk", sug["suggestion_id"], True)

    def test_auditor_cannot_settle_or_change_rules(self) -> None:
        draft = self.service.create_rule_draft("plan", "c-1", simple_rule(effective_from="2026-10-01"))
        with self.assertRaises(Forbidden):
            self.service.submit_rule_for_review("aud", draft["version_id"])
        with self.assertRaises(Forbidden):
            self.service.preview_bill("aud", draft["version_id"], "2026-10-01", [
                {"start_utc": "2026-09-30T16:00:00Z", "end_utc": "2026-10-01T16:00:00Z",
                 "meter_kwh": "1"}])

    def test_settle_requires_pinned_contract(self) -> None:
        vid = self._activate(simple_rule())
        with self.assertRaises(InvalidState):
            self.service.settle_bill("disp", "c-1", "2026-10-01", [
                {"start_utc": "2026-09-30T16:00:00Z", "end_utc": "2026-10-01T16:00:00Z",
                 "meter_kwh": "1"}])

    def test_audit_chain_records_lifecycle(self) -> None:
        vid = self._activate(simple_rule())
        self.service.pin_contract_rule("plan", "c-1", vid)
        chain = self.service.audit_chain("aud")
        self.assertTrue(chain["valid"])
        types = {row[0] for row in self.connection.execute(
            "SELECT DISTINCT event_type FROM supply_audit_events")}
        self.assertIn("rule.drafted", types)
        self.assertIn("rule.activated", types)
        self.assertIn("contract.rule_pinned", types)


class TariffApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        service = SupplyService(
            self.connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
        self.app = JsonApplication(service)
        for uid, role in (("plan", "planner"), ("disp", "dispatcher"),
                          ("risk", "risk"), ("aud", "auditor")):
            service.create_user(uid, uid, role)

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, actor: str, payload: dict[str, object]):
        return self.app.handle("POST", path, {"X-Actor-Id": actor},
                               json.dumps(payload).encode())

    def _activate_over_http(self) -> tuple[int, int]:
        contract = self._post("/tariff/contracts", "plan", {
            "contract_id": "c-1", "counterparty": "工厂", "signed_on": "2026-09-24"})
        self.assertEqual(contract.status, 201)
        draft = self._post("/tariff/contracts/c-1/rules", "plan",
                           {**simple_rule(), "effective_from": "2026-10-01"})
        vid = draft.body["version_id"]
        self.assertEqual(self._post(f"/tariff/rules/{vid}/submit", "plan", {}).status, 200)
        review = self._post(f"/tariff/rules/{vid}/review", "risk", {"approved": True})
        self.assertEqual(review.status, 200)
        self.assertEqual(self._post(f"/tariff/rules/{vid}/activate", "risk", {}).status, 200)
        return vid, 1

    def test_lifecycle_preview_settle_replay_over_http(self) -> None:
        vid, _ = self._activate_over_http()
        self.assertEqual(self._post("/tariff/contracts/c-1/pin", "plan",
                                    {"version_id": vid}).status, 200)
        consumption = [{"start_utc": "2026-09-30T16:00:00Z",
                        "end_utc": "2026-10-01T16:00:00Z", "meter_kwh": "2400"}]
        preview = self._post(f"/tariff/rules/{vid}/preview", "disp",
                             {"settlement_date": "2026-10-01", "consumption": consumption})
        self.assertEqual(preview.status, 200)
        self.assertEqual(preview.body["total_amount_cny"], "2280.00")
        settle = self._post("/tariff/contracts/c-1/settle", "disp", {
            "settlement_date": "2026-10-01", "consumption": consumption,
            "idempotency_key": "http-key-1"})
        self.assertEqual(settle.status, 200)
        self.assertFalse(settle.body["replayed"])
        replay = self._post("/tariff/contracts/c-1/settle", "disp", {
            "settlement_date": "2026-10-01", "consumption": consumption,
            "idempotency_key": "http-key-1"})
        self.assertTrue(replay.body["replayed"])
        self.assertEqual(replay.body["bill_id"], settle.body["bill_id"])
        bills = self.app.handle("GET", "/tariff/contracts/c-1/bills?state=official",
                                {"X-Actor-Id": "aud"})
        self.assertEqual(len(bills.body["bills"]), 1)


if __name__ == "__main__":
    unittest.main()
