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
    ContractInput,
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    Route,
    SettlementInput,
    SupplyScenario,
    TariffRuleInput,
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
    quantize_money,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction
from .tariff import ROUNDING, MeterInterval, TariffDefinition, compute_bill, preview_day


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "tariff.write", "contract.write"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write", "settlement.run"},
    "risk": {"outage.write", "scenario.approve", "report.read", "tariff.review", "tariff.publish", "tariff.retire"},
    "auditor": {"report.read", "audit.read"},
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

    def create_tariff_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记规则草稿；内容不可变，修订只能以同系列新版本登记。"""
        self._require(actor_id, "tariff.write")
        rule = TariffRuleInput.from_dict(raw)
        definition_json = canonical_json(rule.definition.as_dict())
        content_sha256 = digest({
            "series": rule.series,
            "timezone": rule.timezone,
            "effective_from": rule.effective_from,
            "effective_to": rule.effective_to,
            "definition": rule.definition.as_dict(),
        })
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS next_version FROM tariff_rules WHERE series=?",
            (rule.series,),
        ).fetchone()
        version = int(row["next_version"])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO tariff_rules(rule_id,series,version,timezone,effective_from,effective_to,"
                    "definition_json,content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        rule.rule_id,
                        rule.series,
                        version,
                        rule.timezone,
                        rule.effective_from,
                        rule.effective_to,
                        definition_json,
                        content_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "tariff_rule",
                    rule.rule_id,
                    "tariff.created",
                    actor_id,
                    {"series": rule.series, "version": version, "sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("电价规则编号、版本或内容冲突") from exc
        return {
            "rule_id": rule.rule_id,
            "series": rule.series,
            "version": version,
            "state": "draft",
            "revision": 1,
            "content_sha256": content_sha256,
        }

    def _tariff_rule_row(self, rule_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM tariff_rules WHERE rule_id=?", (rule_id,)
        ).fetchone()
        if row is None:
            raise NotFound("电价规则不存在")
        return row

    @staticmethod
    def _definition_from_row(row: sqlite3.Row) -> TariffDefinition:
        return TariffDefinition.from_dict(json.loads(row["definition_json"]))

    @staticmethod
    def _tariff_rule_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "rule_id": row["rule_id"],
            "series": row["series"],
            "version": row["version"],
            "state": row["state"],
            "revision": row["revision"],
            "timezone": row["timezone"],
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
            "content_sha256": row["content_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "published_by": row["published_by"],
            "published_at": row["published_at"],
            "retired_by": row["retired_by"],
            "retired_at": row["retired_at"],
        }

    def tariff_rule(self, rule_id: str) -> dict[str, Any]:
        row = self._tariff_rule_row(rule_id)
        return {**self._tariff_rule_summary(row), "definition": json.loads(row["definition_json"])}

    def review_tariff_rule(self, actor_id: str, rule_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "tariff.review")
        row = self._tariff_rule_row(rule_id)
        if row["created_by"] == actor_id:
            raise Forbidden("复核人不能是规则创建人")
        reviewed_at = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE tariff_rules SET state='reviewed',reviewed_by=?,reviewed_at=?,revision=revision+1 "
                "WHERE rule_id=? AND state='draft' AND revision=?",
                (actor_id, reviewed_at, rule_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("规则不是当前草稿版本")
            self._audit("tariff_rule", rule_id, "tariff.reviewed", actor_id, {})
        return {"rule_id": rule_id, "state": "reviewed", "revision": expected_revision + 1}

    def publish_tariff_rule(self, actor_id: str, rule_id: str, expected_revision: int) -> dict[str, Any]:
        """发布规则版本；正式发布不可覆盖，同日修订以更高版本生效。

        发布会为已出账且被新版本覆盖的账单生成可追踪的重算建议，原账单不变。
        """
        self._require(actor_id, "tariff.publish")
        row = self._tariff_rule_row(rule_id)
        if row["reviewed_by"] is not None and row["reviewed_by"] == actor_id:
            raise Forbidden("发布人不能是复核人")
        published_at = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE tariff_rules SET state='published',published_by=?,published_at=?,revision=revision+1 "
                "WHERE rule_id=? AND state='reviewed' AND revision=?",
                (actor_id, published_at, rule_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("规则不是当前已复核版本")
            overlaps = [
                dict(item)
                for item in self.connection.execute(
                    "SELECT rule_id,version,effective_from,effective_to FROM tariff_rules "
                    "WHERE series=? AND state='published' AND rule_id<>? "
                    "AND effective_from<=? AND effective_to>=? ORDER BY version",
                    (row["series"], rule_id, row["effective_to"], row["effective_from"]),
                ).fetchall()
            ]
            suggestions = self._suggest_recalculations(row, published_at)
            self._audit(
                "tariff_rule",
                rule_id,
                "tariff.published",
                actor_id,
                {
                    "overlaps": [item["rule_id"] for item in overlaps],
                    "recalculation_suggestions": len(suggestions),
                },
            )
        return {
            "rule_id": rule_id,
            "state": "published",
            "revision": expected_revision + 1,
            "overlaps": overlaps,
            "recalculation_suggestions": len(suggestions),
        }

    def _suggest_recalculations(self, rule_row: sqlite3.Row, created_at: str) -> list[dict[str, Any]]:
        bills = self.connection.execute(
            "SELECT * FROM settlement_bills WHERE series=? AND rule_version<? "
            "AND period_start<=? AND period_end>=? ORDER BY settlement_id",
            (
                rule_row["series"],
                int(rule_row["version"]),
                rule_row["effective_to"],
                rule_row["effective_from"],
            ),
        ).fetchall()
        new_definition = self._definition_from_row(rule_row)
        suggestions: list[dict[str, Any]] = []
        for bill in bills:
            new_total: str | None = None
            delta: str | None = None
            covers = (
                rule_row["effective_from"] <= bill["period_start"]
                and bill["period_end"] <= rule_row["effective_to"]
            )
            if covers:
                try:
                    payload = json.loads(bill["bill_json"])
                    readings = tuple(
                        MeterInterval(
                            parse_utc(item["start_utc"]),
                            parse_utc(item["end_utc"]),
                            Decimal(item["mwh"]),
                        )
                        for item in payload["inputs"]["readings"]
                    )
                    recomputed = compute_bill(
                        definition=new_definition,
                        readings=readings,
                        period_start=date.fromisoformat(bill["period_start"]),
                        period_end=date.fromisoformat(bill["period_end"]),
                    )
                    new_total = recomputed["totals"]["amount_cny"]
                    delta = decimal_text(
                        quantize_money(Decimal(new_total) - Decimal(bill["total_amount_cny"]))
                    )
                    detail: dict[str, Any] = {"new_totals": recomputed["totals"], "note": "按新规则版本重算"}
                except (ValueError, KeyError) as exc:
                    detail = {"note": f"重算失败：{exc}"}
            else:
                detail = {"note": "新规则生效区间未完整覆盖账单期间，需人工拆分"}
            cursor = self.connection.execute(
                "INSERT INTO recalculation_suggestions(bill_id,rule_id,rule_version,old_total_cny,"
                "new_total_cny,delta_cny,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    bill["settlement_id"],
                    rule_row["rule_id"],
                    int(rule_row["version"]),
                    bill["total_amount_cny"],
                    new_total,
                    delta,
                    canonical_json(detail),
                    created_at,
                ),
            )
            suggestions.append(
                {"suggestion_id": int(cursor.lastrowid), "bill_id": bill["settlement_id"]}
            )
        return suggestions

    def retire_tariff_rule(self, actor_id: str, rule_id: str, expected_revision: int) -> dict[str, Any]:
        """退役规则；退役不影响已锁定该版本的合同和账单。"""
        self._require(actor_id, "tariff.retire")
        self._tariff_rule_row(rule_id)
        retired_at = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE tariff_rules SET state='retired',retired_by=?,retired_at=?,revision=revision+1 "
                "WHERE rule_id=? AND state IN ('draft','reviewed','published') AND revision=?",
                (actor_id, retired_at, rule_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("规则已退役或版本不匹配")
            self._audit("tariff_rule", rule_id, "tariff.retired", actor_id, {})
        return {"rule_id": rule_id, "state": "retired", "revision": expected_revision + 1}

    def preview_tariff(self, rule_id: str, local_date: str) -> dict[str, Any]:
        """预览某个本地日期的分时应付电价，发布前后的规则版本都可用。"""
        row = self._tariff_rule_row(rule_id)
        try:
            local_day = date.fromisoformat(local_date)
        except ValueError as exc:
            raise ValidationFailed("date 必须是 YYYY-MM-DD 日期") from exc
        preview = preview_day(definition=self._definition_from_row(row), local_day=local_day)
        return {
            "rule_id": row["rule_id"],
            "series": row["series"],
            "version": row["version"],
            "state": row["state"],
            **preview,
        }

    def _resolve_rule_row(self, series: str, local_date: str) -> sqlite3.Row | None:
        """同一日期多个已生效版本重叠时，取版本最高者，保证解析确定性。"""
        return self.connection.execute(
            "SELECT * FROM tariff_rules WHERE series=? AND state='published' "
            "AND effective_from<=? AND effective_to>=? "
            "ORDER BY version DESC, published_at DESC, rule_id ASC LIMIT 1",
            (series, local_date, local_date),
        ).fetchone()

    def resolve_tariff(self, series: str, local_date: str) -> dict[str, Any]:
        try:
            date.fromisoformat(local_date)
        except ValueError as exc:
            raise ValidationFailed("date 必须是 YYYY-MM-DD 日期") from exc
        row = self._resolve_rule_row(series, local_date)
        if row is None:
            raise NotFound("该日期没有已生效的电价规则")
        return self._tariff_rule_summary(row)

    def create_contract(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """签订合同并锁定签订时生效的规则版本，之后按锁定版本计费。"""
        self._require(actor_id, "contract.write")
        contract = ContractInput.from_dict(raw)
        resolved = self._resolve_rule_row(contract.series, contract.service_start)
        if resolved is None or not (
            resolved["effective_from"] <= contract.service_start
            and contract.service_end <= resolved["effective_to"]
        ):
            raise InvalidState("没有覆盖合同完整服务期的已生效电价规则")
        signed_at = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO contracts(contract_id,counterparty,series,rule_id,rule_version,rule_sha256,"
                    "service_start,service_end,signed_by,signed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        contract.contract_id,
                        contract.counterparty,
                        contract.series,
                        resolved["rule_id"],
                        int(resolved["version"]),
                        resolved["content_sha256"],
                        contract.service_start,
                        contract.service_end,
                        actor_id,
                        signed_at,
                    ),
                )
                self._audit(
                    "contract",
                    contract.contract_id,
                    "contract.signed",
                    actor_id,
                    {
                        "rule_id": resolved["rule_id"],
                        "rule_version": int(resolved["version"]),
                        "rule_sha256": resolved["content_sha256"],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("合同编号已经存在") from exc
        return {
            "contract_id": contract.contract_id,
            "series": contract.series,
            "rule_id": resolved["rule_id"],
            "rule_version": int(resolved["version"]),
            "rule_sha256": resolved["content_sha256"],
            "signed_at": signed_at,
        }

    def contract(self, contract_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM contracts WHERE contract_id=?", (contract_id,)
        ).fetchone()
        if row is None:
            raise NotFound("合同不存在")
        return dict(row)

    def settle_contract(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """按合同锁定的规则版本结算；重复提交相同内容返回相同账单。"""
        self._require(actor_id, "settlement.run")
        settlement = SettlementInput.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency "
            "WHERE scope='settlement' AND idempotency_key=?",
            (settlement.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同结算内容")
            return {**json.loads(stored["response_json"]), "replayed": True}
        contract = self.connection.execute(
            "SELECT * FROM contracts WHERE contract_id=?", (settlement.contract_id,)
        ).fetchone()
        if contract is None:
            raise NotFound("合同不存在")
        if not (
            contract["service_start"] <= settlement.period_start
            and settlement.period_end <= contract["service_end"]
        ):
            raise ValidationFailed("结算期间超出合同服务期")
        rule = self._tariff_rule_row(contract["rule_id"])
        if rule["content_sha256"] != contract["rule_sha256"]:
            raise Conflict("合同锁定的规则内容已变化")
        if rule["state"] not in ("published", "retired"):
            raise InvalidState("合同锁定的规则未生效")
        if not (
            rule["effective_from"] <= settlement.period_start
            and settlement.period_end <= rule["effective_to"]
        ):
            raise InvalidState("结算期间超出规则生效区间")
        try:
            computed = compute_bill(
                definition=self._definition_from_row(rule),
                readings=settlement.readings,
                period_start=date.fromisoformat(settlement.period_start),
                period_end=date.fromisoformat(settlement.period_end),
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        normalized_readings = [
            {
                "start_utc": utc_text(item.start_utc),
                "end_utc": utc_text(item.end_utc),
                "mwh": decimal_text(item.mwh),
            }
            for item in sorted(settlement.readings, key=lambda entry: (entry.start_utc, entry.end_utc))
        ]
        input_sha256 = digest({
            "contract_id": settlement.contract_id,
            "period_start": settlement.period_start,
            "period_end": settlement.period_end,
            "rule_sha256": rule["content_sha256"],
            "readings": normalized_readings,
        })
        created_at = self._now()
        bill = {
            "settlement_id": settlement.settlement_id,
            "contract_id": settlement.contract_id,
            "period": {
                "start": settlement.period_start,
                "end": settlement.period_end,
                "timezone": rule["timezone"],
            },
            "rule_snapshot": {
                "rule_id": rule["rule_id"],
                "series": rule["series"],
                "version": int(rule["version"]),
                "content_sha256": rule["content_sha256"],
                "timezone": rule["timezone"],
                "effective_from": rule["effective_from"],
                "effective_to": rule["effective_to"],
                "definition": json.loads(rule["definition_json"]),
            },
            "inputs": {"readings": normalized_readings, "input_sha256": input_sha256},
            "rounding": ROUNDING,
            "lines": computed["lines"],
            "totals": computed["totals"],
            "created_by": actor_id,
            "created_at": created_at,
        }
        response = {
            "settlement_id": settlement.settlement_id,
            "contract_id": settlement.contract_id,
            "rule_id": rule["rule_id"],
            "rule_version": int(rule["version"]),
            "period_start": settlement.period_start,
            "period_end": settlement.period_end,
            "input_sha256": input_sha256,
            "total_energy_mwh": computed["totals"]["energy_mwh"],
            "total_amount_cny": computed["totals"]["amount_cny"],
            "line_count": computed["totals"]["line_count"],
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO settlement_bills(settlement_id,contract_id,series,rule_id,rule_version,"
                    "period_start,period_end,input_sha256,bill_json,total_energy_mwh,total_amount_cny,"
                    "idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        settlement.settlement_id,
                        settlement.contract_id,
                        rule["series"],
                        rule["rule_id"],
                        int(rule["version"]),
                        settlement.period_start,
                        settlement.period_end,
                        input_sha256,
                        canonical_json(bill),
                        computed["totals"]["energy_mwh"],
                        computed["totals"]["amount_cny"],
                        settlement.idempotency_key,
                        actor_id,
                        created_at,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,"
                    "created_at) VALUES('settlement',?,?,?,?)",
                    (settlement.idempotency_key, request_digest, canonical_json(response), created_at),
                )
                self._audit(
                    "settlement",
                    settlement.settlement_id,
                    "settlement.completed",
                    actor_id,
                    {
                        "contract_id": settlement.contract_id,
                        "input_sha256": input_sha256,
                        "total_amount_cny": computed["totals"]["amount_cny"],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("结算编号、幂等键或合同结算期间冲突") from exc
        return {**response, "replayed": False}

    def settlement_bill(self, settlement_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT bill_json FROM settlement_bills WHERE settlement_id=?", (settlement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("结算账单不存在")
        return json.loads(row["bill_json"])

    def recalculations(self, settlement_id: str | None = None) -> dict[str, Any]:
        if settlement_id is None:
            rows = self.connection.execute(
                "SELECT * FROM recalculation_suggestions ORDER BY suggestion_id DESC LIMIT 100"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM recalculation_suggestions WHERE bill_id=? ORDER BY suggestion_id",
                (settlement_id,),
            ).fetchall()
        suggestions = [
            {
                "suggestion_id": row["suggestion_id"],
                "bill_id": row["bill_id"],
                "rule_id": row["rule_id"],
                "rule_version": row["rule_version"],
                "status": row["status"],
                "old_total_cny": row["old_total_cny"],
                "new_total_cny": row["new_total_cny"],
                "delta_cny": row["delta_cny"],
                "detail": json.loads(row["detail_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        return {"suggestions": suggestions, "count": len(suggestions)}

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
