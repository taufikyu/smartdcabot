import os
import time
import json
import sys
import random
from math import floor
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from decimal import Decimal, ROUND_UP, getcontext
getcontext().prec = 18
from datetime import datetime

# ============ CONFIG ============
API_KEY = 'QHeMLNsFIyZifj5TQnb4PXtdWnA38HFCa6nS7ecOJ6hiD7pQquASvqvwtVfXDs7Z'
API_SECRET = 'uWBiv1bUbvhY2xPFzHM3UgbTA1FOP7bfI2MwxUjc8gOu9NnABu0KrN7hyTl4LRBj'

DEBUG = False

delay = 5
PAIR = "DOGEUSDT"
BUDGET_USD = 250
BUY_AMOUNT = 25
DROP_THRESHOLD = 0.01
MAX_LOSS_PERCENT = -15
FEE_RATE = 0.001
TAKE_PROFIT_MARGIN = 0.006 + FEE_RATE
TRAILING_MARGIN = 0.008

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "trade_log.txt")
DATA_FILE = os.path.join(BASE_DIR, "bot.json")

AUTO_STOP_AFTER_SELL = False

client = Client(API_KEY, API_SECRET)

_cached_notional = {}
_cached_step = {}

PRICE_CACHE = {}
PRICE_TTL = 3.0

ACCOUNT_CACHE = {"ts": 0, "account": None, "balances": None}
ACCOUNT_TTL = 30.0

LAST_API_CALL = 0.0
MIN_API_INTERVAL = 0.05

# ============ HELPERS ============
def log_action(action, price=0.0, qty=0.0, profit=0.0, message=""):
    with open(LOG_FILE, 'a') as log:
        log.write("[{}] {} | Price: {} | Qty: {} | Profit: {} | {}\n".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, price, qty, profit, message
        ))
    print("[{}] {} | Price: {} | Qty: {} | Profit: {} | {}\n".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, price, qty, profit, message
        ))

def safe_api_call(func, *args, retries=4, backoff=1.5, **kwargs):
    """Wrapper untuk memanggil API dengan retry/backoff, dan throttle interval."""
    global LAST_API_CALL
    for attempt in range(1, retries+1):
        now = time.time()
        delta = now - LAST_API_CALL
        if delta < MIN_API_INTERVAL:
            time.sleep(MIN_API_INTERVAL - delta)
        try:
            res = func(*args, **kwargs)
            LAST_API_CALL = time.time()
            return res
        except (BinanceAPIException, BinanceRequestException, ConnectionResetError) as e:
            if DEBUG:
                print(f"[DBG] API error attempt {attempt}: {e}")
            if attempt == retries:
                raise
            sleep_for = backoff * (attempt + random.random())
            time.sleep(sleep_for)
        except Exception as e:
            if DEBUG:
                print(f"[DBG] Unexpected API error attempt {attempt}: {e}")
            if attempt == retries:
                raise
            time.sleep(backoff * attempt)
    raise Exception("API call failed after retries")

def get_ticker_price(pair):
    """Ambil ticker price tapi pakai cache agar tidak tiap loop ngetok API."""
    now = time.time()
    cached = PRICE_CACHE.get(pair)
    if cached and (now - cached[1]) < PRICE_TTL:
        return cached[0]
    try:
        data = safe_api_call(client.get_symbol_ticker, symbol=pair)
        price = float(data['price'])
        PRICE_CACHE[pair] = (price, now)
        return price
    except Exception as e:
        if DEBUG:
            print("ERROR get_ticker_price:", e)
        if cached:
            return cached[0]
        return 0.0

def get_account_cached(force=False):
    """Ambil account & balances, cache selama ACCOUNT_TTL detik."""
    now = time.time()
    if not force and ACCOUNT_CACHE['account'] and (now - ACCOUNT_CACHE['ts'] < ACCOUNT_TTL):
        return ACCOUNT_CACHE['account']
    try:
        acc = safe_api_call(client.get_account)
        ACCOUNT_CACHE['account'] = acc
        ACCOUNT_CACHE['balances'] = acc.get('balances', [])
        ACCOUNT_CACHE['ts'] = time.time()
        return acc
    except Exception as e:
        if DEBUG:
            print("ERROR get_account_cached:", e)
        return ACCOUNT_CACHE['account']  # bisa None

