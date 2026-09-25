"""贯通电价、送出线路、燃料库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
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

    # 分时电价：旧合同锁定签订时规则；起草-复核-生效后发布前预览，再正式结算并复放。
    tariff = _tariff_workflow(service)

    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "tariff": tariff, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def _tariff_workflow(service: SupplyService) -> dict[str, object]:
    rule_v1 = {
        "timezone": "Asia/Shanghai",
        "version_tag": "peak-valley-2026-10",
        "holidays": ["2026-10-01"],
        "periods": [
            {"kind": "valley", "day_type": "all", "start": "22:00", "end": "06:00", "price_cny": "0.30"},
            {"kind": "flat", "day_type": "all", "start": "06:00", "end": "08:00", "price_cny": "0.70"},
            {"kind": "peak", "day_type": "all", "start": "08:00", "end": "22:00",
             "price_cny": "1.10", "holiday_add_cny": "0.20"},
        ],
    }
    service.create_contract("plan", {"contract_id": "pp-1001", "counterparty": "沿海终端", "signed_on": "2026-09-24"})
    draft = service.create_rule_draft("plan", "pp-1001", {**rule_v1, "effective_from": "2026-10-01"})
    version_id = draft["version_id"]
    service.submit_rule_for_review("plan", version_id, "下一结算月峰平谷与节假日加价")
    service.review_rule("risk", version_id, True, "时段覆盖完整，准予生效")
    service.activate_rule("risk", version_id)
    service.pin_contract_rule("plan", "pp-1001", version_id)

    # 规则日 2026-10-01（节假日）按上海本地时间覆盖全天；
    # 谷段 22:00-06:00 跨午夜，UTC 窗口为 09-30T14:00Z 至 10-01T14:00Z。
    day_consumption = [
        {"start_utc": "2026-09-30T14:00:00Z", "end_utc": "2026-10-01T14:00:00Z", "meter_kwh": "2400"},
    ]
    preview = service.preview_bill("dispatch", version_id, "2026-10-01", day_consumption)
    first = service.settle_bill("dispatch", "pp-1001", "2026-10-01", day_consumption, "settle-1001")
    replay = service.settle_bill("dispatch", "pp-1001", "2026-10-01", day_consumption, "settle-1001")
    assert replay["bill_id"] == first["bill_id"] and replay["replayed"]

    # 后续规则更正（峰段电价下修）追溯生效，只能产生可追踪重算建议。
    corrected = {
        **rule_v1,
        "version_tag": "peak-valley-2026-10-corrected",
        "periods": [
            {"kind": "valley", "day_type": "all", "start": "22:00", "end": "06:00", "price_cny": "0.30"},
            {"kind": "flat", "day_type": "all", "start": "06:00", "end": "08:00", "price_cny": "0.70"},
            {"kind": "peak", "day_type": "all", "start": "08:00", "end": "22:00",
             "price_cny": "1.00", "holiday_add_cny": "0.20"},
        ],
    }
    fix_draft = service.create_rule_draft("plan", "pp-1001", {**corrected, "effective_from": "2026-10-01"})
    service.submit_rule_for_review("plan", fix_draft["version_id"], "峰段价格更正")
    service.review_rule("risk", fix_draft["version_id"], True)
    service.retire_rule("risk", version_id, "2026-10-01", "由更正版本接管")
    activated = service.activate_rule("risk", fix_draft["version_id"])
    suggestions = service.list_recalc_suggestions("risk", "pp-1001")
    assert activated["recalc_suggestions"] and len(suggestions) == 1
    assert service.bill(first["bill_id"])["total_amount_cny"] == first["total_amount_cny"]

    return {
        "contract_id": "pp-1001",
        "pinned_rule_version_id": version_id,
        "preview_amount_cny": preview["total_amount_cny"],
        "settled_amount_cny": first["total_amount_cny"],
        "bill_id": first["bill_id"],
        "recalc_suggestion": suggestions[0]["suggestion_id"],
        "recalc_delta_cny": suggestions[0]["summary"]["delta_cny"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行电厂调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
