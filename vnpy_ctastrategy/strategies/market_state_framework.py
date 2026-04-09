from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple
import inspect
import math

import numpy as np

from vnpy_ctastrategy import (
    CtaTemplate,
    TickData,
    BarData,
    TradeData,
    OrderData,
    StopOrder,
    BarGenerator,
    ArrayManager,
)

from .trend.simple_trend import SimpleTrendStrategy
from .range.simple_range import SimpleRangeStrategy
from .defense.simple_defense import SimpleDefenseStrategy

def _safe_build(component_cls, **kwargs):
    try:
        sig = inspect.signature(component_cls)
        accepted = set(sig.parameters.keys())
        filtered = {k: v for k, v in kwargs.items() if k in accepted}
        return component_cls(**filtered)
    except Exception:
        return component_cls(**kwargs)


class MarketState(str, Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    CHAOS = "CHAOS"


@dataclass
class StateMetrics:
    efficiency: float = 0.0
    volatility: float = 0.0
    vol_rank: float = 0.0
    breakout_success_rate: float = 0.0
    breakout_count: int = 0
    adx_value: float = 0.0


@dataclass
class RegimeDecision:
    raw_state: MarketState
    final_state: MarketState
    metrics: StateMetrics
    state_score: Dict[str, float]
    confidence: float
    reason: str


@dataclass
class RoleWeights:
    trend: float
    range: float
    defense: float

    def normalize(self) -> "RoleWeights":
        total = self.trend + self.range + self.defense
        if total <= 0:
            return RoleWeights(0.0, 0.0, 1.0)
        return RoleWeights(self.trend / total, self.range / total, self.defense / total)


class StateClassifier:
    def __init__(
        self,
        regime_window: int = 20,
        breakout_lookback: int = 5,
        breakout_confirm_bars: int = 2,
        vol_rank_lookback: int = 120,
        adx_window: int = 14,
        trend_efficiency_threshold: float = 0.42,
        range_efficiency_threshold: float = 0.22,
        chaos_efficiency_ceiling: float = 0.30,
        trend_breakout_success_threshold: float = 0.55,
        range_breakout_success_threshold: float = 0.35,
        chaos_breakout_success_ceiling: float = 0.45,
        low_vol_rank_threshold: float = 0.45,
        high_vol_rank_threshold: float = 0.72,
        trend_adx_threshold: float = 22.0,
        range_adx_ceiling: float = 18.0,
    ):
        self.regime_window = regime_window
        self.breakout_lookback = breakout_lookback
        self.breakout_confirm_bars = breakout_confirm_bars
        self.vol_rank_lookback = vol_rank_lookback
        self.adx_window = adx_window
        self.trend_efficiency_threshold = trend_efficiency_threshold
        self.range_efficiency_threshold = range_efficiency_threshold
        self.chaos_efficiency_ceiling = chaos_efficiency_ceiling
        self.trend_breakout_success_threshold = trend_breakout_success_threshold
        self.range_breakout_success_threshold = range_breakout_success_threshold
        self.chaos_breakout_success_ceiling = chaos_breakout_success_ceiling
        self.low_vol_rank_threshold = low_vol_rank_threshold
        self.high_vol_rank_threshold = high_vol_rank_threshold
        self.trend_adx_threshold = trend_adx_threshold
        self.range_adx_ceiling = range_adx_ceiling

    def classify(self, am: ArrayManager) -> RegimeDecision:
        closes = np.array(am.close, dtype=float)
        highs = np.array(am.high, dtype=float)
        lows = np.array(am.low, dtype=float)
        metrics = self._calculate_metrics(am, closes, highs, lows)
        raw_state, scores, confidence, reason = self._classify_from_metrics(metrics)
        return RegimeDecision(
            raw_state=raw_state,
            final_state=raw_state,
            metrics=metrics,
            state_score=scores,
            confidence=confidence,
            reason=reason,
        )

    def _calculate_metrics(self, am: ArrayManager, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> StateMetrics:
        n = self.regime_window
        recent_closes = closes[-(n + 1):]
        net_move = abs(recent_closes[-1] - recent_closes[0])
        total_move = float(np.sum(np.abs(np.diff(recent_closes))))
        efficiency = net_move / (total_move + 1e-12)

        atr = self._calculate_atr(highs, lows, closes, n)
        latest_close = max(closes[-1], 1e-12)
        volatility = atr / latest_close

        vol_rank = self._calculate_vol_rank(highs, lows, closes)
        breakout_success_rate, breakout_count = self._calculate_breakout_success(closes)
        adx_value = float(am.adx(self.adx_window)) if am.count >= self.adx_window + 5 else 20.0

        return StateMetrics(
            efficiency=float(efficiency),
            volatility=float(volatility),
            vol_rank=float(vol_rank),
            breakout_success_rate=float(breakout_success_rate),
            breakout_count=int(breakout_count),
            adx_value=adx_value,
        )

    def _calculate_atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
        tr_list: List[float] = []
        start = max(1, len(closes) - period)
        for i in range(start, len(closes)):
            prev_close = closes[i - 1]
            tr = max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close))
            tr_list.append(float(tr))
        return float(np.mean(tr_list)) if tr_list else 0.0

    def _calculate_vol_rank(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> float:
        n = self.regime_window
        lookback = self.vol_rank_lookback
        if len(closes) < lookback + n + 2:
            return 0.5
        vol_values: List[float] = []
        start = len(closes) - lookback - 1
        for end_idx in range(start + n, len(closes)):
            atr = self._calculate_atr(highs[:end_idx], lows[:end_idx], closes[:end_idx], n)
            close_value = max(closes[end_idx - 1], 1e-12)
            vol_values.append(atr / close_value)
        if len(vol_values) <= 1:
            return 0.5
        current = vol_values[-1]
        hist = np.array(vol_values[:-1], dtype=float)
        return float(np.mean(hist <= current))

    def _calculate_breakout_success(self, closes: np.ndarray) -> Tuple[float, int]:
        n = self.regime_window
        lb = self.breakout_lookback
        confirm = self.breakout_confirm_bars
        start_idx = max(lb, len(closes) - n)
        end_idx = len(closes) - confirm
        breakouts = 0
        success = 0
        for i in range(start_idx, end_idx):
            lookback_slice = closes[i - lb:i]
            if len(lookback_slice) < lb:
                continue
            if closes[i] > np.max(lookback_slice):
                breakouts += 1
                if closes[i + confirm] > closes[i]:
                    success += 1
                continue
            if closes[i] < np.min(lookback_slice):
                breakouts += 1
                if closes[i + confirm] < closes[i]:
                    success += 1
                continue
        return (float(success / breakouts), int(breakouts)) if breakouts else (0.0, 0)

    def _classify_from_metrics(self, m: StateMetrics) -> Tuple[MarketState, Dict[str, float], float, str]:
        trend_score = 0.0
        range_score = 0.0
        chaos_score = 0.0

        trend_score += self._scaled_positive(m.efficiency, self.trend_efficiency_threshold, 0.80)
        trend_score += self._scaled_positive(m.breakout_success_rate, self.trend_breakout_success_threshold, 1.0)
        trend_score += self._scaled_positive(m.adx_value, self.trend_adx_threshold, 40.0)
        trend_score += self._scaled_negative(m.vol_rank, 0.92, 0.60)

        range_score += self._scaled_negative(m.efficiency, self.range_efficiency_threshold, 0.05)
        range_score += self._scaled_negative(m.breakout_success_rate, self.range_breakout_success_threshold, 0.0)
        range_score += self._scaled_negative(m.adx_value, self.range_adx_ceiling, 8.0)
        range_score += self._scaled_negative(m.vol_rank, self.low_vol_rank_threshold, 0.15)

        chaos_score += self._scaled_positive(m.vol_rank, self.high_vol_rank_threshold, 1.0)
        chaos_score += self._scaled_negative(m.efficiency, self.chaos_efficiency_ceiling, 0.0)
        chaos_score += self._scaled_negative(m.breakout_success_rate, self.chaos_breakout_success_ceiling, 0.0)

        scores = {
            MarketState.TREND.value: round(trend_score, 6),
            MarketState.RANGE.value: round(range_score, 6),
            MarketState.CHAOS.value: round(chaos_score, 6),
        }

        if (
            m.vol_rank > self.high_vol_rank_threshold
            and m.efficiency < self.chaos_efficiency_ceiling
            and m.breakout_success_rate < self.chaos_breakout_success_ceiling
        ):
            state = MarketState.CHAOS
            reason = f"高波动且方向低效: vol_rank={m.vol_rank:.2f}, eff={m.efficiency:.2f}, breakout={m.breakout_success_rate:.2f}, adx={m.adx_value:.1f}"
        elif (
            m.efficiency > self.trend_efficiency_threshold
            and m.breakout_success_rate > self.trend_breakout_success_threshold
            and m.adx_value >= self.trend_adx_threshold
        ):
            state = MarketState.TREND
            reason = f"趋势延续较强: eff={m.efficiency:.2f}, breakout={m.breakout_success_rate:.2f}, adx={m.adx_value:.1f}"
        else:
            state = MarketState.RANGE
            reason = f"按震荡处理: eff={m.efficiency:.2f}, vol_rank={m.vol_rank:.2f}, breakout={m.breakout_success_rate:.2f}, adx={m.adx_value:.1f}"

        ordered = sorted(scores.values(), reverse=True)
        confidence = max(0.0, min(1.0, ordered[0] - ordered[1] if len(ordered) >= 2 else 0.0))
        return state, scores, confidence, reason

    @staticmethod
    def _scaled_positive(value: float, threshold: float, max_value: float) -> float:
        if value <= threshold:
            return 0.0
        return min(1.0, (value - threshold) / max(1e-12, (max_value - threshold)))

    @staticmethod
    def _scaled_negative(value: float, threshold: float, min_value: float) -> float:
        if value >= threshold:
            return 0.0
        return min(1.0, (threshold - value) / max(1e-12, (threshold - min_value)))


class RoleEngine:
    def __init__(
        self,
        trend_weights: Tuple[float, float, float] = (0.85, 0.10, 0.05),
        range_weights: Tuple[float, float, float] = (0.10, 0.85, 0.05),
        chaos_weights: Tuple[float, float, float] = (0.10, 0.10, 0.80),
    ):
        self.mapping = {
            MarketState.TREND: RoleWeights(*trend_weights).normalize(),
            MarketState.RANGE: RoleWeights(*range_weights).normalize(),
            MarketState.CHAOS: RoleWeights(*chaos_weights).normalize(),
        }

    def get_weights(self, state: MarketState) -> RoleWeights:
        return self.mapping[state]


class MarketStateFrameworkStrategy(CtaTemplate):
    author = "OpenAI"

    bar_window: int = 1
    fixed_size: int = 1
    max_pos: int = 10
    min_rebalance_interval_bars: int = 3

    regime_window: int = 20
    breakout_lookback: int = 5
    breakout_confirm_bars: int = 2
    vol_rank_lookback: int = 120
    regime_adx_window: int = 14

    trend_efficiency_threshold: float = 0.42
    range_efficiency_threshold: float = 0.22
    chaos_efficiency_ceiling: float = 0.30
    trend_breakout_success_threshold: float = 0.55
    range_breakout_success_threshold: float = 0.35
    chaos_breakout_success_ceiling: float = 0.45
    low_vol_rank_threshold: float = 0.45
    high_vol_rank_threshold: float = 0.72
    regime_trend_adx_threshold: float = 22.0
    regime_range_adx_ceiling: float = 18.0

    state_confirm_bars: int = 3
    state_cooldown_bars: int = 8

    trend_trend_weight: float = 0.85
    trend_range_weight: float = 0.10
    trend_defense_weight: float = 0.05
    range_trend_weight: float = 0.10
    range_range_weight: float = 0.85
    range_defense_weight: float = 0.05
    chaos_trend_weight: float = 0.10
    chaos_range_weight: float = 0.10
    chaos_defense_weight: float = 0.80

    target_position_scale: float = 1.0
    chaos_max_position_scale: float = 0.50
    low_confidence_scale: float = 0.5
    confidence_threshold: float = 0.20
    rebalance_tolerance: float = 1.0
    entry_signal_threshold: float = 0.70
    exit_signal_threshold: float = 0.25

    trend_entry_window: int = 40
    trend_exit_window: int = 14
    trend_atr_window: int = 20
    trend_fast_window: int = 20
    trend_slow_window: int = 60
    trend_adx_window: int = 14
    trend_adx_threshold: float = 25.0

    range_boll_window: int = 26
    range_boll_dev: float = 2.4
    range_zscore_entry: float = 2.1
    range_zscore_exit: float = 0.45
    range_adx_window: int = 14
    range_adx_ceiling: float = 17.0
    range_band_width_ceiling: float = 0.03
    range_min_hold_bars: int = 3
    range_reentry_cooldown_bars: int = 2

    defense_enable_probe: bool = False
    defense_fast_window: int = 10
    defense_ma_window: int = 30
    defense_rsi_window: int = 10
    defense_long_rsi_threshold: float = 60.0
    defense_short_rsi_threshold: float = 40.0
    defense_probe_signal: float = 0.25

    parameters = [
        "bar_window", "fixed_size", "max_pos", "min_rebalance_interval_bars",
        "regime_window", "breakout_lookback", "breakout_confirm_bars", "vol_rank_lookback", "regime_adx_window",
        "trend_efficiency_threshold", "range_efficiency_threshold", "chaos_efficiency_ceiling",
        "trend_breakout_success_threshold", "range_breakout_success_threshold", "chaos_breakout_success_ceiling",
        "low_vol_rank_threshold", "high_vol_rank_threshold", "regime_trend_adx_threshold", "regime_range_adx_ceiling",
        "state_confirm_bars", "state_cooldown_bars",
        "trend_trend_weight", "trend_range_weight", "trend_defense_weight",
        "range_trend_weight", "range_range_weight", "range_defense_weight",
        "chaos_trend_weight", "chaos_range_weight", "chaos_defense_weight",
        "target_position_scale", "chaos_max_position_scale", "low_confidence_scale", "confidence_threshold",
        "rebalance_tolerance", "entry_signal_threshold", "exit_signal_threshold",
        "trend_entry_window", "trend_exit_window", "trend_atr_window", "trend_fast_window", "trend_slow_window",
        "trend_adx_window", "trend_adx_threshold",
        "range_boll_window", "range_boll_dev", "range_zscore_entry", "range_zscore_exit",
        "range_adx_window", "range_adx_ceiling", "range_band_width_ceiling", "range_min_hold_bars", "range_reentry_cooldown_bars",
        "defense_enable_probe", "defense_fast_window", "defense_ma_window", "defense_rsi_window",
        "defense_long_rsi_threshold", "defense_short_rsi_threshold", "defense_probe_signal",
    ]

    variables = [
        "market_state", "raw_state", "state_confidence", "state_reason",
        "efficiency", "volatility", "vol_rank", "breakout_success_rate", "regime_adx_value",
        "trend_role_weight", "range_role_weight", "defense_role_weight",
        "trend_signal_value", "range_signal_value", "defense_signal_value",
        "ensemble_signal_value", "target_pos", "last_rebalance_bar_count",
    ]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.bg = BarGenerator(self.on_bar, self.bar_window, self.on_window_bar)
        self.am = ArrayManager(size=800)

        self.classifier = StateClassifier(
            regime_window=self.regime_window,
            breakout_lookback=self.breakout_lookback,
            breakout_confirm_bars=self.breakout_confirm_bars,
            vol_rank_lookback=self.vol_rank_lookback,
            adx_window=self.regime_adx_window,
            trend_efficiency_threshold=self.trend_efficiency_threshold,
            range_efficiency_threshold=self.range_efficiency_threshold,
            chaos_efficiency_ceiling=self.chaos_efficiency_ceiling,
            trend_breakout_success_threshold=self.trend_breakout_success_threshold,
            range_breakout_success_threshold=self.range_breakout_success_threshold,
            chaos_breakout_success_ceiling=self.chaos_breakout_success_ceiling,
            low_vol_rank_threshold=self.low_vol_rank_threshold,
            high_vol_rank_threshold=self.high_vol_rank_threshold,
            trend_adx_threshold=self.regime_trend_adx_threshold,
            range_adx_ceiling=self.regime_range_adx_ceiling,
        )

        self.role_engine = RoleEngine(
            trend_weights=(self.trend_trend_weight, self.trend_range_weight, self.trend_defense_weight),
            range_weights=(self.range_trend_weight, self.range_range_weight, self.range_defense_weight),
            chaos_weights=(self.chaos_trend_weight, self.chaos_range_weight, self.chaos_defense_weight),
        )

        self.trend_module = _safe_build(
            SimpleTrendStrategy,
            entry_window=self.trend_entry_window,
            exit_window=self.trend_exit_window,
            atr_window=self.trend_atr_window,
            fast_window=self.trend_fast_window,
            slow_window=self.trend_slow_window,
            adx_window=self.trend_adx_window,
            adx_trend_threshold=self.trend_adx_threshold,
        )
        self.range_module = _safe_build(
            SimpleRangeStrategy,
            boll_window=self.range_boll_window,
            boll_dev=self.range_boll_dev,
            zscore_entry=self.range_zscore_entry,
            zscore_exit=self.range_zscore_exit,
            adx_window=self.range_adx_window,
            adx_ceiling=self.range_adx_ceiling,
            band_width_ceiling=self.range_band_width_ceiling,
            min_hold_bars=self.range_min_hold_bars,
            reentry_cooldown_bars=self.range_reentry_cooldown_bars,
        )
        self.defense_module = _safe_build(
            SimpleDefenseStrategy,
            enable_probe=self.defense_enable_probe,
            fast_window=self.defense_fast_window,
            slow_window=self.defense_ma_window,
            rsi_window=self.defense_rsi_window,
            long_rsi_threshold=self.defense_long_rsi_threshold,
            short_rsi_threshold=self.defense_short_rsi_threshold,
            probe_signal=self.defense_probe_signal,
        )

        self.market_state: str = MarketState.RANGE.value
        self.raw_state: str = MarketState.RANGE.value
        self.state_confidence: float = 0.0
        self.state_reason: str = ""
        self.efficiency: float = 0.0
        self.volatility: float = 0.0
        self.vol_rank: float = 0.0
        self.breakout_success_rate: float = 0.0
        self.regime_adx_value: float = 0.0

        self.trend_role_weight: float = 0.0
        self.range_role_weight: float = 0.0
        self.defense_role_weight: float = 1.0

        self.trend_signal_value: float = 0.0
        self.range_signal_value: float = 0.0
        self.defense_signal_value: float = 0.0
        self.ensemble_signal_value: float = 0.0

        self.current_confirmed_state: MarketState = MarketState.RANGE
        self.pending_state: Optional[MarketState] = None
        self.pending_state_count: int = 0
        self.state_cooldown_remaining: int = 0

        self.target_pos: int = 0
        self.bar_count: int = 0
        self.last_rebalance_bar_count: int = -999999

    def on_init(self) -> None:
        self.write_log("市场状态框架策略初始化（v5）")
        self.load_bar(250)

    def on_start(self) -> None:
        self.write_log("市场状态框架策略启动")
        self.put_event()

    def on_stop(self) -> None:
        self.write_log("市场状态框架策略停止")
        self.put_event()

    def on_tick(self, tick: TickData) -> None:
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        self.bg.update_bar(bar)

    def on_window_bar(self, bar: BarData) -> None:
        self.cancel_all()
        self.bar_count += 1
        self.am.update_bar(bar)
        if not self.am.inited:
            self.put_event()
            return

        if self.state_cooldown_remaining > 0:
            self.state_cooldown_remaining -= 1

        decision = self.evaluate_market_state()
        role_weights = self.role_engine.get_weights(self.current_confirmed_state)
        adjusted_weights = self.adjust_weights_for_risk(role_weights, decision.confidence)

        self.trend_signal_value = self.clip_signal(self.trend_module.signal(self.am, bar))
        self.range_signal_value = self.clip_signal(self.range_module.signal(self.am, bar))
        self.defense_signal_value = self.clip_signal(self.defense_module.signal(self.am, bar))

        self.trend_role_weight = adjusted_weights.trend
        self.range_role_weight = adjusted_weights.range
        self.defense_role_weight = adjusted_weights.defense

        ensemble_signal = (
            adjusted_weights.trend * self.trend_signal_value
            + adjusted_weights.range * self.range_signal_value
            + adjusted_weights.defense * self.defense_signal_value
        )
        self.ensemble_signal_value = self.clip_signal(ensemble_signal)
        self.target_pos = self.calculate_target_position(self.ensemble_signal_value, self.current_confirmed_state)

        if self.should_rebalance(self.target_pos):
            self.rebalance_to_target(bar, self.target_pos)
            self.last_rebalance_bar_count = self.bar_count

        self.put_event()

    def evaluate_market_state(self) -> RegimeDecision:
        decision = self.classifier.classify(self.am)
        raw_state = decision.raw_state

        self.raw_state = raw_state.value
        self.state_confidence = decision.confidence
        self.state_reason = decision.reason
        self.efficiency = decision.metrics.efficiency
        self.volatility = decision.metrics.volatility
        self.vol_rank = decision.metrics.vol_rank
        self.breakout_success_rate = decision.metrics.breakout_success_rate
        self.regime_adx_value = decision.metrics.adx_value

        final_state = self.apply_state_smoothing_and_cooldown(raw_state)
        decision.final_state = final_state
        self.market_state = final_state.value
        return decision

    def apply_state_smoothing_and_cooldown(self, raw_state: MarketState) -> MarketState:
        if self.state_cooldown_remaining > 0 and raw_state != MarketState.CHAOS:
            return self.current_confirmed_state
        if raw_state == self.current_confirmed_state:
            self.pending_state = None
            self.pending_state_count = 0
            return self.current_confirmed_state

        if self.pending_state != raw_state:
            self.pending_state = raw_state
            self.pending_state_count = 1
        else:
            self.pending_state_count += 1

        required_confirm = 1 if raw_state == MarketState.CHAOS else max(1, self.state_confirm_bars)
        if self.pending_state_count >= required_confirm:
            old_state = self.current_confirmed_state
            self.current_confirmed_state = raw_state
            self.pending_state = None
            self.pending_state_count = 0
            self.state_cooldown_remaining = self.state_cooldown_bars
            self.on_market_state_changed(old_state, self.current_confirmed_state)
            return raw_state

        return self.current_confirmed_state

    def adjust_weights_for_risk(self, weights: RoleWeights, confidence: float) -> RoleWeights:
        trend_w = weights.trend
        range_w = weights.range
        defense_w = weights.defense

        if self.current_confirmed_state == MarketState.CHAOS:
            defense_w = max(defense_w, self.chaos_max_position_scale)
            trend_w = 0.0
            range_w = 0.0
        elif confidence < self.confidence_threshold:
            trend_w *= self.low_confidence_scale
            range_w *= self.low_confidence_scale
            defense_w = max(defense_w, 1.0 - (trend_w + range_w))

        return RoleWeights(trend_w, range_w, defense_w).normalize()

    def calculate_target_position(self, ensemble_signal: float, state: MarketState) -> int:
        if state == MarketState.CHAOS:
            max_pos = max(1, int(round(self.max_pos * self.chaos_max_position_scale)))
        else:
            max_pos = self.max_pos

        if self.pos == 0:
            if abs(ensemble_signal) < self.entry_signal_threshold:
                return 0
        else:
            if abs(ensemble_signal) < self.exit_signal_threshold:
                return 0
            if math.copysign(1, ensemble_signal) != math.copysign(1, self.pos):
                return 0

        raw_target = ensemble_signal * max_pos * self.target_position_scale
        target_pos = int(round(raw_target))
        return max(-max_pos, min(max_pos, target_pos))

    def should_rebalance(self, target_pos: int) -> bool:
        if self.bar_count - self.last_rebalance_bar_count < self.min_rebalance_interval_bars:
            return False
        return abs(target_pos - self.pos) >= int(self.rebalance_tolerance)

    def rebalance_to_target(self, bar: BarData, target_pos: int) -> None:
        diff = target_pos - self.pos
        if diff == 0:
            return

        price_tick = self.get_pricetick() or 1
        atr = float(self.am.atr(20)) if self.am.count >= 25 else price_tick * 5
        offset = max(price_tick, atr * 0.15)

        buy_price = bar.close_price + offset
        sell_price = bar.close_price - offset

        if self.pos > 0 and target_pos <= 0:
            self.sell(sell_price, abs(self.pos))
            return
        if self.pos < 0 and target_pos >= 0:
            self.cover(buy_price, abs(self.pos))
            return
        if diff > 0:
            self.buy(buy_price, diff)
        elif diff < 0:
            self.short(sell_price, abs(diff))

    def on_market_state_changed(self, old_state: MarketState, new_state: MarketState) -> None:
        self.write_log(
            f"状态切换: {old_state.value} → {new_state.value} | "
            f"eff={self.efficiency:.3f} vol_rank={self.vol_rank:.3f} breakout={self.breakout_success_rate:.3f} "
            f"adx={self.regime_adx_value:.1f} conf={self.state_confidence:.3f} "
            f"trend_sig={self.trend_signal_value:.2f} range_sig={self.range_signal_value:.2f}"
        )

    @staticmethod
    def clip_signal(value: float) -> float:
        return max(-1.0, min(1.0, float(value)))

    def on_order(self, order: OrderData) -> None:
        self.put_event()

    def on_trade(self, trade: TradeData) -> None:
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        self.put_event()