def get_balance_from_cache(asset):
    """Return free balance from cached balances; jika tidak ada, return 0."""
    acc = get_account_cached()
    if not acc:
        return 0.0
    for b in acc.get('balances', []):
        if b.get('asset') == asset:
            return float(b.get('free', 0.0))
    return 0.0

def get_notion(symbol=None):
    symbol = symbol or PAIR
    if symbol in _cached_notional:
        return _cached_notional[symbol]
    try:
        info = safe_api_call(client.get_symbol_info, symbol)
        for f in info.get('filters', []):
            if f.get('filterType') in ('NOTIONAL', 'MIN_NOTIONAL'):
                val = float(f.get('minNotional') or f.get('minNotional') or f.get('minNotional', 0))
                _cached_notional[symbol] = val
                return val
    except Exception as e:
        if DEBUG:
            print("ERROR get_notion error:", e)
    return 5.0

def get_step_size(symbol):
    if symbol in _cached_step:
        return _cached_step[symbol]
    try:
        info = safe_api_call(client.get_symbol_info, symbol)
        for f in info.get('filters', []):
            if f.get('filterType') == 'LOT_SIZE':
                val = float(f.get('stepSize'))
                _cached_step[symbol] = val
                return val
    except Exception as e:
        if DEBUG:
            print("ERROR get_step_size error:", e)
    return 0.1

def get_min_qty(symbol):
    try:
        info = safe_api_call(client.get_symbol_info, symbol)
        for f in info.get('filters', []):
            if f.get('filterType') == 'LOT_SIZE':
                return float(f.get('minQty'))
    except Exception as e:
        if DEBUG:
            print("ERROR get_min_qty error:", e)
    return 0.0

