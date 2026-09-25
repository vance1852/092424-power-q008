"""分时电价规则的确定性计算：时段展开、分段计价与舍入过程。

时段按本地墙钟时间定义，再统一换算成 UTC 区间，以正确处理跨午夜和夏令时。
所有金额与电量用 Decimal 计算，逐段保留舍入前中间值，保证账单可复核。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from .errors import ValidationFailed
from .planning import canonical_json, decimal_text, digest, quantize_money, quantize_volume


MONEY_QUANTUM = Decimal("0.01")
VOLUME_QUANTUM = Decimal("0.001")
WEEKDAYS = frozenset(range(7))  # Monday=0 ... Sunday=6

PERIOD_KINDS = {"peak", "flat", "valley"}
DAY_TYPES = {"workday", "holiday", "all"}
DEDUCT_KINDS = {"absolute", "percent"}


class TariffRuleError(ValidationFailed):
    """规则定义不合法。"""


def _hour_minute(value: object, field: str) -> time:
    if not isinstance(value, str):
        raise TariffRuleError(f"{field} 必须是 HH:MM 文本")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise TariffRuleError(f"{field} 必须是 HH:MM") from exc
    if parsed.second != 0 or parsed.microsecond != 0:
        raise TariffRuleError(f"{field} 只能精确到分钟")
    return parsed.replace(tzinfo=None)


def _money(value: object, field: str, *, allow_negative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # InvalidOperation 等
        raise TariffRuleError(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise TariffRuleError(f"{field} 必须是有限数值")
    if not allow_negative and result < 0:
        raise TariffRuleError(f"{field} 不能为负数")
    return result


@dataclass(frozen=True, slots=True)
class TimePeriod:
    kind: str
    day_type: str
    start_minute: int  # 含，0..1439
    end_minute: int  # 不含，1..1440，>start_minute（跨午夜在展开时翻卷）
    price_cny: Decimal
    holiday_add: Decimal
    deduct_kind: str
    deduct_value: Decimal

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "TimePeriod":
        kind = str(raw.get("kind", "")).strip()
        if kind not in PERIOD_KINDS:
            raise TariffRuleError("时段类型必须是 peak、flat 或 valley")
        day_type = str(raw.get("day_type", "all")).strip()
        if day_type not in DAY_TYPES:
            raise TariffRuleError("day_type 必须是 workday、holiday 或 all")
        start = _hour_minute(raw.get("start"), "时段 start")
        end_text = raw.get("end")
        end = _hour_minute(end_text, "时段 end") if end_text != "24:00" else time(0, 0)
        start_minute = start.hour * 60 + start.minute
        end_minute = end.hour * 60 + end.minute
        if end_text == "24:00":
            end_minute = 1440
        if not 0 <= start_minute < 1440:
            raise TariffRuleError("时段起点必须在 00:00 到 23:59 之间")
        if not 1 <= end_minute <= 1440:
            raise TariffRuleError("时段终点必须在 00:01 到 24:00 之间")
        if end_minute == start_minute:
            raise TariffRuleError("时段长度不能为零")
        price = _money(raw.get("price_cny"), "price_cny")
        holiday_add = _money(raw.get("holiday_add_cny", 0), "holiday_add_cny", allow_negative=True)
        deduct_kind = str(raw.get("deduct_kind", "absolute")).strip()
        if deduct_kind not in DEDUCT_KINDS:
            raise TariffRuleError("deduct_kind 必须是 absolute 或 percent")
        deduct_value = _money(raw.get("deduct_value", 0), "deduct_value", allow_negative=True)
        if deduct_kind == "percent" and not Decimal("-100") < deduct_value:
            raise TariffRuleError("百分比减免必须大于 -100")
        return cls(
            kind=kind,
            day_type=day_type,
            start_minute=start_minute,
            end_minute=end_minute,
            price_cny=price,
            holiday_add=holiday_add,
            deduct_kind=deduct_kind,
            deduct_value=deduct_value,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "day_type": self.day_type,
            "start": _minute_text(self.start_minute),
            "end": _minute_text(self.end_minute),
            "price_cny": decimal_text(self.price_cny),
            "holiday_add_cny": decimal_text(self.holiday_add),
            "deduct_kind": self.deduct_kind,
            "deduct_value": decimal_text(self.deduct_value),
        }


def _minute_text(value: int) -> str:
    if value == 1440:
        return "24:00"
    return f"{value // 60:02d}:{value % 60:02d}"


@dataclass(frozen=True, slots=True)
class TariffRule:
    timezone_name: str
    periods: tuple[TimePeriod, ...]
    holidays: frozenset[str]
    workday_weekdays: frozenset[int]
    version_tag: str

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "TariffRule":
        timezone_name = str(raw.get("timezone", "")).strip()
        try:
            zone = ZoneInfo(timezone_name)
        except Exception as exc:  # ZoneInfoError/KeyError
            raise TariffRuleError("timezone 必须是有效的 IANA 时区") from exc
        # UTC 固定偏移无夏令时问题，但仍校验可解析；非 IANA 名称直接拒绝
        if timezone_name != "UTC" and "/" not in timezone_name:
            raise TariffRuleError("timezone 必须是 IANA 时区或 UTC")
        del zone
        periods_raw = raw.get("periods")
        if not isinstance(periods_raw, list) or not periods_raw:
            raise TariffRuleError("periods 至少包含一个时段")
        periods: list[TimePeriod] = []
        for item in periods_raw:
            if not isinstance(item, dict):
                raise TariffRuleError("periods 中的每个时段必须是对象")
            periods.append(TimePeriod.from_dict(item))
        periods = tuple(periods)
        holidays = cls._holidays(raw.get("holidays", []))
        weekdays_raw = raw.get("workday_weekdays", [0, 1, 2, 3, 4])
        if not isinstance(weekdays_raw, list) or not weekdays_raw:
            raise TariffRuleError("workday_weekdays 必须是非空数组")
        weekdays: set[int] = set()
        for item in weekdays_raw:
            if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 6:
                raise TariffRuleError("workday_weekdays 使用 0(周一) 到 6(周日) 的整数")
            weekdays.add(item)
        version_tag = str(raw.get("version_tag", "")).strip()
        if not 1 <= len(version_tag) <= 64:
            raise TariffRuleError("version_tag 不能为空且不超过 64 字符")
        rule = cls(timezone_name, periods, holidays, frozenset(weekdays), version_tag)
        rule.validate_coverage()
        return rule

    @staticmethod
    def _holidays(raw: object) -> frozenset[str]:
        if not isinstance(raw, list):
            raise TariffRuleError("holidays 必须是日期数组")
        result: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                raise TariffRuleError("holidays 必须是 YYYY-MM-DD 日期")
            try:
                text = date.fromisoformat(item).isoformat()
            except ValueError as exc:
                raise TariffRuleError("holidays 必须是 YYYY-MM-DD 日期") from exc
            if text in result:
                raise TariffRuleError(f"节假日 {text} 重复")
            result.add(text)
        return frozenset(result)

    def as_dict(self) -> dict[str, object]:
        return {
            "timezone": self.timezone_name,
            "periods": [period.as_dict() for period in sorted(self.periods, key=_period_sort_key)],
            "holidays": sorted(self.holidays),
            "workday_weekdays": sorted(self.workday_weekdays),
            "version_tag": self.version_tag,
        }

    def canonical_text(self) -> str:
        return canonical_json(self.as_dict())

    def content_sha256(self) -> str:
        return digest(self.as_dict())

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def is_holiday(self, local_day: date) -> bool:
        return local_day.isoformat() in self.holidays

    def day_class(self, local_day: date) -> str:
        if self.is_holiday(local_day):
            return "holiday"
        return "workday" if local_day.weekday() in self.workday_weekdays else "weekend"

    def validate_coverage(self) -> None:
        """每张生效时段表必须恰好覆盖 00:00–24:00，且互不重叠。

        - all 是默认表（周末等无专属表的日类使用），自身必须完整；
        - workday/holiday 若给出专属时段，则整体替换 all，也必须独立完整覆盖。
        同一物理时段在节假日只是价格不同时，用 holiday_add_cny 加价而非重复定义。
        """

        for day_type in ("all", "workday", "holiday"):
            scoped = [period for period in self.periods if period.day_type == day_type]
            if scoped:
                self._check_class(day_type, scoped)
        if not any(period.day_type == "all" for period in self.periods):
            raise TariffRuleError("必须提供 day_type=all 的默认时段表")

    def periods_for(self, day_class: str) -> tuple[TimePeriod, ...]:
        """返回某日类实际生效的时段表：专属表整体优先，否则用 all。"""

        specific = tuple(p for p in self.periods if p.day_type == day_class)
        if specific:
            return specific
        return tuple(p for p in self.periods if p.day_type == "all")

    @staticmethod
    def _check_class(day_type: str, periods: list[TimePeriod]) -> None:
        # 用分钟位图：跨午夜时段同时占用当日末段与当日首段。
        covered = [False] * 1440
        labels: list[str] = [""] * 1440
        for period in periods:
            label = f"{_minute_text(period.start_minute)}-{_minute_text(period.end_minute)}"
            ranges: list[tuple[int, int]] = (
                [(0, period.end_minute), (period.start_minute, 1440)]
                if period.end_minute <= period.start_minute
                else [(period.start_minute, period.end_minute)]
            )
            for left, right in ranges:
                for minute in range(left, right):
                    if covered[minute]:
                        raise TariffRuleError(
                            f"[{day_type}] 时段 {label} 与时段 {labels[minute]} 在 "
                            f"{_minute_text(minute)} 重叠"
                        )
                    covered[minute] = True
                    labels[minute] = label
        missing = next((minute for minute, flag in enumerate(covered) if not flag), None)
        if missing is not None:
            raise TariffRuleError(f"[{day_type}] 时段在 {_minute_text(missing)} 前后存在未覆盖空档")


def _period_sort_key(period: TimePeriod) -> tuple[str, int, int]:
    return (period.day_type, period.start_minute, period.end_minute)


@dataclass(frozen=True, slots=True)
class UtcInterval:
    """本地规则日内的一段半开区间 [start_utc, end_utc)，允许跨午夜。"""

    start_utc: datetime
    end_utc: datetime
    period: TimePeriod

    def minutes(self) -> int:
        return int((self.end_utc - self.start_utc).total_seconds() // 60)


def day_intervals(rule: TariffRule, local_day: date, day_class: str) -> list[UtcInterval]:
    """把规则日的时段展开成 UTC 区间。

    跨午夜的时段从当日起点向前翻卷（start 在前一日历日）、终点向后翻卷。
    每个端点单独本地化，因此在夏令时切换日也能得到正确的 UTC 时刻。
    """

    zone = rule.zone()
    intervals: list[UtcInterval] = []
    for period in rule.periods_for(day_class):

        def localize(day: date, minute: int) -> datetime:
            if minute == 1440:
                day = day + timedelta(days=1)
                minute = 0
            return datetime.combine(day, time(minute // 60, minute % 60), tzinfo=zone).astimezone(timezone.utc)

        start_day = local_day
        start_minute = period.start_minute
        end_day = local_day
        end_minute = period.end_minute
        if end_minute <= start_minute:  # 跨午夜：22:00-06:00 → 前一日 22:00 至当日 06:00
            start_day = local_day - timedelta(days=1)
        intervals.append(
            UtcInterval(
                start_utc=localize(start_day, start_minute),
                end_utc=localize(end_day, end_minute),
                period=period,
            )
        )
    return sorted(intervals, key=lambda item: item.start_utc)


@dataclass(frozen=True, slots=True)
class ConsumptionSlice:
    start_utc: str
    end_utc: str
    meter_kwh: Decimal


def canonical_decimal_text(value: Decimal) -> str:
    """把数值上相等的 Decimal 统一成相同文本（2400 与 2400.0 视为同一输入）。"""

    normalized = value.normalize()
    if normalized == 0:
        normalized = Decimal(0)
    return format(normalized, "f")


def parse_consumption(raw: object) -> tuple[ConsumptionSlice, ...]:
    """解析电量输入，按 UTC 排序并校验半开区间互不重叠。"""

    if not isinstance(raw, list) or not raw:
        raise ValidationFailed("consumption 必须是非空数组")
    slices: list[ConsumptionSlice] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationFailed(f"consumption[{index}] 必须是对象")
        start_text = str(item.get("start_utc", ""))
        end_text = str(item.get("end_utc", ""))
        try:
            start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationFailed(f"consumption[{index}] 时间必须是带时区的 ISO 8601") from exc
        if start.tzinfo is None or end.tzinfo is None:
            raise ValidationFailed(f"consumption[{index}] 时间必须包含时区")
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if start.second != 0 or start.microsecond != 0 or end.second != 0 or end.microsecond != 0:
            raise ValidationFailed(f"consumption[{index}] 时间必须对齐到整分钟")
        if end <= start:
            raise ValidationFailed(f"consumption[{index}] 结束必须晚于开始")
        try:
            meter = Decimal(str(item.get("meter_kwh")))
        except Exception as exc:
            raise ValidationFailed(f"consumption[{index}].meter_kwh 必须是十进制数值") from exc
        if not meter.is_finite() or meter < 0:
            raise ValidationFailed(f"consumption[{index}].meter_kwh 必须是非负有限数值")
        meter_text = canonical_decimal_text(meter)
        slices.append(
            ConsumptionSlice(
                start_utc=start.isoformat().replace("+00:00", "Z"),
                end_utc=end.isoformat().replace("+00:00", "Z"),
                meter_kwh=Decimal(meter_text),
            )
        )
    slices.sort(key=lambda item: (item.start_utc, item.end_utc))
    for previous, current in zip(slices, slices[1:]):
        if current.start_utc < previous.end_utc:
            raise ValidationFailed("电量输入时段存在重叠")
    return tuple(slices)


def _period_unit_price(period: TimePeriod, is_holiday: bool) -> Decimal:
    price = period.price_cny + (period.holiday_add if is_holiday else Decimal(0))
    if period.deduct_kind == "absolute":
        price -= period.deduct_value
    elif period.deduct_kind == "percent" and period.deduct_value:
        price *= Decimal(1) - period.deduct_value / Decimal(100)
    return price


def price_bill(
    rule: TariffRule,
    settlement_day: date,
    consumption: tuple[ConsumptionSlice, ...],
) -> dict[str, object]:
    """按规则快照对规则日（本地）的电量计费，逐段输出舍入过程。

    采用时长权重法：单个电量段内按与各时段区间重叠分钟比例分配电量，
    均匀分摊不依赖表计粒度，临界分钟归属由半开区间的重叠长度决定。
    """

    day_class = rule.day_class(settlement_day)
    is_holiday = day_class == "holiday"
    intervals = day_intervals(rule, settlement_day, day_class)
    window_start = min(item.start_utc for item in intervals)
    window_end = max(item.end_utc for item in intervals)

    lines: list[dict[str, object]] = []
    total_amount = Decimal(0)
    total_kwh = Decimal(0)
    coverage_minutes = 0
    for consumed in consumption:
        c_start = datetime.fromisoformat(consumed.start_utc.replace("Z", "+00:00"))
        c_end = datetime.fromisoformat(consumed.end_utc.replace("Z", "+00:00"))
        c_minutes = int((c_end - c_start).total_seconds() // 60)
        if c_minutes <= 0:
            continue
        overlap_total = 0
        allocations: list[tuple[UtcInterval, int, Decimal]] = []
        for interval in intervals:
            overlap = int((min(c_end, interval.end_utc) - max(c_start, interval.start_utc)).total_seconds() // 60)
            if overlap <= 0:
                continue
            overlap_total += overlap
            allocated_kwh = consumed.meter_kwh * Decimal(overlap) / Decimal(c_minutes)
            allocations.append((interval, overlap, allocated_kwh))
        if overlap_total != c_minutes:
            raise ValidationFailed(
                f"电量段 {consumed.start_utc}–{consumed.end_utc} 落在 {settlement_day.isoformat()} "
                f"规则日范围（{_utc(window_start)}–{_utc(window_end)}）之外"
            )
        coverage_minutes += overlap_total
        for interval, overlap, allocated in allocations:
            unit_price = _period_unit_price(interval.period, is_holiday)
            if unit_price < 0:
                raise ValidationFailed("计费结果出现负电价，请检查节假日加价与减免配置")
            raw_amount = allocated * unit_price
            rounded_kwh = allocated.quantize(VOLUME_QUANTUM, rounding=ROUND_HALF_UP)
            rounded_amount = raw_amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            total_amount += rounded_amount
            total_kwh += rounded_kwh
            lines.append({
                "kind": interval.period.kind,
                "day_type": interval.period.day_type,
                "start_utc": _utc(max(c_start, interval.start_utc)),
                "end_utc": _utc(min(c_end, interval.end_utc)),
                "overlap_minutes": overlap,
                "unit_price_cny_per_kwh": decimal_text(unit_price),
                "allocated_kwh_raw": format(allocated, "f"),
                "allocated_kwh": decimal_text(rounded_kwh),
                "amount_raw": format(raw_amount, "f"),
                "amount_cny": decimal_text(rounded_amount),
            })
    expected_window_minutes = int((window_end - window_start).total_seconds() // 60)
    if coverage_minutes != expected_window_minutes:
        raise ValidationFailed(
            f"电量未完整覆盖规则日：覆盖 {coverage_minutes} 分钟，应为 {expected_window_minutes} 分钟"
        )

    total_amount = total_amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    total_kwh = total_kwh.quantize(VOLUME_QUANTUM, rounding=ROUND_HALF_UP)
    input_meter = sum((item.meter_kwh for item in consumption), Decimal(0))
    return {
        "settlement_date": settlement_day.isoformat(),
        "timezone": rule.timezone_name,
        "day_class": day_class,
        "is_holiday": is_holiday,
        "window_start_utc": _utc(window_start),
        "window_end_utc": _utc(window_end),
        "input_meter_kwh": format(input_meter, "f"),
        "total_kwh": decimal_text(total_kwh),
        "total_amount_cny": decimal_text(total_amount),
        "rounding": {
            "mode": "ROUND_HALF_UP",
            "volume_quantum": "0.001",
            "money_quantum": "0.01",
            "line_note": "每段先保留 allocated_kwh_raw/amount_raw，再按分量分别四舍五入；合计再做一次分位舍入",
        },
        "lines": lines,
    }


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def bill_fingerprint(
    rule_sha256: str,
    settlement_date: str,
    consumption: tuple[ConsumptionSlice, ...],
) -> str:
    """账单幂等指纹：规则快照 + 日期 + 规范化输入电量。"""

    payload = {
        "rule_sha256": rule_sha256,
        "settlement_date": settlement_date,
        "consumption": [
            {
                "start_utc": item.start_utc,
                "end_utc": item.end_utc,
                "meter_kwh": format(item.meter_kwh, "f"),
            }
            for item in consumption
        ],
    }
    return digest(payload)
