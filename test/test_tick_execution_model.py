from datetime import datetime

from vnpy.trader.constant import Direction, Offset, Exchange
from vnpy.trader.object import OrderData

from vnpy_ctastrategy.back_modules import TickExecutionModel
from vnpy_ctastrategy.order_flow.tick_replay_store import TickExecutionContext
from vnpy_ctastrategy.order_flow.friction import FillMode, MatchBehavior
from vnpy_ctastrategy.base import StopOrder


def _make_order(price, volume, direction=Direction.LONG):
    o = OrderData(
        symbol="rb",
        exchange=Exchange.SHFE,
        orderid="1",
        direction=direction,
        offset=Offset.OPEN,
        price=price,
        volume=volume,
        gateway_name="TEST",
    )
    o.traded = 0.0
    return o


def _make_ctx(
    bid1=3000,
    ask1=3001,
    bid_vol=10,
    ask_vol=10,
    last=3000,
    delta=20,
):
    return TickExecutionContext(
        bid1=bid1,
        ask1=ask1,
        bid_vol_1=bid_vol,
        ask_vol_1=ask_vol,
        last_price=last,
        delta_volume=delta,
        spread=ask1 - bid1 if bid1 > 0 and ask1 > 0 else 0,
        mid_price=(bid1 + ask1) / 2 if bid1 > 0 and ask1 > 0 else last,
        dt=datetime.now(),
    )


def test_aggressive_full_when_volume_fits():
    model = TickExecutionModel()
    order = _make_order(price=3005, volume=8)
    ctx = _make_ctx(ask_vol=10)
    r = model.match(order, ctx, FillMode.TOP_OF_BOOK_CAPPED)
    assert r.matched
    assert r.fill_volume == 8
    assert r.remaining_volume == 0
    assert r.behavior == MatchBehavior.AGGRESSIVE_LIMIT


def test_aggressive_capped_when_thin_book():
    model = TickExecutionModel()
    order = _make_order(price=3005, volume=15)
    ctx = _make_ctx(ask_vol=10)
    r = model.match(order, ctx, FillMode.TOP_OF_BOOK_CAPPED)
    assert r.matched
    assert r.fill_volume == 10
    assert r.remaining_volume == 5
    assert r.behavior == MatchBehavior.AGGRESSIVE_LIMIT


def test_passive_touch_by_participation_even_in_top_of_book_mode():
    model = TickExecutionModel()
    order = _make_order(price=3000, volume=20)
    ctx = _make_ctx(last=2999, delta=30)
    r = model.match(order, ctx, FillMode.TOP_OF_BOOK_CAPPED, participation_rate=0.3)
    assert r.matched
    assert abs(r.fill_volume - 9.0) < 1e-8
    assert abs(r.remaining_volume - 11.0) < 1e-8
    assert r.behavior == MatchBehavior.PASSIVE_LIMIT


def test_no_fill_when_price_not_reached():
    model = TickExecutionModel()
    order = _make_order(price=2990, volume=10)
    ctx = _make_ctx(last=3000)
    r = model.match(order, ctx, FillMode.TOP_OF_BOOK_CAPPED)
    assert not r.matched
    assert r.fill_volume == 0
    assert r.remaining_volume == 10


def test_stop_triggered_by_last_price_and_capped_by_book():
    model = TickExecutionModel()
    stop = StopOrder(
        vt_symbol="rb888.SHFE",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        price=3000,
        volume=8,
        stop_orderid="STOP_1",
        strategy_name="test",
        datetime=datetime.now(),
    )
    ctx = _make_ctx(last=3005, ask1=3002, ask_vol=5)
    r = model.match_stop(stop, ctx, FillMode.TOP_OF_BOOK_CAPPED)
    assert r.matched
    assert r.match_price == 3002
    assert r.fill_volume == 5
    assert r.remaining_volume == 3
    assert r.behavior == MatchBehavior.STOP_TRIGGERED


def test_stop_not_triggered():
    model = TickExecutionModel()
    stop = StopOrder(
        vt_symbol="rb888.SHFE",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        price=3010,
        volume=8,
        stop_orderid="STOP_2",
        strategy_name="test",
        datetime=datetime.now(),
    )
    ctx = _make_ctx(last=3000)
    r = model.match_stop(stop, ctx, FillMode.TOP_OF_BOOK_CAPPED)
    assert not r.matched
    assert r.remaining_volume == 8


def test_synthetic_aggressive_ignores_original_limit_price():
    model = TickExecutionModel()
    order = _make_order(price=3001, volume=6)
    order.synthetic_aggressive = True

    # ask1 已上移到 3005；普通限价买单不会主动成交，但 synthetic aggressive 应继续吃当前 ask1。
    ctx = _make_ctx(ask1=3005, ask_vol=4, last=3005)
    r = model.match(order, ctx, FillMode.TOP_OF_BOOK_CAPPED)

    assert r.matched
    assert r.match_price == 3005
    assert r.fill_volume == 4
    assert r.remaining_volume == 2
    assert r.behavior == MatchBehavior.AGGRESSIVE_LIMIT
