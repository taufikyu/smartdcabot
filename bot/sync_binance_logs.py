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
except:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
pairs_to_sync = ["PEPEUSDT", "TUTUSDT", "DOGEUSDT"]

for pair in pairs_to_sync:
    print(f"Fetching trades for {pair}...")
    try:
        trades = client.get_my_trades(symbol=pair, limit=50)
        trades.sort(key=lambda x: x['time'])
        
        log_lines = []
        total_buy_qty = 0.0
        total_buy_cost = 0.0
        total_fee_usdt = 0.0
        
        for t in trades:
            dt = datetime.fromtimestamp(t['time'] / 1000)
            date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            is_buy = t['isBuyer']
            action = "BUY" if is_buy else "SELL"
            price = float(t['price'])
            qty = float(t['qty'])
            commission = float(t['commission'])
            commission_asset = t['commissionAsset']
            fee_str = f"{commission:.8f} {commission_asset}"
            quote_qty = float(t.get('quoteQty', qty * price))
            fee_usdt = commission if commission_asset == 'USDT' else 0.0
            
            profit = 0.0
            if is_buy:
                total_buy_qty += qty
                total_buy_cost += quote_qty
                total_fee_usdt += fee_usdt
                line = f"[{date_str}] {action} | Price: {price:.8f} | Qty: {int(qty) if qty > 100 else qty} | Profit: 0 | cost={quote_qty:.8f} USDT | fee={fee_str} | usable_quote=2.200000\n"
            else:
                avg_buy_price = (total_buy_cost / total_buy_qty) if total_buy_qty > 0 else price
                profit = (price - avg_buy_price) * qty - total_fee_usdt - fee_usdt
                line = f"[{date_str}] {action} | Price: {price:.8f} | Qty: {int(qty) if qty > 100 else qty} | Profit: {max(0.0, profit):.8f} |\n"
                total_buy_qty = 0.0
                total_buy_cost = 0.0
                total_fee_usdt = 0.0
            
            log_lines.append(line)
            
        if log_lines:
            target_file = os.path.join(BASE_DIR, f"trade_log_{pair}.txt")
            with open(target_file, "w", encoding="utf-8") as f:
                f.writelines(log_lines)
            print(f"-> Saved {len(log_lines)} trades to {target_file}")
        else:
            print(f"-> No recent trades found for {pair}.")
            
    except Exception as e:
        print(f"Error fetching {pair}: {e}")

print("Sync completed successfully.")
