import sqlite3
import time
from binance.client import Client
from datetime import datetime
import os

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

API_KEY = os.getenv('BINANCE_API_KEY', '')
API_SECRET = os.getenv('BINANCE_API_SECRET', '')

Client.API_URL = "https://api.binance.com/api"
client = Client(API_KEY, API_SECRET, requests_params={'timeout': 10})

try:
    server_time = client.get_server_time()
    time_offset = server_time['serverTime'] - int(datetime.now().timestamp() * 1000)
    client.timestamp_offset = time_offset
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot_trading.db")

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
try:
    c.execute("SELECT DISTINCT pair FROM pairs_config UNION SELECT DISTINCT pair FROM trade_history")
    known_pairs = [r[0] for r in c.fetchall() if r[0]]
except Exception:
    known_pairs = []

pairs_to_sync = list(dict.fromkeys(["PEPEUSDT", "NEIROUSDT", "TUTUSDT", "DOGEUSDT"] + known_pairs))

for pair in pairs_to_sync:
    print(f"Fetching trades for {pair} from Binance API...")
    try:
        trades = client.get_my_trades(symbol=pair, limit=100)
        if not trades:
            print(f"-> No recent trades found for {pair}.")
            continue
            
        trades.sort(key=lambda x: x['time'])
        
        c.execute("SELECT trade_date, trade_time, action, price, qty FROM trade_history WHERE pair = ?", (pair,))
        rows = c.fetchall()
        seen_tx = set((r[0], r[1], str(r[2]).strip(), round(float(r[3]), 8), round(float(r[4]), 8)) for r in rows)
        seen_sell_slots = set((r[0], r[1], round(float(r[3]), 6), round(float(r[4]), 4)) for r in rows if r[2] in ('SELL', 'PARTIAL_TP', 'MANUAL_RECYCLE', 'FORCED_SELL_CUTLOSS', 'FORCE SELL', 'CUT LOSS', 'TAKE PROFIT'))
        seen_buy_slots = set((r[0], r[1], round(float(r[3]), 6), round(float(r[4]), 4)) for r in rows if r[2] in ('BUY', 'PREBUY'))
        
        total_buy_qty = 0.0
        total_buy_cost = 0.0
        total_fee_usdt = 0.0
        synced_count = 0
        
        for t in trades:
            dt = datetime.fromtimestamp(t['time'] / 1000)
            d_str = dt.strftime("%Y-%m-%d")
            t_str = dt.strftime("%H:%M:%S")
            is_buy = t['isBuyer']
            action = "BUY" if is_buy else "SELL"
            price = float(t['price'])
            qty = float(t['qty'])
            commission = float(t['commission'])
            commission_asset = t['commissionAsset']
            fee_str = f"{commission:.8f} {commission_asset}"
            quote_qty = float(t.get('quoteQty', qty * price))
            fee_usdt = commission if commission_asset == 'USDT' else 0.0
            
            if is_buy:
                total_buy_qty += qty
                total_buy_cost += quote_qty
                total_fee_usdt += fee_usdt
                profit = 0.0
                msg = f"cost={quote_qty:.8f} USDT | fee={fee_str}"
            else:
                avg_buy_price = (total_buy_cost / total_buy_qty) if total_buy_qty > 0 else price
                profit = max(0.0, (price - avg_buy_price) * qty - total_fee_usdt - fee_usdt)
                msg = f"fee={fee_str}"
                total_buy_qty = 0.0
                total_buy_cost = 0.0
                total_fee_usdt = 0.0
            
            key = (d_str, t_str, action, round(price, 8), round(qty, 8))
            slot_key = (d_str, t_str, round(price, 6), round(qty, 4))
            is_already_logged = (key in seen_tx) or (not is_buy and slot_key in seen_sell_slots) or (is_buy and slot_key in seen_buy_slots)
            
            if not is_already_logged:
                seen_tx.add(key)
                if is_buy:
                    seen_buy_slots.add(slot_key)
                else:
                    seen_sell_slots.add(slot_key)
                c.execute("""
                INSERT INTO trade_history (trade_date, trade_time, pair, action, price, qty, profit, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (d_str, t_str, pair, action, price, qty, profit, msg, int(time.time())))
                synced_count += 1
                
        print(f"-> Synced {synced_count} new trades directly into SQLite bot_trading.db!")
    except Exception as e:
        print(f"Error fetching {pair}: {e}")

# Cleanup any duplicate entries
try:
    c.execute("""
    UPDATE trade_history 
    SET profit = 0.0 
    WHERE action IN ('COMPOUND', 'CONFIG_REVERTED', 'CONFIG_APPLIED', 'AUTO_RESCUE', 
                     'AUTOPILOT_START', 'AUTOPILOT_TRIGGER', 'SWAP', 'PREBUY', 'AUTO_REVERT')
      AND profit != 0.0
    """)
    c.execute("""
    DELETE FROM trade_history 
    WHERE action = 'SELL' 
    AND EXISTS (
        SELECT 1 FROM trade_history t2 
        WHERE t2.pair = trade_history.pair 
        AND t2.trade_date = trade_history.trade_date 
        AND t2.trade_time = trade_history.trade_time 
        AND t2.action IN ('PARTIAL_TP', 'MANUAL_RECYCLE', 'FORCED_SELL_CUTLOSS')
    )
    """)
    c.execute("""
    DELETE FROM trade_history 
    WHERE id NOT IN (
        SELECT MIN(id) 
        FROM trade_history 
        GROUP BY trade_date, trade_time, pair, action, price, qty
    )
    """)
    conn.commit()
except Exception as e:
    print(f"Error cleaning duplicates: {e}")

conn.commit()
conn.close()
print("Sync completed successfully.")
