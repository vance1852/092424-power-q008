"""峰平谷分时电价与节假日加价的确定性计算。

时段按 [start, end) 分钟语义解释，end 不晚于 start 的时段视为跨午夜时段，
在内部展开为两段。一天 1440 分钟必须被全部时段恰好划分一次，不允许重叠
或空缺。电量区间跨越时段边界或规则时区本地午夜时按微秒比例拆分，节假日
按规则时区的本地日期判定。每条账单明细同时记录原始值与舍入值，舍入量子
和舍入方式固定，保证账单可以离线复算且重复计算结果一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clock import utc_text
from .planning import decimal_text, quantize_money, quantize_volume


MINUTES_PER_DAY = 1440
PRICE_QUANTUM = Decimal("0.0001")
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
TOU_KINDS = ("peak", "flat", "valley")
ROUNDING = {
    "energy_quantum": "0.001",
    "price_quantum": "0.0001",
    "money_quantum": "0.01",
    "mode": "ROUND_HALF_UP",
}


def quantize_price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)


def minute_text(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def parse_minute(value: object, field: str, *, allow_end_of_day: bool = False) -> int:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 HH:MM 文本")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"{field} 必须是 HH:MM 文本")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 HH:MM 文本") from exc
    if allow_end_of_day and hour == 24 and minute == 0:
        return MINUTES_PER_DAY
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"{field} 超出有效时间范围")
    return hour * 60 + minute


def _decimal(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValueError(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{field} 不能大于 {maximum}")
    return result


@dataclass(frozen=True, slots=True)
class TouPeriod:
    kind: str
    start_minute: int
    end_minute: int
    price_cny: Decimal

    def intervals(self) -> tuple[tuple[int, int], ...]:
        """展开为不跨午夜的开闭区间；end <= start 的时段拆成两段。"""
        if self.end_minute > self.start_minute:
            return ((self.start_minute, self.end_minute),)
        return ((self.start_minute, MINUTES_PER_DAY), (0, self.end_minute))

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "start": minute_text(self.start_minute),
            "end": minute_text(self.end_minute),
            "price_cny_per_mwh": decimal_text(self.price_cny),
        }


@dataclass(frozen=True, slots=True)
class TariffDefinition:
    timezone: str
    periods: tuple[TouPeriod, ...]
    holiday_surcharge_percent: Decimal
    holidays: frozenset[date]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TariffDefinition":
        if not isinstance(raw, Mapping):
            raise ValueError("definition 必须是对象")
        timezone_text = raw.get("timezone")
        if not isinstance(timezone_text, str) or not timezone_text.strip():
            raise ValueError("timezone 不能为空")
        timezone_text = timezone_text.strip()
        try:
            ZoneInfo(timezone_text)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone 不是有效的 IANA 时区") from exc
        raw_periods = raw.get("periods")
        if not isinstance(raw_periods, list) or not 1 <= len(raw_periods) <= 24:
            raise ValueError("periods 必须是 1 到 24 个时段")
        periods: list[TouPeriod] = []
        for index, item in enumerate(raw_periods):
            field = f"periods[{index}]"
            if not isinstance(item, Mapping):
                raise ValueError(f"{field} 必须是对象")
            kind = item.get("kind")
            if kind not in TOU_KINDS:
                raise ValueError(f"{field}.kind 必须是 peak、flat 或 valley")
            start = parse_minute(item.get("start"), f"{field}.start")
            end = parse_minute(item.get("end"), f"{field}.end", allow_end_of_day=True)
            if start == end:
                raise ValueError(f"{field} 起止时间不能相同")
            price = _decimal(
                item.get("price_cny_per_mwh"),
                f"{field}.price_cny_per_mwh",
                minimum=ZERO,
                maximum=Decimal("1000000"),
            )
            periods.append(TouPeriod(kind, start, end, price))
        coverage_map(periods)
        raw_holidays = raw.get("holidays", [])
        if not isinstance(raw_holidays, list) or len(raw_holidays) > 400:
            raise ValueError("holidays 必须是不超过 400 个日期的数组")
        holidays: set[date] = set()
        for item in raw_holidays:
            if not isinstance(item, str):
                raise ValueError("holidays 必须是 YYYY-MM-DD 日期文本")
            try:
                holidays.add(date.fromisoformat(item))
            except ValueError as exc:
                raise ValueError("holidays 必须是 YYYY-MM-DD 日期") from exc
        surcharge = _decimal(
            raw.get("holiday_surcharge_percent", 0),
            "holiday_surcharge_percent",
            minimum=ZERO,
            maximum=HUNDRED,
        )
        return cls(
            timezone=timezone_text,
            periods=tuple(periods),
            holiday_surcharge_percent=surcharge,
            holidays=frozenset(holidays),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "timezone": self.timezone,
            "periods": [period.as_dict() for period in self.periods],
            "holiday_surcharge_percent": decimal_text(self.holiday_surcharge_percent),
            "holidays": sorted(day.isoformat() for day in self.holidays),
        }


@dataclass(frozen=True, slots=True)
class MeterInterval:
    start_utc: datetime
    end_utc: datetime
    mwh: Decimal


def coverage_map(periods: Sequence[TouPeriod]) -> tuple[TouPeriod, ...]:
    """把时段展开成 1440 分钟的覆盖表，校验恰好划分全天。"""
    slots: list[TouPeriod | None] = [None] * MINUTES_PER_DAY
    for period in periods:
        for start, end in period.intervals():
            for minute in range(start, end):
                if slots[minute] is not None:
                    raise ValueError(f"时段在 {minute_text(minute)} 附近重叠")
                slots[minute] = period
    for minute, period in enumerate(slots):
        if period is None:
            raise ValueError(f"时段未覆盖 {minute_text(minute)}，一天必须覆盖 1440 分钟")
    return tuple(slot for slot in slots if slot is not None)


def local_instant(zone: ZoneInfo, day: date, minute_of_day: int) -> datetime:
    """把规则时区的本地日期和分钟（0..1440）映射为 UTC 时刻。

    夏令时切换导致本地时间不存在时顺延到下一个有效分钟；重复时刻取第一次
    出现，保证映射确定性。
    """
    naive = datetime.combine(day, time.min) + timedelta(minutes=minute_of_day)
    for _ in range(180):
        utc = naive.replace(tzinfo=zone).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            return utc
        naive += timedelta(minutes=1)
    raise ValueError("本地时间无法映射到 UTC")


def _micros(delta: timedelta) -> int:
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _split_interval(
    zone: ZoneInfo,
    boundary_minutes: Sequence[int],
    start_utc: datetime,
    end_utc: datetime,
) -> list[tuple[datetime, datetime]]:
    """在时段边界和本地午夜处切开电量区间。"""
    first_day = start_utc.astimezone(zone).date()
    last_day = (end_utc - timedelta(microseconds=1)).astimezone(zone).date()
    points: set[datetime] = set()
    day = first_day
    while day <= last_day:
        for minute in boundary_minutes:
            points.add(local_instant(zone, day, minute))
        day += timedelta(days=1)
    cuts = sorted(point for point in points if start_utc < point < end_utc)
    edges = [start_utc, *cuts, end_utc]
    return list(zip(edges, edges[1:]))


def compute_bill(
    *,
    definition: TariffDefinition,
    readings: Sequence[MeterInterval],
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    """按规则定义把结算期内的电量区间计价，返回明细行与合计。

    每条明细记录原始电量、舍入电量、原始单价、舍入单价、原始金额和舍入
    金额；合计为各明细舍入值之和，不做二次舍入。
    """
    if period_end < period_start:
        raise ValueError("结算期间起止日期颠倒")
    if not readings:
        raise ValueError("结算电量不能为空")
    zone = ZoneInfo(definition.timezone)
    slots = coverage_map(definition.periods)
    window_start = local_instant(zone, period_start, 0)
    window_end = local_instant(zone, period_end, MINUTES_PER_DAY)
    ordered = sorted(readings, key=lambda item: (item.start_utc, item.end_utc))
    previous_end: datetime | None = None
    for reading in ordered:
        if reading.mwh <= ZERO:
            raise ValueError("结算电量必须为正数")
        if reading.end_utc <= reading.start_utc:
            raise ValueError("电量区间结束必须晚于开始")
        if reading.start_utc < window_start or reading.end_utc > window_end:
            raise ValueError("电量区间超出结算期间")
        if previous_end is not None and reading.start_utc < previous_end:
            raise ValueError("电量区间存在重叠")
        previous_end = reading.end_utc
    boundary_minutes = sorted(
        {0}
        | {period.start_minute for period in definition.periods}
        | {period.end_minute for period in definition.periods}
    )
    lines: list[dict[str, Any]] = []
    with localcontext() as context:
        context.prec = 28
        for index, reading in enumerate(ordered):
            total_micros = _micros(reading.end_utc - reading.start_utc)
            for seg_start, seg_end in _split_interval(
                zone, boundary_minutes, reading.start_utc, reading.end_utc
            ):
                midpoint = seg_start + (seg_end - seg_start) / 2
                local = midpoint.astimezone(zone)
                period = slots[local.hour * 60 + local.minute]
                local_day = local.date()
                is_holiday = local_day in definition.holidays
                share = reading.mwh * Decimal(_micros(seg_end - seg_start)) / Decimal(total_micros)
                energy = quantize_volume(share)
                surcharge = definition.holiday_surcharge_percent if is_holiday else ZERO
                effective_raw = period.price_cny * (ONE + surcharge / HUNDRED)
                effective = quantize_price(effective_raw)
                amount_raw = energy * effective
                amount = quantize_money(amount_raw)
                lines.append({
                    "reading_index": index,
                    "start_utc": utc_text(seg_start),
                    "end_utc": utc_text(seg_end),
                    "local_date": local_day.isoformat(),
                    "kind": period.kind,
                    "holiday": is_holiday,
                    "energy_raw_mwh": decimal_text(share),
                    "energy_mwh": decimal_text(energy),
                    "unit_price_cny_per_mwh": decimal_text(period.price_cny),
                    "surcharge_percent": decimal_text(surcharge),
                    "effective_price_raw_cny_per_mwh": decimal_text(effective_raw),
                    "effective_price_cny_per_mwh": decimal_text(effective),
                    "amount_raw_cny": decimal_text(amount_raw),
                    "amount_cny": decimal_text(amount),
                })
    by_kind: dict[str, dict[str, Decimal]] = {}
    total_energy = ZERO
    total_amount = ZERO
    holiday_energy = ZERO
    for line in lines:
        energy = Decimal(line["energy_mwh"])
        amount = Decimal(line["amount_cny"])
        total_energy += energy
        total_amount += amount
        bucket = by_kind.setdefault(line["kind"], {"energy": ZERO, "amount": ZERO})
        bucket["energy"] += energy
        bucket["amount"] += amount
        if line["holiday"]:
            holiday_energy += energy
    totals = {
        "energy_mwh": decimal_text(quantize_volume(total_energy)),
        "amount_cny": decimal_text(quantize_money(total_amount)),
        "holiday_energy_mwh": decimal_text(quantize_volume(holiday_energy)),
        "line_count": len(lines),
        "by_kind": [
            {
                "kind": kind,
                "energy_mwh": decimal_text(quantize_volume(by_kind[kind]["energy"])),
                "amount_cny": decimal_text(quantize_money(by_kind[kind]["amount"])),
            }
            for kind in TOU_KINDS
            if kind in by_kind
        ],
    }
    return {"lines": lines, "totals": totals}


def preview_day(*, definition: TariffDefinition, local_day: date) -> dict[str, Any]:
    """生成某个本地日期的分时应付电价曲线，草稿和已生效规则都可用。"""
    slots = coverage_map(definition.periods)
    is_holiday = local_day in definition.holidays
    surcharge = definition.holiday_surcharge_percent if is_holiday else ZERO
    segments: list[dict[str, str]] = []
    minute = 0
    while minute < MINUTES_PER_DAY:
        period = slots[minute]
        end = minute + 1
        while end < MINUTES_PER_DAY and slots[end] is period:
            end += 1
        effective = quantize_price(period.price_cny * (ONE + surcharge / HUNDRED))
        segments.append({
            "start": minute_text(minute),
            "end": minute_text(end),
            "kind": period.kind,
            "price_cny_per_mwh": decimal_text(period.price_cny),
            "surcharge_percent": decimal_text(surcharge),
            "effective_price_cny_per_mwh": decimal_text(effective),
        })
        minute = end
    return {
        "date": local_day.isoformat(),
        "timezone": definition.timezone,
        "holiday": is_holiday,
        "holiday_surcharge_percent": decimal_text(definition.holiday_surcharge_percent),
        "segments": segments,
    }