def floor_to_step(qty, step):
    if step == 0:
        return float(qty)
    q = Decimal(str(qty))
    s = Decimal(str(step))
    floored = (q // s) * s
    return float(floored.quantize(Decimal('0.00000001')))

def ceil_to_step(qty, step):
    if step == 0:
        return float(qty)
    q = Decimal(str(qty))
    s = Decimal(str(step))
    ceiled = ((q + s - Decimal('1e-16')) // s) * s
    if ceiled < q:
        ceiled += s
    return float(ceiled.quantize(Decimal('0.00000001')))

def required_gross_qty_for_min_net(min_net_qty, step, fee_rate):
    min_net = Decimal(str(min_net_qty))
    gross_needed = min_net / (Decimal('1') - Decimal(str(fee_rate)))
    step_d = Decimal(str(step))
    gross_steps = (gross_needed / step_d).to_integral_value(rounding=ROUND_UP)
    gross_qty = gross_steps * step_d
    return float(gross_qty)

def required_quote_for_gross_qty(gross_qty, price):
    return float(Decimal(str(gross_qty)) * Decimal(str(price)))

def calc_profit(buy_price, sell_price, qty, fee=None):
    if fee is None:
        fee = data["config"]["fee_rate"]
    gross = sell_price * qty
    cost = buy_price * qty
    total_fee = (sell_price + buy_price) * qty * fee
    return gross - cost - total_fee

# ============ Data load/save ============
def save_data(d):
    with open(DATA_FILE + ".bak", 'w') as f:
        json.dump(d, f, indent=4)
    with open(DATA_FILE, 'w') as f:
        json.dump(d, f, indent=4)

def load_data():
    available_usdt = get_balance_from_cache("USDT")
    if BUDGET_USD > available_usdt:
        adjusted_budget = floor(available_usdt)
    else:
        adjusted_budget = BUDGET_USD
        
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            d = json.load(f)
        default_conf = {
            "budget_usd": adjusted_budget,
            "buy_amount": BUY_AMOUNT,
            "drop_threshold": DROP_THRESHOLD,
            "max_loss_percent": MAX_LOSS_PERCENT,
            "fee_rate": FEE_RATE,
            "take_profit_margin": TAKE_PROFIT_MARGIN,
            "trailing_margin": TRAILING_MARGIN,
            "peak_time": int(time.time())
        }
        if "config" not in d:
            d["config"] = default_conf.copy()
        else:
            for k, v in default_conf.items():
                if k not in d["config"]:
                    d["config"][k] = v
        if "lowest_price" not in d:
            d["lowest_price"] = get_ticker_price(PAIR)
        save_data(d)
        return d
    else:
        new_data = {
            "buys": [],
            "budget_left": adjusted_budget,
            "peak_price": get_ticker_price(PAIR),
            "lowest_price": get_ticker_price(PAIR),
            "last_buy_time": 0,
            "config": {
                "budget_usd": adjusted_budget,
                "buy_amount": BUY_AMOUNT,
                "drop_threshold": DROP_THRESHOLD,
                "max_loss_percent": MAX_LOSS_PERCENT,
                "fee_rate": FEE_RATE,
                "take_profit_margin": TAKE_PROFIT_MARGIN,
                "trailing_margin": TRAILING_MARGIN
            }
        }
        save_data(new_data)
        return new_data
        
# ============ Order helpers (DCA) ============
def buy():
    pass
    #global data
    #available = get_balance_from_cache("USDT")
    #
    #if is_dead_market():
    #    if DEBUG:
    #        print("Market dead, skip buy")
    #    return
    #
    #desired_buy_amount = data["config"]["buy_amount"]
    #if available < 0.000001 or data['budget_left'] < 0.000001:
    #    if DEBUG:
    #        print("[DBG BUY] no funds available, skip. available=", available, "budget_left=", data['budget_left'])
    #    return
    #    
    #price = get_ticker_price(PAIR)
    #if price == 0:
    #    if DEBUG:
    #        print("[DBG BUY] price == 0, skip")
    #    return
    #
    #step = get_step_size(PAIR)
    #min_qty = get_min_qty(PAIR)
    #min_notional = get_notion(PAIR)
    #
    #desired_buy_amount = get_adaptive_buy_amount()
    #    
    #min_buy, max_buy = get_dynamic_buy_limits()
    #
    #if min_buy and current_price < min_buy:
    #    if DEBUG:
    #        print("[DBG BUY] below dynamic min_buy {}, skip".format(round(min_buy,5)))
    #    return
    #
    #if max_buy and current_price > max_buy:
    #    if DEBUG:
    #        print("[DBG BUY] above dynamic max_buy {}, skip".format(round(max_buy,5)))
    #    return
    #
    ## ======= NEW: pastikan quote minimal memenuhi min_notional + buffer =======
    #SAFE_BUFFER = max(0.05, min_notional * 0.05)
    #recommended_quote = max(desired_buy_amount, min_notional + SAFE_BUFFER)
    #usable_quote = min(recommended_quote, available, data['budget_left'])
    #
    #if DEBUG:
    #    gross_needed = required_gross_qty_for_min_net(min_qty, step, data["config"]["fee_rate"])
    #    quote_needed_for_gross = required_quote_for_gross_qty(gross_needed, price)
    #    print(f"[DBG BUY] desired={desired_buy_amount:.6f} recommended={recommended_quote:.6f} usable={usable_quote:.6f} price={price:.6f} min_notional={min_notional:.6f} quote_needed_for_gross={quote_needed_for_gross:.6f}")
    #
    #if usable_quote < min_notional:
    #    if DEBUG:
    #        print(f"[DBG BUY] usable_quote {usable_quote:.6f} < min_notional {min_notional:.6f}, skip")
    #    return
    #
    #try:
    #    get_account_cached(force=True)
    #    order = safe_api_call(
    #        client.order_market_buy,
    #        symbol=PAIR,
    #        quoteOrderQty=round(float(usable_quote), 6),
    #        newOrderRespType='FULL'
    #    )
    #
    #    fills = order.get('fills') or []
    #    if not fills:
    #        if DEBUG:
    #            print("[DBG BUY] order filled no fills, skip")
    #        log_action("BUY_FAILED", 0, 0, message="no fills returned")
    #        return
    #
    #    total_cost = sum(float(f['price']) * float(f['qty']) for f in fills)
    #    total_qty = sum(float(f['qty']) for f in fills)
    #    avg_fill_price = total_cost / total_qty if total_qty else 0.0
    #
    #    total_fee_usdt = 0
    #    for f in fills:
    #        commission = float(f['commission'])
    #        asset = f['commissionAsset']
    #        if asset != "USDT":
    #            try:
    #                ticker = client.get_symbol_ticker(symbol=f"{asset}USDT")
    #                fee_price = float(ticker['price'])
    #                total_fee_usdt += commission * fee_price
    #            except:
    #                pass
    #        else:
    #            total_fee_usdt += commission
    #
    #    net_cost = total_cost + total_fee_usdt
    #
    #    data['buys'].append({
    #        "price": avg_fill_price,
    #        "qty": total_qty,
    #    })
    #    
    #    data['budget_left'] -= usable_quote
    #    data['last_buy_time'] = int(time.time())
    #    
    #    data['lowest_price'] = min(data.get('lowest_price', current_price), current_price)
    #    save_data(data)
    #
    #    log_action("BUY", avg_fill_price, total_qty,
    #               message=f"cost={total_cost:.8f} USDT | fee={total_fee_usdt:.8f} | net={net_cost:.8f} | usable_quote={usable_quote:.6f}")
    #
    #    get_account_cached(force=True)
    #    time.sleep(1)
    #
    #except Exception as e:
    #    if DEBUG:
    #        print("[DBG BUY] order_market_buy failed:", e)
    #    log_action("BUY_ERROR", 0, 0, message=str(e))
    #    return

def sell_all(CUT_LOSS=False):
    pass
    #global data
    #try:
    #    get_account_cached(force=True)
    #    live_qty = get_balance_from_cache("DOGE")
    #
    #    avg_price = get_avg_buy()
    #    if avg_price <= 0:
    #        return
    #
    #    price = get_ticker_price(PAIR)
    #    if price == 0:
    #        return
    #
    #    step = get_step_size(PAIR)
    #    min_qty = get_min_qty(PAIR)
    #    min_notional = get_notion(PAIR)
    #
    #    qty = floor_to_step(live_qty, step)
    #    notional = price * qty
    #
    #    if qty < min_qty:
    #        return
    #    if notional < min_notional:
    #        return
    #    
    #    if not CUT_LOSS:
    #        profit = calc_profit(avg_price, price, qty)
    #        if profit < 0:
    #            return
    #
    #    order = safe_api_call(
    #        client.order_market_sell,
    #        symbol=PAIR,
    #        quantity=qty,
    #        newOrderRespType='FULL'  # tambahkan ini!
    #    )
    #    fills = order.get('fills', [])
    #    if fills:
    #        total_sell = sum(float(f['price']) * float(f['qty']) for f in fills)
    #        total_qty = sum(float(f['qty']) for f in fills)
    #        avg_sell_price = total_sell / total_qty if total_qty else 0
    #
    #        total_fee_usdt = 0
    #        for f in fills:
    #            commission = float(f['commission'])
    #            asset = f['commissionAsset']
    #            if asset != "USDT":
    #                try:
    #                    ticker = client.get_symbol_ticker(symbol=f"{asset}USDT")
    #                    fee_price = float(ticker['price'])
    #                    total_fee_usdt += commission * fee_price
    #                except:
    #                    pass
    #            else:
    #                total_fee_usdt += commission
    #
    #        total_buy_cost = 0
    #        total_buy_qty = 0
    #        total_buy_fee_usdt = 0
    #
    #        for b in data["buys"]:
    #            b_price = float(b["price"])
    #            b_qty = float(b["qty"])
    #            b_fee = b_price * b_qty * data["config"]["fee_rate"]
    #            total_buy_cost += b_price * b_qty
    #            total_buy_qty += b_qty
    #            total_buy_fee_usdt += b_fee
    #
    #        avg_buy_price = total_buy_cost / total_buy_qty if total_buy_qty else 0
    #        profit_real = (avg_sell_price - avg_buy_price) * total_buy_qty - (total_buy_fee_usdt + total_fee_usdt)
    #
    #        if DEBUG:
    #            print("\n[SELL BREAKDOWN]")
    #            for b in data["buys"]:
    #                layer_profit = (avg_sell_price - b["price"]) * b["qty"]
    #                print(f"  Layer @ {b['price']:.5f} | Qty: {b['qty']} | Profit: {layer_profit:.6f}")
    #            print(f"Total Profit (real): {profit_real:.8f}\n")
    #
    #    else:
    #        avg_sell_price = get_ticker_price(PAIR)
    #        profit_real = calc_profit(avg_price, avg_sell_price, qty)
    #
    #    log_action("SELL", avg_sell_price, qty, f"{profit_real:.8f}")
    #
    #    get_account_cached(force=True)
    #    available_usdt = get_balance_from_cache("USDT")
    #    
    #    new_budget = min(available_usdt, BUDGET_USD)
    #    data = {
    #        "buys": [],
    #        "budget_left": floor(new_budget),
    #        "peak_price": get_ticker_price(PAIR),
    #        "lowest_price": get_ticker_price(PAIR),
    #        "peak_time": int(time.time()),
    #        "last_buy_time": 0,
    #        "config": {
    #            "budget_usd": floor(new_budget),
    #            "buy_amount": BUY_AMOUNT,
    #            "drop_threshold": DROP_THRESHOLD,
    #            "max_loss_percent": MAX_LOSS_PERCENT,
    #            "fee_rate": FEE_RATE,
    #            "take_profit_margin": TAKE_PROFIT_MARGIN,
    #            "trailing_margin": TRAILING_MARGIN
    #        }
    #    }
    #    save_data(data)
    #
    #    if AUTO_STOP_AFTER_SELL:
    #        sys.exit()
    #
    #    get_account_cached(force=True)
    #    time.sleep(1)
    #
    #except Exception as e:
    #    if DEBUG:
    #        print("ERROR SELL gagal: {} ".format(str(e)))

def get_avg_buy():
    total_qty = sum([b['qty'] for b in data['buys']])
    if total_qty == 0:
        return 0
    total_cost = sum([b['price'] * b['qty'] for b in data['buys']])
    return total_cost / total_qty

def get_dynamic_drop_threshold():
    return data["config"]["drop_threshold"] + (len(data['buys']) * 0.002)

def get_dynamic_cooldown_secs():

    peak = data['peak_price']
    low = data['lowest_price']

    if peak <= 0 or low <= 0:
        return 60 * 60

    volatility = (peak - low) / peak

    base = 15 * 60

    if volatility > 0.20:
        return base * 4

    if volatility > 0.10:
        return base * 2

    return base

def is_fund_exhausted():
    if data['budget_left'] < data['config']['buy_amount']:
        if DEBUG:
            print('FUND EXHAUSTED')
        return True
    return False
    
def get_total_usdt_value_cached(pair=None):
    """
    Kalkulasi total USDT dari cached account & price caches.
    Jika butuh akurasi sempurna, panggil force refresh account + price (satu kali).
    """
    acc = get_account_cached()
    if not acc:
        return 0.0
    total = 0.0
    balances = acc.get('balances', [])
    for b in balances:
        asset = b.get('asset')
        free = float(b.get('free', 0.0))
        if free <= 0:
            continue
        if asset == 'USDT':
            total += free
        else:
            if not pair:
                pair = asset + "USDT"
            price = PRICE_CACHE.get(pair, (None, 0))[0]
            if price is None:
                try:
                    tick = safe_api_call(client.get_symbol_ticker, symbol=pair)
                    price = float(tick['price'])
                    PRICE_CACHE[pair] = (price, time.time())
                except Exception:
                    price = 0.0
            total += free * price
    return round(total, 4)
    
def get_dynamic_buy_limits():

    peak = data['peak_price']
    low = data['lowest_price']

    if peak <= 0 or low <= 0: return None, None

    range_size = peak - low

    if range_size <= 0:
        return None, None

    if len(data['buys']) == 0:
        min_buy = None # Izinkan beli di harga berapa pun selama drop terpenuhi
        max_buy = low + (range_size * 0.80) 
    else:
        min_buy = low + (range_size * 0.15)
        max_buy = low + (range_size * 0.50)
    
    if current_price <= low * 1.10:
        min_buy = None

    return min_buy, max_buy
    
def is_market_dumping():

    peak = data['peak_price']
    if peak <= 0:
        return False

    drop_from_peak = (peak - current_price) / peak

    return drop_from_peak > 0.03
    
def get_adaptive_buy_amount():

    total_layers = len(data['buys'])
    base = data["config"]["buy_amount"]
    return base

    if total_layers <= 1:
        return base

    if total_layers <= 3:
        return base * 0.8

    if total_layers <= 5:
        return base * 0.6

    return base * 0.5
    
def is_rebounding():

    low = data['lowest_price']
    if low <= 0:
        return False

    rebound = (current_price - low) / low

    return rebound > 0.003

def is_sideways_market():

    if len(data['buys']) > 0:
        return False

    peak = data['peak_price']
    low = data['lowest_price']

    if peak <= 0 or low <= 0:
        return False

    volatility = (peak - low) / peak

    return volatility < 0.015
    
def is_dead_market():
    high = data["peak_price"]
    low = data["lowest_price"]

    if high <= 0 or low <= 0:
        return False

    volatility = (high - low) / high

    return volatility < 0.008
    
def display_status():
    clear_screen()
    avg_price = get_avg_buy()
    diff = 0 if avg_price == 0 else round((current_price - avg_price) / avg_price * 100, 3)
    total_doge = sum([b['qty'] for b in data['buys']])
    total_usdt = get_total_usdt_value_cached(PAIR)
    fee_rate = data["config"]["fee_rate"]
    total_fee_est = (total_doge * avg_price * fee_rate) + (total_doge * current_price * fee_rate)
    profit = round(((total_doge * current_price) - (total_doge * avg_price)) - total_fee_est, 5)
    
    if data.get('last_buy_time'):
        last_buy_time = datetime.fromtimestamp(data['last_buy_time']).strftime("%d-%m-%Y %H:%M:%S")
    else:
        last_buy_time = "-"

    drop_percent = ((data['peak_price'] - current_price) * 100) / data['peak_price']
    now = datetime.now()
    print("\n=== Smart DCA Bot Running (API-light mode) ===")
    print(f"""
{now.strftime('%d-%m-%Y %H:%M:%S')}
[STATUS] Budget: {round(data['budget_left'],5)} | Current: {current_price} |
AVG Buy: {round(avg_price,5)} | Diff {diff}% | Profit {profit} |
Peak: {data['peak_price']} | Low: {data['lowest_price']} | 
Buys: {[f"{round(b['price'],5)}({round(b['qty'],0)})" for b in data['buys']]} |
Total DOGE (recorded): {total_doge:.6f} |
Total USDT : {total_usdt} |
DROP: {round(drop_percent,5)} |
Last Buy Time: {last_buy_time}
""")

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')
    
# ============ MAIN LOOP ============
data = load_data()
current_price = get_ticker_price(PAIR)
if current_price > 0:
    display_status()
    
while True:
    try:
        current_price = get_ticker_price(PAIR)
        if current_price == 0:
            if DEBUG:
                print("CURRENT PRICE = 0")
            time.sleep(delay)
            continue
        drop_percent = ((data['peak_price'] - current_price) * 100) / data['peak_price']

        if not data.get('peak_price'):
            data['peak_price'] = current_price
            save_data(data)
        if current_price > data['peak_price']:
            data['peak_price'] = current_price
            data['peak_time'] = int(time.time())
            save_data(data)
        if current_price < data.get("lowest_price", current_price):
            data["lowest_price"] = current_price
            save_data(data)

        avg_price = get_avg_buy()
        diff = 0 if avg_price == 0 else round((current_price - avg_price) / avg_price * 100, 3)

        if data.get('last_buy_time'):
            dt_buy_time = datetime.fromtimestamp(data['last_buy_time'])
            last_buy_time = dt_buy_time.strftime("%d-%m-%Y %H:%M:%S")
        else:
            last_buy_time = "-"
        
        now = datetime.now()
        if now.minute % 5 == 0 and now.second < 5:
            display_status()

        free_usdt = get_balance_from_cache("USDT")
        min_notional = get_notion(PAIR)
        max_allowed_buys = int(floor(data["config"]["budget_usd"] / data["config"]["buy_amount"]))

        if avg_price > 0:
           target_price = avg_price * (1 - get_dynamic_drop_threshold())
           if current_price > target_price:
               if DEBUG:
                   print(f"[DBG get_dynamic_drop_threshold BELUM CUKUP] current={current_price:.5f} target={target_price:.5f}")
            
        if avg_price and is_fund_exhausted() and diff < data['config']['max_loss_percent'] :
            log_action("CUT LOSS", current_price, message="Cut loss hit")
            if DEBUG:
                    print("[DBG] CUT LOSS, {}".format(current_price))
            sell_all(CUT_LOSS=True)
        elif data['budget_left'] >= data["config"]["buy_amount"]\
            and free_usdt >= min_notional \
            and (avg_price == 0 or current_price <= avg_price * (1 - get_dynamic_drop_threshold())):
                
            if is_market_dumping() and len(data['buys']) > 0:
                if DEBUG:
                    print("[DBG] market dumping, pause buy")
                time.sleep(60)
                continue
                
            if not is_rebounding() and len(data['buys']) == 0:
                if DEBUG:
                    print("[DBG] market not rebound, pause buy")
                time.sleep(delay)
                continue
            
            now = int(time.time())
            
            if is_sideways_market() and len(data['buys']) > 0:
                if DEBUG:
                    print("[DBG] Market sideways, skip buy")
                time.sleep(10)
                continue

            cooldown_secs = get_dynamic_cooldown_secs()

            if len(data['buys']) > 0 and len(data['buys']) <= 3 and drop_percent > 50:
                buy()
                time.sleep(delay)
                continue

            if len(data['buys']) >= 3 and diff > -5:
                if DEBUG:
                    print("[DBG] Skip buy - layer 3 guard, drop < 5%")
                time.sleep(delay)
                continue

            if now - data.get('last_buy_time', 0) < cooldown_secs:
                if DEBUG:
                    remaining = cooldown_secs - (now - data.get('last_buy_time', 0))
                    print(f"[DBG] cooling down, remain {remaining}s")
                time.sleep(delay)
                continue

            buy()

        elif avg_price and current_price >= \
            avg_price * (1 + (data["config"]["take_profit_margin"] + (len(data['buys']) * 0.0005))):
            sell_all()
        
        elif avg_price and current_price <= data['peak_price'] * \
            (1 - (data["config"]["trailing_margin"] + (len(data['buys']) * 0.0003))) and current_price >= avg_price * (1 + data["config"]["take_profit_margin"]):
            step = get_step_size(PAIR)
            qty = floor_to_step(sum([b['qty'] for b in data['buys']]), step)
            profit = calc_profit(avg_price, current_price, qty)
            if profit > 0:
                sell_all()

        time.sleep(5)
    except Exception as e:
        if DEBUG:
            print("ERROR Main loop error: {}".format(str(e)))
        time.sleep(5)
