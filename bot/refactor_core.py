import sys
import time

new_core = '''def check_buy_preconditions(pair, data, current_price):
    now_ts = time.time()
    if now_ts - data.get('last_buy_time', 0) < MIN_BUY_INTERVAL:
        if DEBUG: print("[DBG] BUY ditolak - kena cooldown internal")
        return False
    if is_dead_market(pair):
        if DEBUG: print("Market dead, skip buy")
        return False
    if current_price == 0:
        if DEBUG: print("[DBG BUY] price == 0, skip")
        return False
        
    min_buy, max_buy = get_dynamic_buy_limits(pair)
    if min_buy and current_price < min_buy:
        if DEBUG: print(f"[DBG BUY] below dynamic min_buy {fmt(min_buy)}, skip")
        return False
    if max_buy and current_price > max_buy:
        if DEBUG: print(f"[DBG BUY] above dynamic max_buy {fmt(max_buy)}, skip")
        return False
        
    available = get_balance_from_cache("USDT")
    if available < 0.000001 or data['budget_left'] < 0.000001:
        if DEBUG: print("[DBG BUY] no funds available, skip. available=", available, "budget_left=", data['budget_left'])
        return False
        
    return True

def calculate_usable_buy_quote(pair, data, current_price):
    available = get_balance_from_cache("USDT")
    min_notional = get_notion(pair)
    desired_buy_amount = get_adaptive_buy_amount(pair)
    
    SAFE_BUFFER = max(0.05, min_notional * 0.05)
    min_required = min_notional + SAFE_BUFFER
    
    if desired_buy_amount < min_required:
        if DEBUG: print(f"[DBG BUY] desired {desired_buy_amount:.6f} < min_required {min_required:.6f}, abort!")
        return 0
        
    usable_quote = min(desired_buy_amount, available, data['budget_left'])
    
    if DEBUG:
        step = get_step_size(pair)
        min_qty = get_min_qty(pair)
        gross_needed = required_gross_qty_for_min_net(min_qty, step, data["config"]["fee_rate"])
        quote_needed_for_gross = required_quote_for_gross_qty(gross_needed, current_price)
        print(f"[DBG BUY] desired={desired_buy_amount:.6f} usable={usable_quote:.6f} price={current_price:.6f} min_notional={min_notional:.6f} quote_needed_for_gross={quote_needed_for_gross:.6f}")
    
    if usable_quote < min_notional:
        if DEBUG: print(f"[DBG BUY] usable_quote {usable_quote:.6f} < min_notional {min_notional:.6f}, skip")
        return 0
        
    return usable_quote

def execute_buy_order(pair, usable_quote):
    get_account_cached(force=True)
    order = safe_api_call(
        client.order_market_buy,
        symbol=pair,
        quoteOrderQty=round(float(usable_quote), 6),
        newOrderRespType='FULL'
    )
    return order.get('fills', []) if order else []

def process_buy_fills_and_update_state(pair, data, fills, usable_quote, current_price):
    total_cost = sum(float(f['price']) * float(f['qty']) for f in fills)
    total_qty = sum(float(f['qty']) for f in fills)
    avg_fill_price = total_cost / total_qty if total_qty else 0.0

    total_fee_usdt = 0
    for f in fills:
        commission = float(f['commission'])
        asset = f['commissionAsset']
        if asset != "USDT":
            try:
                ticker = client.get_symbol_ticker(symbol=f"{asset}USDT")
                fee_price = float(ticker['price'])
                total_fee_usdt += commission * fee_price
            except:
                pass
        else:
            total_fee_usdt += commission

    net_cost = total_cost + total_fee_usdt

    data['buys'].append({
        "price": avg_fill_price,
        "qty": total_qty,
    })
    
    data['budget_left'] -= usable_quote
    data['last_buy_time'] = int(time.time())
    data['lowest_price'] = min(data.get('lowest_price', current_price), current_price)
    
    save_data(pair, data)

    log_action(pair, "BUY", avg_fill_price, total_qty,
               message=f"cost={total_cost:.8f} USDT | fee={total_fee_usdt:.8f} | net={net_cost:.8f} | usable_quote={usable_quote:.6f}")

    get_account_cached(force=True)

def buy(pair):
    data = bot_data[pair]
    if is_buying[pair]:
        if DEBUG: print("[DBG] BUY LOCK aktif, skip")
        return
        
    current_price = get_ticker_price(pair)
    if not check_buy_preconditions(pair, data, current_price):
        return
        
    is_buying[pair] = True
    try:
        usable_quote = calculate_usable_buy_quote(pair, data, current_price)
        if usable_quote <= 0:
            return
            
        fills = execute_buy_order(pair, usable_quote)
        if not fills:
            if DEBUG: print("[DBG BUY] order filled no fills, skip")
            log_action(pair, "BUY_FAILED", 0, 0, message="no fills returned")
            return
            
        process_buy_fills_and_update_state(pair, data, fills, usable_quote, current_price)
        time.sleep(1)

    except Exception as e:
        if DEBUG: print("[DBG BUY] order_market_buy failed:", e)
        log_action(pair, "BUY_ERROR", 0, 0, message=str(e))
    finally:
        is_buying[pair] = False

def calculate_sell_qty(pair, current_price):
    get_account_cached(force=True)
    live_qty = get_balance_from_cache(pair.replace("USDT", ""))
    step = get_step_size(pair)
    min_qty = get_min_qty(pair)
    min_notional = get_notion(pair)

    qty = floor_to_step(live_qty, step)
    notional = current_price * qty
    
    return qty, notional, min_qty, min_notional

def process_ghost_trade_reset(pair, data, current_price):
    if DEBUG:
        print("\\n[SYNC] Koin di Binance kosong/receh. Menghapus Ghost Trade (Auto-Reset)!")
    get_account_cached(force=True)
    available_usdt = get_balance_from_cache("USDT")
    new_budget = min(available_usdt, data["config"]["budget_usd"])
    data["buys"] = []
    data["budget_left"] = floor(new_budget)
    data["peak_price"] = current_price
    data["lowest_price"] = current_price
    data["peak_time"] = int(time.time())
    data["last_buy_time"] = 0
    save_data(pair, data)

def execute_sell_order(pair, qty):
    order = safe_api_call(
        client.order_market_sell,
        symbol=pair,
        quantity=qty,
        newOrderRespType='FULL'
    )
    return order.get('fills', []) if order else []

def process_sell_fills_and_update_state(pair, data, fills, qty, current_price, avg_price):
    if fills:
        total_sell = sum(float(f['price']) * float(f['qty']) for f in fills)
        total_qty = sum(float(f['qty']) for f in fills)
        avg_sell_price = total_sell / total_qty if total_qty else 0

        total_fee_usdt = 0
        for f in fills:
            commission = float(f['commission'])
            asset = f['commissionAsset']
            if asset != "USDT":
                try:
                    ticker = client.get_symbol_ticker(symbol=f"{asset}USDT")
                    fee_price = float(ticker['price'])
                    total_fee_usdt += commission * fee_price
                except:
                    pass
            else:
                total_fee_usdt += commission

        total_buy_cost = 0
        total_buy_qty = 0
        total_buy_fee_usdt = 0

        for b in data["buys"]:
            b_price = float(b["price"])
            b_qty = float(b["qty"])
            b_fee = b_price * b_qty * data["config"]["fee_rate"]
            total_buy_cost += b_price * b_qty
            total_buy_qty += b_qty
            total_buy_fee_usdt += b_fee

        avg_buy_price = total_buy_cost / total_buy_qty if total_buy_qty else 0
        profit_real = (avg_sell_price - avg_buy_price) * total_buy_qty - (total_buy_fee_usdt + total_fee_usdt)

        if DEBUG:
            print("\\n[SELL BREAKDOWN]")
            for b in data["buys"]:
                layer_profit = (avg_sell_price - float(b["price"])) * float(b["qty"])
                print(f"  Layer @ {float(b['price']):.8f} | Qty: {float(b['qty']):.8f} | Profit: {layer_profit:.6f}")
            print(f"Total Profit (real): {profit_real:.8f}\\n")

    else:
        avg_sell_price = get_ticker_price(pair)
        profit_real = calc_profit(pair, avg_price, avg_sell_price, qty)

    log_action(pair, "SELL", avg_sell_price, qty, f"{profit_real:.8f}")
    
    get_account_cached(force=True)
    available_usdt = get_balance_from_cache("USDT")
    new_budget = min(available_usdt, data["config"]["budget_usd"])
    data["buys"] = []
    data["budget_left"] = floor(new_budget)
    data["peak_price"] = get_ticker_price(pair)
    data["lowest_price"] = get_ticker_price(pair)
    data["peak_time"] = int(time.time())
    data["last_buy_time"] = 0
    save_data(pair, data)

    get_account_cached(force=True)

def sell_all(pair, CUT_LOSS=False):
    data = bot_data[pair]
    current_price = get_ticker_price(pair)
    if current_price == 0:
        return
        
    avg_price = get_avg_buy(pair)
    if avg_price <= 0:
        return
        
    try:
        qty, notional, min_qty, min_notional = calculate_sell_qty(pair, current_price)
        
        if qty < min_qty or notional < min_notional:
            process_ghost_trade_reset(pair, data, current_price)
            return

        if not CUT_LOSS:
            profit = calc_profit(pair, avg_price, current_price, qty)
            if profit < 0:
                return

        fills = execute_sell_order(pair, qty)
        process_sell_fills_and_update_state(pair, data, fills, qty, current_price, avg_price)
        time.sleep(1)

    except Exception as e:
        print("ERROR SELL gagal: {} ".format(str(e)))
'''

with open('multibot.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

start_idx = [i for i, l in enumerate(lines) if l.startswith('def buy(')][0]
end_idx = [i for i, l in enumerate(lines) if l.startswith('@app.route(')][0]

lines = lines[:start_idx] + [new_core + '\n'] + lines[end_idx-1:] # line 920 is @app.route, we want to replace up to 919

with open('multibot.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print('Success')
