# -*- coding: utf-8 -*-
from binance.client import Client
import time
from datetime import datetime

# Import config dari multibot
try:
    from multibot import API_KEY, API_SECRET, PAIRS_CONFIG
except ImportError:
    print("Gagal mengimpor dari multibot. Pastikan API_KEY, API_SECRET, dan PAIRS_CONFIG ada di multibot.py.")
    exit(1)

def print_separator():
    print("=" * 50)

def calculate_profit(client, pair):
    print("Mengambil data transaksi untuk {} (ini mungkin butuh beberapa detik)...".format(pair))
    retries = 5
    trades = None
    for attempt in range(1, retries + 1):
        try:
            trades = client.get_my_trades(symbol=pair)
            break
        except Exception as e:
            print("Gagal mengambil data untuk {} (Percobaan {}/{}): {}".format(pair, attempt, retries, e))
            if attempt == retries:
                return
            time.sleep(2 * attempt)

    if not trades:
        print("Tidak ada riwayat transaksi untuk {}.".format(pair))
        return

    total_buy_vol = 0.0
    total_sell_vol = 0.0
    
    held_qty = 0.0
    total_cost = 0.0
    
    realized_profit = 0.0
    
    total_fee_usdt = 0.0
    total_fee_bnb = 0.0
    total_fee_coin = 0.0
    
    buy_count = 0
    sell_count = 0
    
    base_asset = pair.replace("USDT", "")

    for t in trades:
        qty = float(t['qty'])
        quoteQty = float(t['quoteQty'])
        fee = float(t['commission'])
        fee_asset = t['commissionAsset']
        
        # Accumulate fees
        if fee_asset == 'USDT':
            total_fee_usdt += fee
        elif fee_asset == 'BNB':
            total_fee_bnb += fee
        elif fee_asset == base_asset:
            total_fee_coin += fee
        else:
            # fee in other asset
            pass
            
        if t['isBuyer']:
            buy_count += 1
            total_buy_vol += quoteQty
            
            # Jika fee dibayar pakai koin itu sendiri, saldo koin berkurang
            if fee_asset == base_asset:
                actual_qty_received = qty - fee
            else:
                actual_qty_received = qty
                
            held_qty += actual_qty_received
            total_cost += quoteQty
        else:
            sell_count += 1
            total_sell_vol += quoteQty
            
            if held_qty > 0:
                # Harga rata-rata modal saat ini
                avg_cost = total_cost / held_qty
                cost_of_sold = avg_cost * qty
                
                # Untung = Nilai Jual - Modal
                profit = quoteQty - cost_of_sold
                realized_profit += profit
                
                held_qty -= qty
                total_cost -= cost_of_sold
                
                # Cegah minus atau floating point kecil
                if held_qty < 1e-8 or total_cost < 1e-8:
                    held_qty = 0.0
                    total_cost = 0.0
            else:
                # Menjual koin yang riwayat belinya tidak ada di data ini
                # Anggap 100% profit (atau modal 0)
                realized_profit += quoteQty

    print_separator()
    print("=== LAPORAN PROFIT {} ===".format(pair))
    print("Total Transaksi  : {} ({} Buy / {} Sell)".format(len(trades), buy_count, sell_count))
    print("Total Beli       : {:.2f} USDT".format(total_buy_vol))
    print("Total Jual       : {:.2f} USDT".format(total_sell_vol))
    print("Koin Ditahan     : {:.6f} {}".format(held_qty, base_asset))
    print("Modal Mengambang : {:.2f} USDT".format(total_cost))
    print("-" * 50)
    print("Fee (USDT)       : {:.4f} USDT".format(total_fee_usdt))
    if total_fee_bnb > 0:
        print("Fee (BNB)        : {:.6f} BNB".format(total_fee_bnb))
    if total_fee_coin > 0:
        print("Fee ({})       : {:.6f} {}".format(base_asset, total_fee_coin, base_asset))
        
    print("-" * 50)
    # Net profit estimasi mengurangi fee USDT
    net_profit = realized_profit - total_fee_usdt
    profit_symbol = "🟢" if net_profit > 0 else "🔴" if net_profit < 0 else "⚪"
    
    print("REALIZED PNL     : {:+.4f} USDT {}".format(realized_profit, profit_symbol))
    print("NET PROFIT (Est) : {:+.4f} USDT {}".format(net_profit, profit_symbol))
    print("*(Belum memotong fee BNB/Koin jika ada)")
    print_separator()
    print("")

def main():
    print_separator()
    print("MENGHITUNG TOTAL KEUNTUNGAN BINANCE")
    print_separator()
    
    client = Client(API_KEY, API_SECRET)
    
    # Sinkronisasi waktu otomatis untuk mencegah error Timestamp 1000ms
    try:
        server_time = client.get_server_time()
        time_offset = server_time['serverTime'] - int(time.time() * 1000)
        # Pada versi python-binance tertentu, atribut ini bernama timestamp_offset
        if hasattr(client, 'timestamp_offset'):
            client.timestamp_offset = time_offset
        else:
            client.timestamp_offset = time_offset
    except Exception as e:
        pass

    pairs_to_check = PAIRS_CONFIG.keys()
    
    if not pairs_to_check:
        print("Tidak ada koin yang dikonfigurasi di PAIRS_CONFIG pada multibot.py.")
        return
        
    for pair in pairs_to_check:
        calculate_profit(client, pair)
        
if __name__ == "__main__":
    main()
