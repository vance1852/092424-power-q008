"""电厂调度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed
from .tariff import MeterInterval, TariffDefinition


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
CRUDE_GRADES = {"PEAK_VALLEY", "WTI", "DUBAI", "ESPO", "URAL", "CUSTOM"}
PRODUCTS = {"crude", "gasoline-92", "gasoline-95", "diesel", "jet-fuel", "condensate"}
ROUTE_KINDS = {"pipeline", "terminal", "refinery", "storage", "truck-rack"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class IndexQuote:
    market_index: str
    trade_date: str
    close_cny: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IndexQuote":
        market_index = required_text(raw.get("market_index"), "market_index", 16).upper()
        if market_index not in CRUDE_GRADES - {"CUSTOM"}:
            raise ValidationFailed("market_index 必须是 PEAK_VALLEY、WTI、DUBAI、ESPO 或 URAL")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            market_index=market_index,
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            close_cny=decimal_value(raw.get("close_cny"), "close_cny", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class Facility:
    facility_id: str
    name: str
    kind: str
    timezone: str
    capacity_mwh: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Facility":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in ROUTE_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_mwh=decimal_value(
                raw.get("capacity_mwh"), "capacity_mwh", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    origin_id: str
    destination_id: str
    product: str
    daily_capacity: Decimal
    loss_basis_points: int
    transit_hours: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Route":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的电源类型")
        loss = raw.get("loss_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("loss_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_id"), "origin_id")
        destination = identifier(raw.get("destination_id"), "destination_id")
        if origin == destination:
            raise ValidationFailed("送出线路起点和终点不能相同")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            origin_id=origin,
            destination_id=destination,
            product=product,
            daily_capacity=decimal_value(
                raw.get("daily_capacity"), "daily_capacity", minimum=Decimal("0.001")
            ),
            loss_basis_points=loss,
            transit_hours=positive_integer(raw.get("transit_hours"), "transit_hours"),
        )


@dataclass(frozen=True, slots=True)
class InventoryLot:
    lot_id: str
    facility_id: str
    product: str
    grade: str
    quantity_mwh: Decimal
    unit_cost_cny: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryLot":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的电源类型")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_mwh=decimal_value(
                raw.get("quantity_mwh"), "quantity_mwh", minimum=Decimal("0.001")
            ),
            unit_cost_cny=decimal_value(
                raw.get("unit_cost_cny"), "unit_cost_cny", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class NominationRequest:
    nomination_id: str
    route_id: str
    shipper_id: str
    service_date: str
    requested_mwh: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NominationRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            shipper_id=identifier(raw.get("shipper_id"), "shipper_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            requested_mwh=decimal_value(
                raw.get("requested_mwh"), "requested_mwh", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class TariffRuleInput:
    rule_id: str
    series: str
    timezone: str
    effective_from: str
    effective_to: str
    definition: TariffDefinition

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TariffRuleInput":
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        definition_raw = raw.get("definition")
        if not isinstance(definition_raw, Mapping):
            raise ValidationFailed("definition 必须是对象")
        try:
            definition = TariffDefinition.from_dict({**definition_raw, "timezone": timezone})
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        effective_from = date_text(raw.get("effective_from"), "effective_from")
        effective_to = date_text(raw.get("effective_to"), "effective_to")
        if effective_to < effective_from:
            raise ValidationFailed("effective_to 不能早于 effective_from")
        span = date.fromisoformat(effective_to) - date.fromisoformat(effective_from)
        if span.days > 3660:
            raise ValidationFailed("生效区间不能超过 3660 天")
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            series=identifier(raw.get("series"), "series"),
            timezone=timezone,
            effective_from=effective_from,
            effective_to=effective_to,
            definition=definition,
        )


@dataclass(frozen=True, slots=True)
class ContractInput:
    contract_id: str
    counterparty: str
    series: str
    service_start: str
    service_end: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ContractInput":
        service_start = date_text(raw.get("service_start"), "service_start")
        service_end = date_text(raw.get("service_end"), "service_end")
        if service_end < service_start:
            raise ValidationFailed("service_end 不能早于 service_start")
        span = date.fromisoformat(service_end) - date.fromisoformat(service_start)
        if span.days > 3660:
            raise ValidationFailed("合同服务期不能超过 3660 天")
        return cls(
            contract_id=identifier(raw.get("contract_id"), "contract_id"),
            counterparty=required_text(raw.get("counterparty"), "counterparty"),
            series=identifier(raw.get("series"), "series"),
            service_start=service_start,
            service_end=service_end,
        )


@dataclass(frozen=True, slots=True)
class SettlementInput:
    settlement_id: str
    contract_id: str
    period_start: str
    period_end: str
    idempotency_key: str
    readings: tuple[MeterInterval, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SettlementInput":
        period_start = date_text(raw.get("period_start"), "period_start")
        period_end = date_text(raw.get("period_end"), "period_end")
        if period_end < period_start:
            raise ValidationFailed("period_end 不能早于 period_start")
        span = date.fromisoformat(period_end) - date.fromisoformat(period_start)
        if span.days > 400:
            raise ValidationFailed("结算期间不能超过 400 天")
        raw_readings = raw.get("readings")
        if not isinstance(raw_readings, list) or not 1 <= len(raw_readings) <= 5000:
            raise ValidationFailed("readings 必须是 1 到 5000 个电量区间")
        readings: list[MeterInterval] = []
        for index, item in enumerate(raw_readings):
            field = f"readings[{index}]"
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"{field} 必须是对象")
            try:
                start_utc = parse_utc(
                    required_text(item.get("start_utc"), f"{field}.start_utc", 40),
                    f"{field}.start_utc",
                )
                end_utc = parse_utc(
                    required_text(item.get("end_utc"), f"{field}.end_utc", 40),
                    f"{field}.end_utc",
                )
            except ValueError as exc:
                raise ValidationFailed(str(exc)) from exc
            mwh = decimal_value(
                item.get("mwh"), f"{field}.mwh", minimum=Decimal("0.001"), maximum=Decimal("1000000000")
            )
            readings.append(MeterInterval(start_utc, end_utc, mwh))
        return cls(
            settlement_id=identifier(raw.get("settlement_id"), "settlement_id"),
            contract_id=identifier(raw.get("contract_id"), "contract_id"),
            period_start=period_start,
            period_end=period_end,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
            readings=tuple(readings),
        )


@dataclass(frozen=True, slots=True)
class SupplyScenario:
    scenario_id: str
    name: str
    market_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplyScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_routes = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            market_index_drop_percent=decimal_value(
                raw.get("market_index_drop_percent", 0),
                "market_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_routes,
            demand_changes=parsed_demand,
        )
