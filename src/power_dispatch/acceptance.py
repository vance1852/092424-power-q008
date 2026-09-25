"""贯通电价、送出线路、燃料库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("risk2", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部电厂", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "PEAK_VALLEY", "quantity_mwh": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    tariff_payload = {
        "rule_id": "tou-2026-10",
        "series": "tou-standard",
        "timezone": "Asia/Shanghai",
        "effective_from": "2026-10-01",
        "effective_to": "2026-10-31",
        "definition": {
            "periods": [
                {"kind": "valley", "start": "23:00", "end": "07:00", "price_cny_per_mwh": "60"},
                {"kind": "flat", "start": "07:00", "end": "08:00", "price_cny_per_mwh": "100"},
                {"kind": "peak", "start": "08:00", "end": "12:00", "price_cny_per_mwh": "150"},
                {"kind": "flat", "start": "12:00", "end": "17:00", "price_cny_per_mwh": "100"},
                {"kind": "peak", "start": "17:00", "end": "21:00", "price_cny_per_mwh": "150"},
                {"kind": "flat", "start": "21:00", "end": "23:00", "price_cny_per_mwh": "100"},
            ],
            "holiday_surcharge_percent": "20",
            "holidays": ["2026-10-01", "2026-10-02", "2026-10-03"],
        },
    }
    service.create_tariff_rule("plan", tariff_payload)
    service.review_tariff_rule("risk", "tou-2026-10", 1)
    service.publish_tariff_rule("risk2", "tou-2026-10", 2)
    preview = service.preview_tariff("tou-2026-10", "2026-10-01")
    service.create_contract("plan", {"contract_id": "ppa-2026-10", "counterparty": "华东售电公司", "series": "tou-standard", "service_start": "2026-10-01", "service_end": "2026-10-31"})
    day_start = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)
    readings = [
        {
            "start_utc": (day_start + timedelta(hours=hour)).isoformat().replace("+00:00", "Z"),
            "end_utc": (day_start + timedelta(hours=hour + 1)).isoformat().replace("+00:00", "Z"),
            "mwh": "2",
        }
        for hour in range(24)
    ]
    settlement_payload = {"settlement_id": "set-2026-10-01", "contract_id": "ppa-2026-10", "period_start": "2026-10-01", "period_end": "2026-10-01", "idempotency_key": "set-key-001", "readings": readings}
    bill = service.settle_contract("dispatch", settlement_payload)
    replay = service.settle_contract("dispatch", settlement_payload)
    correction = dict(tariff_payload, rule_id="tou-2026-10-r2")
    correction["definition"] = dict(tariff_payload["definition"], periods=[
        dict(period, price_cny_per_mwh="155") if period["kind"] == "peak" else dict(period)
        for period in tariff_payload["definition"]["periods"]
    ])
    service.create_tariff_rule("plan", correction)
    service.review_tariff_rule("risk", "tou-2026-10-r2", 1)
    revision = service.publish_tariff_rule("risk2", "tou-2026-10-r2", 2)
    suggestions = service.recalculations("set-2026-10-01")
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "tariff": {"preview": preview, "bill": bill, "replay": replay, "revision": revision, "recalculations": suggestions}, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行电厂调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
