"""电价、燃料库存、送出线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    Route,
    SupplyScenario,
    date_text,
    identifier,
    required_text,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction
from .tariff import (
    ConsumptionSlice,
    TariffRule,
    bill_fingerprint,
    parse_consumption,
    price_bill,
)


ROLE_PERMISSIONS = {
    "planner": {
        "quote.write", "catalog.write", "scenario.write", "scenario.run",
        "tariff.contract.write", "tariff.rule.write", "tariff.preview",
    },
    "dispatcher": {
        "nomination.write", "allocation.run", "transfer.write", "inventory.write",
        "tariff.preview", "settlement.run", "bill.read",
    },
    "risk": {
        "outage.write", "scenario.approve", "report.read",
        "tariff.contract.retire", "tariff.rule.review", "tariff.rule.activate",
        "tariff.rule.retire", "tariff.preview", "bill.read",
        "tariff.recalc.read", "tariff.recalc.handle",
    },
    "auditor": {
        "report.read", "audit.read", "bill.read", "tariff.recalc.read",
    },
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("电价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准电价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_mwh,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_mwh),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("送出线路编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("送出线路不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_mwh,available_mwh,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("燃料批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("燃料批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("送出线路当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_mwh,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_mwh),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("送出线路不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_mwh"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_mwh"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_mwh=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_mwh"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可送电版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("燃料批次不存在")
        allocated = Decimal(nomination["allocated_mwh"])
        available = Decimal(lot["available_mwh"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("燃料批次与送出线路起点或电源类型不匹配")
        if available < allocated:
            raise Conflict("燃料库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_mwh=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_mwh,"
                "expected_delivered_mwh,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_mwh": decimal_text(allocated),
            "expected_delivered_mwh": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用电价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_mwh AS REAL)) available_mwh "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    # ------------------------------------------------------------------
    # 电价合同与分时规则生命周期
    # ------------------------------------------------------------------

    def create_contract(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "tariff.contract.write")
        contract_id = identifier(raw.get("contract_id"), "contract_id")
        counterparty = required_text(raw.get("counterparty"), "counterparty")
        signed_on = date_text(raw.get("signed_on"), "signed_on")
        note = str(raw.get("note", "") or "")
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO tariff_contracts(contract_id,counterparty,signed_on,note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (contract_id, counterparty, signed_on, note, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("合同编号已经存在") from exc
            self._audit("tariff_contract", contract_id, "contract.created", actor_id, {
                "counterparty": counterparty,
                "signed_on": signed_on,
            })
        return self.contract(contract_id)

    def pin_contract_rule(self, actor_id: str, contract_id: str, version_id: object) -> dict[str, Any]:
        """把签订时适用的规则版本锁定到合同；锁定后数据库触发器禁止更改。"""

        self._require(actor_id, "tariff.contract.write")
        if isinstance(version_id, bool) or not isinstance(version_id, int):
            raise ValidationFailed("version_id 必须是整数版本号")
        with transaction(self.connection, immediate=True):
            contract = self.connection.execute(
                "SELECT * FROM tariff_contracts WHERE contract_id=?", (contract_id,)
            ).fetchone()
            if contract is None:
                raise NotFound("合同不存在")
            if contract["pricing_version_id"] is not None:
                raise Conflict("合同已经锁定计费规则版本，不能重复锁定")
            row = self.connection.execute(
                "SELECT * FROM tariff_rule_versions WHERE version_id=? AND contract_id=?",
                (version_id, contract_id),
            ).fetchone()
            if row is None:
                raise NotFound("规则版本不存在或不属于该合同")
            if row["status"] not in ("active", "retired"):
                raise InvalidState("只有已生效（含已退役）版本可以锁定到合同")
            try:
                self.connection.execute(
                    "UPDATE tariff_contracts SET pricing_version_id=?,revision=revision+1 "
                    "WHERE contract_id=? AND pricing_version_id IS NULL",
                    (version_id, contract_id),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("合同计费规则版本锁定冲突") from exc
            self._audit("tariff_contract", contract_id, "contract.rule_pinned", actor_id, {
                "version_id": version_id,
                "signed_on": contract["signed_on"],
            })
        return self.contract(contract_id)

    def contract(self, contract_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM tariff_contracts WHERE contract_id=?", (contract_id,)
        ).fetchone()
        if row is None:
            raise NotFound("合同不存在")
        result = dict(row)
        pinned = self.connection.execute(
            "SELECT v.version_id,v.revision_no,v.status,v.effective_from,v.effective_to,v.content_sha256 "
            "FROM tariff_contracts c JOIN tariff_rule_versions v ON v.version_id=c.pricing_version_id "
            "WHERE c.contract_id=?",
            (contract_id,),
        ).fetchone()
        result["pricing_rule"] = None if pinned is None else dict(pinned)
        return result

    def _rule_version_row(self, version_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM tariff_rule_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("规则版本不存在")
        return row

    def _latest_version_row(self, contract_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM tariff_rule_versions WHERE contract_id=? ORDER BY revision_no DESC LIMIT 1",
            (contract_id,),
        ).fetchone()

    def create_rule_draft(self, actor_id: str, contract_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """起草新规则版本（草稿）。同日多次修订产生新版本行，绝不覆盖旧版本。"""

        self._require(actor_id, "tariff.rule.write")
        contract = self.connection.execute(
            "SELECT * FROM tariff_contracts WHERE contract_id=?", (contract_id,)
        ).fetchone()
        if contract is None:
            raise NotFound("合同不存在")
        rule = TariffRule.from_dict(dict(raw))
        definition = rule.canonical_text()
        sha = rule.content_sha256()
        effective_from = date_text(raw.get("effective_from"), "effective_from")
        effective_to = raw.get("effective_to")
        if effective_to is not None:
            effective_to = date_text(effective_to, "effective_to")
            if effective_to <= effective_from:
                raise ValidationFailed("effective_to 必须晚于 effective_from")
        with transaction(self.connection, immediate=True):
            latest = self._latest_version_row(contract_id)
            revision_no = 1 if latest is None else int(latest["revision_no"]) + 1
            duplicate = self.connection.execute(
                "SELECT version_id FROM tariff_rule_versions WHERE contract_id=? AND content_sha256=?",
                (contract_id, sha),
            ).fetchone()
            if duplicate is not None:
                raise Conflict("规则内容与已有版本完全相同，无需重复起草")
            cursor = self.connection.execute(
                "INSERT INTO tariff_rule_versions(contract_id,revision_no,status,definition_json,content_sha256,"
                "effective_from,effective_to,supersedes_version_id,created_by,created_at) "
                "VALUES(?,?, 'draft', ?,?,?,?,?,?,?)",
                (
                    contract_id,
                    revision_no,
                    definition,
                    sha,
                    effective_from,
                    effective_to,
                    None if latest is None else int(latest["version_id"]),
                    actor_id,
                    self._now(),
                ),
            )
            version_id = int(cursor.lastrowid)
            self._audit("tariff_rule", str(version_id), "rule.drafted", actor_id, {
                "contract_id": contract_id,
                "revision_no": revision_no,
                "sha256": sha,
                "effective_from": effective_from,
            })
        return self.rule_version(version_id)

    def submit_rule_for_review(self, actor_id: str, version_id: int, note: str = "") -> dict[str, Any]:
        self._require(actor_id, "tariff.rule.write")
        with transaction(self.connection, immediate=True):
            row = self._rule_version_row(version_id)
            if row["status"] != "draft":
                raise InvalidState("只有草稿可以提交复核")
            self.connection.execute(
                "UPDATE tariff_rule_versions SET status='in_review',submitted_at=?,review_note=? "
                "WHERE version_id=? AND status='draft'",
                (self._now(), note, version_id),
            )
            self._audit("tariff_rule", str(version_id), "rule.submitted", actor_id, {"note": note})
        return self.rule_version(version_id)

    def review_rule(self, actor_id: str, version_id: int, approved: bool, note: str = "") -> dict[str, Any]:
        self._require(actor_id, "tariff.rule.review")
        target_status = "approved" if approved else "rejected"
        with transaction(self.connection, immediate=True):
            row = self._rule_version_row(version_id)
            if row["status"] != "in_review":
                raise InvalidState("只有复核中的版本可以给出复核结论")
            self.connection.execute(
                "UPDATE tariff_rule_versions SET status=?,reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE version_id=?",
                (target_status, actor_id, self._now(), note, version_id),
            )
            self._audit("tariff_rule", str(version_id), "rule.reviewed", actor_id, {
                "decision": target_status,
                "note": note,
            })
        return self.rule_version(version_id)

    def _assert_no_active_overlap(self, contract_id: str, effective_from: str,
                                 effective_to: str | None, exclude_version: int | None = None) -> None:
        rows = self.connection.execute(
            "SELECT version_id,revision_no,effective_from,effective_to FROM tariff_rule_versions "
            "WHERE contract_id=? AND status='active'"
            + (" AND version_id<>?" if exclude_version is not None else ""),
            (contract_id, *((exclude_version,) if exclude_version is not None else ())),
        ).fetchall()
        for row in rows:
            other_from = row["effective_from"]
            other_to = row["effective_to"]
            left = max(effective_from, other_from)
            right = min(effective_to or "9999-12-31", other_to or "9999-12-31")
            if left < right:
                raise Conflict(
                    f"生效区间与已生效版本 r{row['revision_no']}（{other_from} 起）重叠；"
                    "同日修订请先将旧版本退役到生效日之前"
                )

    def activate_rule(self, actor_id: str, version_id: int) -> dict[str, Any]:
        """复核通过后正式生效；与其他生效区间重叠（含同日生效）将被拒绝。"""

        self._require(actor_id, "tariff.rule.activate")
        with transaction(self.connection, immediate=True):
            row = self._rule_version_row(version_id)
            if row["status"] != "approved":
                raise InvalidState("只有复核通过的版本可以生效")
            self._assert_no_active_overlap(
                row["contract_id"], row["effective_from"], row["effective_to"]
            )
            self.connection.execute(
                "UPDATE tariff_rule_versions SET status='active',activated_at=? WHERE version_id=?",
                (self._now(), version_id),
            )
            self._audit("tariff_rule", str(version_id), "rule.activated", actor_id, {
                "contract_id": row["contract_id"],
                "effective_from": row["effective_from"],
                "effective_to": row["effective_to"],
            })
            suggestions = self._create_recalc_suggestions(actor_id, row)
        result = self.rule_version(version_id)
        result["recalc_suggestions"] = suggestions
        return result

    def retire_rule(self, actor_id: str, version_id: int, effective_to: str | None = None,
                    note: str = "") -> dict[str, Any]:
        self._require(actor_id, "tariff.rule.retire")
        with transaction(self.connection, immediate=True):
            row = self._rule_version_row(version_id)
            if row["status"] != "active":
                raise InvalidState("只有生效中的版本可以退役")
            end = effective_to or self.clock.now().date().isoformat()
            end = date_text(end, "effective_to")
            if end < row["effective_from"]:
                raise ValidationFailed("退役日期不能早于版本生效日期")
            self.connection.execute(
                "UPDATE tariff_rule_versions SET status='retired',effective_to=?,review_note=? "
                "WHERE version_id=?",
                (end, note, version_id),
            )
            self._audit("tariff_rule", str(version_id), "rule.retired", actor_id, {
                "effective_to": end,
                "note": note,
            })
        return self.rule_version(version_id)

    def rule_version(self, version_id: int) -> dict[str, Any]:
        row = self._rule_version_row(version_id)
        result = {
            "version_id": row["version_id"],
            "contract_id": row["contract_id"],
            "revision_no": row["revision_no"],
            "status": row["status"],
            "definition": json.loads(row["definition_json"]),
            "content_sha256": row["content_sha256"],
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
            "supersedes_version_id": row["supersedes_version_id"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
        }
        return result

    def list_rule_versions(self, contract_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT version_id FROM tariff_rule_versions WHERE contract_id=? ORDER BY revision_no",
            (contract_id,),
        ).fetchall()
        return [self.rule_version(int(row["version_id"])) for row in rows]

    # ------------------------------------------------------------------
    # 预览、正式结算与重算建议
    # ------------------------------------------------------------------

    @staticmethod
    def _rule_from_row(row: sqlite3.Row) -> TariffRule:
        return TariffRule.from_dict(json.loads(row["definition_json"]))

    def _compute_version_bill(self, row: sqlite3.Row, settlement_date: str,
                              consumption_raw: object) -> tuple[dict[str, Any], tuple, str]:
        day = date.fromisoformat(date_text(settlement_date, "settlement_date"))
        consumption = parse_consumption(consumption_raw)
        rule = self._rule_from_row(row)
        result = price_bill(rule, day, consumption)
        fingerprint = bill_fingerprint(row["content_sha256"], day.isoformat(), consumption)
        return result, consumption, fingerprint

    @staticmethod
    def _normalized_input(consumption: tuple) -> list[dict[str, str]]:
        return [
            {
                "start_utc": item.start_utc,
                "end_utc": item.end_utc,
                "meter_kwh": format(item.meter_kwh, "f"),
            }
            for item in consumption
        ]

    def preview_bill(self, actor_id: str, version_id: int, settlement_date: str,
                     consumption: object) -> dict[str, Any]:
        """发布前预览：任意状态的规则版本都可试算，结果只保存为 preview，不影响正式账单。"""

        self._require(actor_id, "tariff.preview")
        row = self._rule_version_row(version_id)
        result, parsed, fingerprint = self._compute_version_bill(row, settlement_date, consumption)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO tariff_bills(contract_id,rule_version_id,rule_sha256,settlement_date,state,"
                "input_fingerprint,input_json,rule_snapshot_json,result_json,total_kwh,total_amount_cny,"
                "created_by,created_at) "
                "VALUES(?,?,?,?, 'preview', ?,?,?,?,?,?,?,?)",
                (
                    row["contract_id"],
                    version_id,
                    row["content_sha256"],
                    result["settlement_date"],
                    fingerprint,
                    canonical_json(self._normalized_input(parsed)),
                    row["definition_json"],
                    canonical_json(result),
                    result["total_kwh"],
                    result["total_amount_cny"],
                    actor_id,
                    self._now(),
                ),
            )
            bill_id = int(cursor.lastrowid)
            self._audit("tariff_bill", str(bill_id), "bill.previewed", actor_id, {
                "contract_id": row["contract_id"],
                "rule_version_id": version_id,
                "settlement_date": result["settlement_date"],
            })
        return {"bill_id": bill_id, "state": "preview", **result}

    def settle_bill(self, actor_id: str, contract_id: str, settlement_date: str,
                    consumption: object, idempotency_key: str | None = None) -> dict[str, Any]:
        """正式结算：始终使用合同签订时绑定的规则版本。

        相同（规则快照, 日期, 输入电量）重复结算直接返回原账单；
        正式账单在数据库层禁止 UPDATE/DELETE。
        """

        self._require(actor_id, "settlement.run")
        contract = self.connection.execute(
            "SELECT * FROM tariff_contracts WHERE contract_id=?", (contract_id,)
        ).fetchone()
        if contract is None:
            raise NotFound("合同不存在")
        if contract["pricing_version_id"] is None:
            raise InvalidState("合同尚未锁定签订时的计费规则版本，不能正式结算")
        version_id = int(contract["pricing_version_id"])
        if idempotency_key is not None:
            idempotency_key = identifier(idempotency_key, "idempotency_key")
            keyed = self.connection.execute(
                "SELECT bill_id,input_fingerprint FROM tariff_bills WHERE state='official' AND idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if keyed is not None:
                # 指纹在拿到键对应账单前无法计算，先查出版本再比对：
                # 键相同但（规则快照/日期/输入）不同属于调用方冲突。
                row = self._rule_version_row(version_id)
                _, _, fingerprint = self._compute_version_bill(row, settlement_date, consumption)
                if keyed["input_fingerprint"] != fingerprint:
                    raise Conflict("幂等键对应不同的结算内容")
                return self.bill(int(keyed["bill_id"]), replay=True)
        row = self._rule_version_row(version_id)
        result, parsed, fingerprint = self._compute_version_bill(row, settlement_date, consumption)
        existing = self.connection.execute(
            "SELECT bill_id FROM tariff_bills WHERE state='official' AND contract_id=? AND input_fingerprint=?",
            (contract_id, fingerprint),
        ).fetchone()
        if existing is not None:
            return self.bill(int(existing["bill_id"]), replay=True)
        with transaction(self.connection, immediate=True):
            try:
                cursor = self.connection.execute(
                    "INSERT INTO tariff_bills(contract_id,rule_version_id,rule_sha256,settlement_date,state,"
                    "input_fingerprint,input_json,rule_snapshot_json,result_json,total_kwh,total_amount_cny,"
                    "idempotency_key,created_by,created_at) VALUES(?,?,?,?, 'official', ?,?,?,?,?,?,?,?,?)",
                    (
                        contract_id,
                        version_id,
                        row["content_sha256"],
                        result["settlement_date"],
                        fingerprint,
                        canonical_json(self._normalized_input(parsed)),
                        row["definition_json"],
                        canonical_json(result),
                        result["total_kwh"],
                        result["total_amount_cny"],
                        idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("正式结算冲突（幂等键已被其他账单使用）") from exc
            bill_id = int(cursor.lastrowid)
            self._audit("tariff_bill", str(bill_id), "bill.settled", actor_id, {
                "contract_id": contract_id,
                "rule_version_id": version_id,
                "settlement_date": result["settlement_date"],
                "total_amount_cny": result["total_amount_cny"],
                "input_fingerprint": fingerprint,
            })
        return self.bill(bill_id, replay=False)

    def bill(self, bill_id: int, replay: bool = False) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM tariff_bills WHERE bill_id=?", (bill_id,)).fetchone()
        if row is None:
            raise NotFound("账单不存在")
        return {
            "bill_id": row["bill_id"],
            "state": row["state"],
            "replayed": replay,
            "contract_id": row["contract_id"],
            "rule_version_id": row["rule_version_id"],
            "rule_sha256": row["rule_sha256"],
            "settlement_date": row["settlement_date"],
            "input_fingerprint": row["input_fingerprint"],
            "input": json.loads(row["input_json"]),
            "rule_snapshot": json.loads(row["rule_snapshot_json"]),
            "bill": json.loads(row["result_json"]),
            "total_kwh": row["total_kwh"],
            "total_amount_cny": row["total_amount_cny"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def list_bills(self, contract_id: str, *, state: str | None = None) -> list[dict[str, Any]]:
        if state not in (None, "preview", "official"):
            raise ValidationFailed("state 必须是 preview 或 official")
        sql = "SELECT bill_id FROM tariff_bills WHERE contract_id=?"
        params: list[object] = [contract_id]
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY bill_id"
        rows = self.connection.execute(sql, params).fetchall()
        return [self.bill(int(row["bill_id"])) for row in rows]

    def _create_recalc_suggestions(self, actor_id: str, new_row: sqlite3.Row) -> list[dict[str, Any]]:
        """新版本生效后，对与其生效区间重叠的历史正式账单做影子重算。

        只生成可追踪建议，绝不修改或覆盖原账单；典型场景是规则更正追溯到
        已结算日期（新版本生效日早于或等于已出账单日）。
        """

        suggestions: list[dict[str, Any]] = []
        bills = self.connection.execute(
            "SELECT * FROM tariff_bills WHERE state='official' AND contract_id=? "
            "AND rule_version_id<>? AND settlement_date>=? ORDER BY bill_id",
            (new_row["contract_id"], int(new_row["version_id"]), new_row["effective_from"]),
        ).fetchall()
        if not bills:
            return suggestions
        new_rule = self._rule_from_row(new_row)
        affected: list[dict[str, Any]] = []
        total_old = Decimal(0)
        total_new = Decimal(0)
        for bill_row in bills:
            stored_input = json.loads(bill_row["input_json"])
            day = date.fromisoformat(bill_row["settlement_date"])
            consumption = tuple(
                ConsumptionSlice(item["start_utc"], item["end_utc"], Decimal(item["meter_kwh"]))
                for item in stored_input
            )
            new_result = price_bill(new_rule, day, consumption)
            old_amount = Decimal(bill_row["total_amount_cny"])
            new_amount = Decimal(new_result["total_amount_cny"])
            delta = new_amount - old_amount
            total_old += old_amount
            total_new += new_amount
            affected.append({
                "bill_id": bill_row["bill_id"],
                "settlement_date": bill_row["settlement_date"],
                "old_version_id": int(bill_row["rule_version_id"]),
                "new_version_id": int(new_row["version_id"]),
                "old_total_cny": str(old_amount),
                "new_total_cny": str(new_amount),
                "delta_cny": str(delta),
                "projected_result": new_result,
            })
        suggestion_id = f"recalc-{new_row['version_id']}-{digest(affected)[:12]}"
        summary = {
            "affected_bills": len(affected),
            "old_total_cny": str(total_old.quantize(Decimal("0.01"))),
            "new_total_cny": str(total_new.quantize(Decimal("0.01"))),
            "delta_cny": str((total_new - total_old).quantize(Decimal("0.01"))),
        }
        self.connection.execute(
            "INSERT INTO tariff_recalc_suggestions(suggestion_id,contract_id,old_version_id,new_version_id,"
            "range_from,range_to,affected_bills_json,summary_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                suggestion_id,
                new_row["contract_id"],
                int(affected[0]["old_version_id"]),
                int(new_row["version_id"]),
                affected[0]["settlement_date"],
                affected[-1]["settlement_date"],
                canonical_json(affected),
                canonical_json(summary),
                actor_id,
                self._now(),
            ),
        )
        self._audit("tariff_recalc", suggestion_id, "recalc.suggested", actor_id, {
            "contract_id": new_row["contract_id"],
            "new_version_id": int(new_row["version_id"]),
            **summary,
        })
        suggestions.append({"suggestion_id": suggestion_id, **summary})
        return suggestions

    def list_recalc_suggestions(self, actor_id: str, contract_id: str | None = None) -> list[dict[str, Any]]:
        self._require(actor_id, "tariff.recalc.read")
        sql = "SELECT suggestion_id FROM tariff_recalc_suggestions"
        params: list[object] = []
        if contract_id:
            sql += " WHERE contract_id=?"
            params.append(contract_id)
        sql += " ORDER BY suggestion_id"
        rows = self.connection.execute(sql, params).fetchall()
        return [self.recalc_suggestion(row["suggestion_id"]) for row in rows]

    def recalc_suggestion(self, suggestion_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM tariff_recalc_suggestions WHERE suggestion_id=?", (suggestion_id,)
        ).fetchone()
        if row is None:
            raise NotFound("重算建议不存在")
        return {
            "suggestion_id": row["suggestion_id"],
            "contract_id": row["contract_id"],
            "status": row["status"],
            "old_version_id": row["old_version_id"],
            "new_version_id": row["new_version_id"],
            "range_from": row["range_from"],
            "range_to": row["range_to"],
            "summary": json.loads(row["summary_json"]),
            "affected_bills": json.loads(row["affected_bills_json"]),
            "decision_note": row["decision_note"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "created_at": row["created_at"],
        }

    def decide_recalc_suggestion(self, actor_id: str, suggestion_id: str,
                                 accepted: bool, note: str = "") -> dict[str, Any]:
        """采纳或驳回建议。采纳也只留下可追踪记录，绝不覆盖原账单。"""

        self._require(actor_id, "tariff.recalc.handle")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE tariff_recalc_suggestions SET status=?,decision_note=?,decided_by=?,decided_at=? "
                "WHERE suggestion_id=? AND status='open'",
                ("accepted" if accepted else "dismissed", note, actor_id, self._now(), suggestion_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("重算建议不存在或已处理")
            self._audit("tariff_recalc", suggestion_id, "recalc.decided", actor_id, {
                "decision": "accepted" if accepted else "dismissed",
                "note": note,
            })
        return self.recalc_suggestion(suggestion_id)

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
