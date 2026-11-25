from models import Candle
from strategies.indicators import adx, atr, ema, macd, rsi


def candles(count: int = 260) -> list[Candle]:
    result = []
    price = 100.0
    for i in range(count):
        change = 0.08 if i % 11 else -0.03
        open_price = price
        price += change
        result.append(Candle(i*60_000,open_price,max(open_price,price)+0.2,
                             min(open_price,price)-0.2,price,1000+i))
    return result


def test_ema_seeds_after_period():
    result = ema([1,2,3,4,5],3)
    assert result[:2] == [None,None]
    assert result[2] == 2
    assert result[3] is not None


def test_rsi_is_bounded():
    values = [10,11,10.5,12,11,13,12.5,14,13,15,14,16,15,17,16,18,17]
    value = rsi(values,14)
    assert value is not None
    assert 0 <= value <= 100


def test_atr_adx_and_macd_return_finite_values():
    data = candles()
    assert atr(data,14) > 0
    strength = adx(data,14)
    assert strength is not None and 0 <= strength <= 100
    current_hist, previous_hist = macd([c.close for c in data])
    assert current_hist is not None
    assert previous_hist is not None
