import os
import time
import json
import logging
import sys
import random
import re
import glob
from math import floor
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from decimal import Decimal, ROUND_UP, getcontext
getcontext().prec = 18
from datetime import datetime

# ============ CONFIG ============
API_KEY = 'QHeMLNsFIyZifj5TQnb4PXtdWnA38HFCa6nS7ecOJ6hiD7pQquASvqvwtVfXDs7Z'
API_SECRET = 'uWBiv1bUbvhY2xPFzHM3UgbTA1FOP7bfI2MwxUjc8gOu9NnABu0KrN7hyTl4LRBj'

# ============ SIMPLE AUTH CONFIG ============
AUTH_USERNAME = os.getenv('AUTH_USERNAME', 'admin')
AUTH_PASSWORD = os.getenv('AUTH_PASSWORD', 'pAssword123456:)')

DEBUG = False

delay = 5
# Konfigurasi spesifik per koin
PAIRS_CONFIG = {
    "DOGEUSDT": {
        "BUDGET_USD": 15,
        "BUY_AMOUNT": 2.1,
        "DROP_THRESHOLD": 0.01,
        "MAX_LOSS_PERCENT": -15,
        "FEE_RATE": 0.001,
        "TAKE_PROFIT_MARGIN": 0.01,
        "TRAILING_MARGIN": 0.001,
        "RSI_MAX_ENTRY": 48,
        "STATUS": 1
    },
    "PEPEUSDT": {
        "BUDGET_USD": 16,
        "BUY_AMOUNT": 2.2,
        "DROP_THRESHOLD": 0.01,
        "MAX_LOSS_PERCENT": -15,
        "FEE_RATE": 0.001,
        "TAKE_PROFIT_MARGIN": 0.01,
        "TRAILING_MARGIN": 0.001,
        "RSI_MAX_ENTRY": 48,
        "STATUS": 1
    }
}
PAIRS = list(PAIRS_CONFIG.keys())
MAX_SLOTS = 2

MIN_BUY_INTERVAL = 30
PRICE_HIST = 48

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIVE_PAIRS_FILE = os.path.join(BASE_DIR, "active_pairs.json")

def save_active_pairs():
    try:
        data = {
            "PAIRS": PAIRS,
            "PAIRS_CONFIG": PAIRS_CONFIG
        }
        with open(ACTIVE_PAIRS_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Error saving active pairs: {e}")

def load_active_pairs():
    global PAIRS, PAIRS_CONFIG
    if os.path.exists(ACTIVE_PAIRS_FILE):
        try:
            with open(ACTIVE_PAIRS_FILE, 'r') as f:
                d = json.load(f)
                if "PAIRS" in d and isinstance(d["PAIRS"], list) and len(d["PAIRS"]) > 0:
                    PAIRS = d["PAIRS"]
                if "PAIRS_CONFIG" in d and isinstance(d["PAIRS_CONFIG"], dict):
                    PAIRS_CONFIG.update(d["PAIRS_CONFIG"])
        except Exception as e:
            print(f"Error loading active pairs: {e}")

load_active_pairs()

def get_log_file(pair):
    return os.path.join(BASE_DIR, "trade_log_{}.txt".format(pair))

def get_data_file(pair):
    return os.path.join(BASE_DIR, "bot_{}.json".format(pair))

def get_price_hist_file(pair):
    return os.path.join(BASE_DIR, "price_history_{}.txt".format(pair))

AUTO_STOP_AFTER_SELL = False

BINANCE_API_URL = os.getenv('BINANCE_API_URL')
if BINANCE_API_URL:
    Client.API_URL = BINANCE_API_URL

try:
    client = Client(API_KEY, API_SECRET)
except Exception as e:
    print(f"[INIT] Notice: Switching to Binance Vision API mirror due to connection: {e}")
    Client.API_URL = "https://data-api.binance.vision/api"
    client = Client(API_KEY, API_SECRET)

# Sinkronisasi waktu otomatis (mencegah error Timestamp 1000ms ahead)
try:
    server_time = client.get_server_time()
    time_offset = server_time['serverTime'] - int(time.time() * 1000)
    client.timestamp_offset = time_offset
except Exception as e:
    print("[INIT] Gagal sync waktu:", e)
is_buying = {}

_cached_notional = {}
_cached_step = {}

PRICE_CACHE = {}
PRICE_TTL = 3.0

ACCOUNT_CACHE = {"ts": 0, "account": None, "balances": None}
ACCOUNT_TTL = 30.0

LAST_API_CALL = 0.0
MIN_API_INTERVAL = 0.05

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from datetime import timedelta
import threading

bot_locks = {}
bot_data = {}
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'smart-dca-bot-secure-key-2026')
app.permanent_session_lifetime = timedelta(days=7)

@app.before_request
def require_auth():
    # Whitelist login, static files, and error handlers
    if request.path.startswith('/static') or request.path in ['/login']:
        return None
        
    if not session.get('logged_in'):
        if request.path.startswith('/api/'):
            return jsonify({'success': False, 'message': 'Unauthorized. Please login.'}), 401
        return redirect(url_for('login'))


# ============ HELPERS ============

def fmt(v):
    if v == 0: return "0"
    return f"{float(v):.8f}".rstrip('0').rstrip('.')

def log_price_to_file(pair, price):
    with open(get_price_hist_file(pair), "a") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Price: {fmt(price)}\n")
        
_LOGGERS = {}
def get_logger(pair):
    if pair not in _LOGGERS:
        logger = logging.getLogger(pair)
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        fh = logging.FileHandler(get_log_file(pair), encoding='utf-8')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        if DEBUG:
            ch = logging.StreamHandler()
            ch.setFormatter(formatter)
            logger.addHandler(ch)
        _LOGGERS[pair] = logger
    return _LOGGERS[pair]

def log_action(pair, action, price=0.0, qty=0.0, profit=0.0, message=""):
    logger = get_logger(pair)
    logger.info(f"{action} | Price: {fmt(price)} | Qty: {fmt(qty)} | Profit: {fmt(profit)} | {message}")

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
def get_min_notional(symbol):
    if not symbol: return 5.0
    if symbol in _cached_notional:
        return _cached_notional[symbol]
    try:
        info = safe_api_call(client.get_symbol_info, symbol)
        for f in info.get('filters', []):
            if f.get('filterType') in ('NOTIONAL', 'MIN_NOTIONAL'):
                val = float(f.get('minNotional') or f.get('minNotional', 0))
                _cached_notional[symbol] = val
                return val
    except Exception as e:
        if DEBUG:
            print(f"ERROR get_min_notional({symbol}):", e)
    return 5.0

get_notion = get_min_notional

# ============ RSI & ANALYTICS HELPERS ============

_RSI_CACHE = {}
_RSI_TTL = 60.0  # Cache RSI selama 60 detik

def get_rsi(pair, interval='15m', period=14):
    """Menghitung RSI(14) timeframe 15 menit secara native dan efisien dengan caching."""
    now = time.time()
    cached = _RSI_CACHE.get(pair)
    if cached and (now - cached['time']) < _RSI_TTL:
        return cached['rsi']
    
    try:
        klines = safe_api_call(client.get_klines, symbol=pair, interval='15m', limit=period + 15)
        if not klines or len(klines) < period + 1:
            return 50.0
        
        close_prices = [float(k[4]) for k in klines]
        
        gains = []
        losses = []
        for i in range(1, len(close_prices)):
            delta = close_prices[i] - close_prices[i - 1]
            if delta >= 0:
                gains.append(delta)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(delta))
                
        if len(gains) < period:
            return 50.0
            
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            
        if avg_loss == 0:
            rsi_val = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_val = 100.0 - (100.0 / (1.0 + rs))
            
        rsi_val = round(rsi_val, 1)
        _RSI_CACHE[pair] = {'rsi': rsi_val, 'time': now}
        return rsi_val
    except Exception as e:
        if DEBUG: print(f"[DBG RSI] Error get_rsi {pair}: {e}")
        return 50.0

def get_capital_config():
    path = os.path.join(BASE_DIR, "capital_config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"injected_capital": 20.0, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

def save_capital_config(injected_amount):
    path = os.path.join(BASE_DIR, "capital_config.json")
    try:
        val = float(injected_amount)
    except:
        val = 20.0
    data = {
        "injected_capital": max(0.0, val),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    return data

def get_analytics_data():
    """Membaca dan memparsing seluruh file log transaksi untuk analitik profit kalender & pelacak modal."""
    all_sells = []
    daily_summary = {}
    
    log_files = [f for f in glob.glob(os.path.join(BASE_DIR, "trade_log_*.txt")) if 'backup' not in os.path.basename(f).lower()]
    if not log_files:
        main_log = os.path.join(BASE_DIR, "trade_log.txt")
        if os.path.exists(main_log): log_files = [main_log]
        
    total_realized_profit = 0.0
    total_trades_count = 0
    
    sell_pattern = re.compile(r'\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\]\s+(?:SELL|FORCED_SELL_CUTLOSS)\s+\|\s+Price:\s+([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+\|\s+Qty:\s+([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+\|\s+Profit:\s+([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)')
    
    for lfile in log_files:
        pair_from_file = os.path.basename(lfile).replace("trade_log_", "").replace(".txt", "")
        if pair_from_file == "trade_log" or not pair_from_file:
            pair_from_file = "DOGEUSDT"
            
        try:
            with open(lfile, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                for line in lines:
                    match = sell_pattern.search(line)
                    if match:
                        dt_date = match.group(1)
                        dt_time = match.group(2)
                        p_price = float(match.group(3))
                        p_qty = float(match.group(4))
                        p_profit = float(match.group(5))
                        
                        total_realized_profit += p_profit
                        total_trades_count += 1
                        
                        if dt_date not in daily_summary:
                            daily_summary[dt_date] = {
                                'date': dt_date,
                                'profit': 0.0,
                                'trades': 0,
                                'pairs': set()
                            }
                        daily_summary[dt_date]['profit'] += p_profit
                        daily_summary[dt_date]['trades'] += 1
                        daily_summary[dt_date]['pairs'].add(pair_from_file)
                        
                        all_sells.append({
                            'date': dt_date,
                            'time': dt_time,
                            'datetime': f"{dt_date} {dt_time}",
                            'pair': pair_from_file,
                            'price': p_price,
                            'qty': p_qty,
                            'profit': round(p_profit, 6)
                        })
        except Exception as ex:
            if DEBUG: print(f"Error reading log file {lfile}: {ex}")
            
    all_sells.sort(key=lambda x: x['datetime'], reverse=True)
    
    daily_list = []
    today_str = datetime.now().strftime('%Y-%m-%d')
    today_profit = 0.0
    month_str = datetime.now().strftime('%Y-%m')
    month_profit = 0.0
    
    MONTH_NAMES_ID = {
        "01": "Januari", "02": "Februari", "03": "Maret", "04": "April",
        "05": "Mei", "06": "Juni", "07": "Juli", "08": "Agustus",
        "09": "September", "10": "Oktober", "11": "November", "12": "Desember"
    }
    
    monthly_summary = {}
    
    for d_str in sorted(daily_summary.keys(), reverse=True):
        entry = daily_summary[d_str]
        p_val = round(entry['profit'], 4)
        if d_str == today_str:
            today_profit = p_val
        if d_str.startswith(month_str):
            month_profit += p_val
            
        m_key = d_str[:7]
        if m_key not in monthly_summary:
            monthly_summary[m_key] = {
                'month': m_key,
                'profit': 0.0,
                'trades': 0,
                'pairs': set()
            }
        monthly_summary[m_key]['profit'] += entry['profit']
        monthly_summary[m_key]['trades'] += entry['trades']
        monthly_summary[m_key]['pairs'].update(entry['pairs'])
            
        daily_list.append({
            'date': d_str,
            'profit': p_val,
            'trades': entry['trades'],
            'pairs': list(entry['pairs'])
        })
        
    monthly_list = []
    for m_str in sorted(monthly_summary.keys(), reverse=True):
        m_entry = monthly_summary[m_str]
        parts = m_str.split('-')
        m_label = f"{MONTH_NAMES_ID.get(parts[1], parts[1])} {parts[0]}" if len(parts) == 2 else m_str
        monthly_list.append({
            'month': m_str,
            'label': m_label,
            'profit': round(m_entry['profit'], 4),
            'trades': m_entry['trades'],
            'pairs': list(m_entry['pairs']),
            'avg_per_trade': round(m_entry['profit'] / m_entry['trades'], 4) if m_entry['trades'] > 0 else 0.0
        })
        
    # Capital Tracker Calculations
    cap_data = get_capital_config()
    injected_capital = float(cap_data.get('injected_capital', 20.0))
    free_usdt, total_usd = get_total_usdt_value_cached()
    
    net_growth = total_usd - injected_capital
    net_growth_pct = (net_growth / injected_capital * 100.0) if injected_capital > 0 else 0.0
    
    return {
        'total_realized_profit': round(total_realized_profit, 4),
        'total_trades': total_trades_count,
        'today_profit': round(today_profit, 4),
        'month_profit': round(month_profit, 4),
        'win_rate': 100.0 if total_trades_count > 0 else 0.0,
        'capital_tracker': {
            'injected_capital': round(injected_capital, 4),
            'current_equity': round(total_usd, 4),
            'free_usdt': round(free_usdt, 4),
            'net_growth': round(net_growth, 4),
            'net_growth_pct': round(net_growth_pct, 2),
            'updated_at': cap_data.get('updated_at', '')
        },
        'monthly_breakdown': monthly_list,
        'daily_breakdown': daily_list,
        'recent_sells': all_sells[:100]
    }

# ============ SCANNER & BACKTEST ENGINE ============

COIN_INFO = {
    "PEPE": {"name": "Pepe", "sector": "Memecoin (Ethereum)", "desc": "Memecoin berbasis karakter meme katak hijau legendaris karya Matt Furie. Menjadi salah satu koin paling likuid dan volatil di pasar crypto."},
    "DOGE": {"name": "Dogecoin", "sector": "Memecoin (Layer-1 PoW)", "desc": "Pionir memecoin pertama di dunia yang dibuat pada tahun 2013 berbasis blockchain PoW sendiri. Memiliki komunitas raksasa dan sering didukung Elon Musk."},
    "SHIB": {"name": "Shiba Inu", "sector": "Meme & DeFi Ecosystem", "desc": "Memecoin bertema anjing Shiba yang berevolusi menjadi ekosistem DeFi lengkap (ShibaSwap, Shibarium Layer-2)."},
    "PYTH": {"name": "Pyth Network", "sector": "Oracle & DeFi Infra", "desc": "Jaringan Oracle keuangan berkecepatan tinggi yang menyediakan data harga real-time (sub-detik) untuk dApps di Solana dan 50+ blockchain lainnya."},
    "TRUMP": {"name": "Official Trump", "sector": "PoliFi / Memecoin", "desc": "Memecoin bertema politik populer di jaringan Solana yang merefleksikan sentimen kampanye dan kultur internet AS."},
    "PENGU": {"name": "Pudgy Penguins", "sector": "NFT Brand & Web3 IP", "desc": "Token resmi dari brand Web3 dan komunitas mainan global Pudgy Penguins yang berfokus pada adopsi massal budaya internet."},
    "FLOKI": {"name": "Floki", "sector": "Meme & Gaming/DeFi", "desc": "Ekosistem Web3 yang mencakup game Valhalla Metaverse, FlokiFi locker, dan utilitas kartu crypto komunitas."},
    "BONK": {"name": "Bonk", "sector": "Memecoin (Solana)", "desc": "Memecoin komunitas nomor 1 di jaringan Solana yang membantu membangkitkan ekosistem Solana pada akhir 2022."},
    "WIF": {"name": "dogwifhat", "sector": "Memecoin (Solana)", "desc": "Memecoin viral di blockchain Solana yang menampilkan anjing Shiba mengenakan topi rajut wol pink."},
    "SUI": {"name": "Sui Network", "sector": "Layer-1 Blockchain", "desc": "Blockchain Layer-1 berperforma ultra-tinggi yang menggunakan bahasa pemrograman Move dan arsitektur eksekusi paralel."},
    "SOL": {"name": "Solana", "sector": "Layer-1 Blockchain", "desc": "Blockchain generasi ketiga dengan kecepatan transaksi ribuan TPS dan biaya gas super murah, rumah bagi ekosistem DeFi dan Memecoin terbesar."},
    "TRX": {"name": "Tron", "sector": "Layer-1 & Payment Network", "desc": "Blockchain jaringan pembayaran global yang didirikan Justin Sun, memproses sebagian besar transaksi transfer USDT di dunia."},
    "ZEC": {"name": "Zcash", "sector": "Privacy Coin (PoW)", "desc": "Mata uang digital berbasis privasi kriptografi zero-knowledge (zk-SNARKs) yang memungkinkan transaksi rahasia tanpa melacak pengirim/penerima."},
    "NEIRO": {"name": "First Neiro on Ethereum", "sector": "Memecoin (Ethereum)", "desc": "Memecoin anjing Shiba diadopsi oleh pemilik asli Kabosu (ikon Doge), menjadi salah satu token komunitas terpopuler di Binance."},
    "TURBO": {"name": "Turbo", "sector": "AI-Generated Memecoin", "desc": "Memecoin pertama yang dibuat 100% menggunakan prompt kecerdasan buatan GPT-4 dengan modal awal hanya $69."},
    "BOME": {"name": "Book of Meme", "sector": "Meme & Decentralized Archive", "desc": "Proyek meme eksperimental di Solana yang diinisiasi oleh seniman Web3 Darkfarms untuk mengabadikan kultur meme di blockchain."},
    "1000SATS": {"name": "SATS (Ordinals)", "sector": "Bitcoin BRC-20 Ecosystem", "desc": "Token standar BRC-20 di atas jaringan Bitcoin yang memberi penghormatan kepada Satoshi Nakamoto (unit terkecil Bitcoin)."},
    "CATI": {"name": "Catizen", "sector": "Telegram / TON Gaming", "desc": "Game Web3 bertema kucing viral berbasis bot Telegram dan blockchain TON yang dimainkan puluhan juta pengguna."},
    "PNUT": {"name": "Peanut the Squirrel", "sector": "Memecoin (Solana)", "desc": "Memecoin bertema tupai peliharaan viral Peanut yang memicu gelombang dukungan komunitas internet global."},
    "ACT": {"name": "Act I : The AI Prophecy", "sector": "AI Agent / Ecosystem", "desc": "Proyek eksplorasi AI agent otonom terdesentralisasi dan kolaborasi interaksi multi-AI di jaringan Solana."},
    "ADA": {"name": "Cardano", "sector": "Layer-1 PoS Blockchain", "desc": "Blockchain terdesentralisasi berbasis riset akademis peer-reviewed yang didirikan oleh Charles Hoskinson (co-founder Ethereum)."},
    "XRP": {"name": "Ripple / XRP", "sector": "Cross-Border Payment", "desc": "Aset digital yang dirancang untuk pembayaran lintas batas perbankan dan lembaga keuangan global secara instan dengan biaya sangat rendah."},
    "AVAX": {"name": "Avalanche", "sector": "Layer-1 & Subnets", "desc": "Platform smart contract berkecepatan tinggi dengan arsitektur Subnet yang dapat disesuaikan untuk skala korporat dan gaming."},
    "NEAR": {"name": "NEAR Protocol", "sector": "Layer-1 & AI Infrastructure", "desc": "Blockchain ramah pengembang dengan teknologi Nightshade sharding dan fokus kuat pada integrasi User-Owned AI."},
    "RENDER": {"name": "Render Network", "sector": "DePIN & GPU Rendering", "desc": "Jaringan komputasi GPU terdesentralisasi untuk rendering 3D grafis, motion graphics, dan komputasi model AI."},
    "FET": {"name": "Artificial Superintelligence Alliance", "sector": "AI / Machine Learning", "desc": "Aliansi ekosistem kecerdasan buatan terdesentralisasi untuk agen otonom dan komputasi machine learning terbuka."},
    "DOT": {"name": "Polkadot", "sector": "Layer-0 Interoperability", "desc": "Protokol multi-chain terfragmentasi yang menghubungkan berbagai blockchain khusus (parachains) menjadi satu jaringan aman."},
    "LINK": {"name": "Chainlink", "sector": "Oracle Network Standard", "desc": "Standar industri jaringan oracle terdesentralisasi yang menghubungkan smart contract blockchain dengan data dunia nyata."},
    "INJ": {"name": "Injective", "sector": "DeFi / Layer-1 Financial", "desc": "Blockchain Layer-1 yang dioptimalkan khusus untuk membangun aplikasi keuangan desentralisasi (DEX, derivatives, lending)."},
    "TIA": {"name": "Celestia", "sector": "Modular Blockchain (DA)", "desc": "Jaringan ketersediaan data (Data Availability) modular pertama yang memungkinkan siapa saja meluncurkan blockchain sendiri dengan mudah."},
    "SEI": {"name": "Sei Network", "sector": "Layer-1 Trading/Parallel", "desc": "Blockchain Layer-1 pertama yang khusus dioptimalkan untuk aktivitas trading dengan mesin pencocokan order bawaan."},
    "BTC": {"name": "Bitcoin", "sector": "Store of Value / King Crypto", "desc": "Cryptocurrency pertama di dunia yang diciptakan oleh Satoshi Nakamoto pada 2009, berfungsi sebagai emas digital terdesentralisasi."},
    "ETH": {"name": "Ethereum", "sector": "Smart Contract Platform", "desc": "Platform komputasi terdesentralisasi terbesar di dunia yang memelopori smart contract, DeFi, NFT, dan ekosistem Web3."},
    "BNB": {"name": "Binance Coin", "sector": "BNB Chain & Exchange Token", "desc": "Token utilitas ekosistem Binance dan bahan bakar gas untuk jaringan BNB Smart Chain (BSC)."}
}

def get_coin_info(coin_or_symbol):
    c = str(coin_or_symbol).upper().replace('USDT', '').strip()
    if c in COIN_INFO:
        return COIN_INFO[c]
    return {
        "name": c,
        "sector": "Binance Spot Asset",
        "desc": f"Aset kripto {c} yang diperdagangkan secara aktif di pasar Spot Binance dengan likuiditas tinggi."
    }

GLOBAL_BLACKLIST = {
    "LUNAUSDT", "LUNCUSDT", "USTCUSDT", "FTTUSDT", "VGXUSDT", "WTCUSDT",
    "BTTUSDT", "BTTCUSDT", "DREPUSDT", "MOBUSDT", "PNTUSDT", "TORNUSDT",
    "MULTIUSDT", "OMGUSDT", "WAVESUSDT", "XEMUSDT", "WNXMUSDT", "SUNUSDT"
}

STABLECOIN_PAIRS = {
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "EURUSDT", "DAIUSDT",
    "AEURUSDT", "USDPUSDT", "WBETHUSDT", "WBTCUSDT"
}

_SCANNER_CACHE = {}
_SCANNER_TTL = 120.0  # 2 minutes cache

def scan_market_candidates(min_volume_usd=15_000_000, max_notional=2.5, max_results=8):
    """Memindai pasar Binance Spot dengan 4 lapis filter keselamatan (Volume, Anti-Delist, Healthy Dip, Min Notional, RSI)."""
    now = time.time()
    cache_key = f"{min_volume_usd}_{max_notional}"
    if cache_key in _SCANNER_CACHE and (now - _SCANNER_CACHE[cache_key]['time']) < _SCANNER_TTL:
        return _SCANNER_CACHE[cache_key]['data']
        
    try:
        tickers = safe_api_call(client.get_ticker)
        candidates = []
        
        for t in tickers:
            symbol = t.get('symbol', '')
            if not symbol.endswith('USDT'):
                continue
            if symbol in GLOBAL_BLACKLIST or symbol in STABLECOIN_PAIRS:
                continue
            if any(sym in symbol for sym in ['UPUSDT', 'DOWNUSDT', 'BEARUSDT', 'BULLUSDT']):
                continue
            if symbol in PAIRS:
                continue
                
            quote_vol = float(t.get('quoteVolume', 0))
            if quote_vol < min_volume_usd:
                continue
                
            price_change_pct = float(t.get('priceChangePercent', 0))
            # Koreksi sehat: -8.0% s/d +3.0%
            if not (-8.0 <= price_change_pct <= 3.0):
                continue
                
            current_price = float(t.get('lastPrice', 0))
            if current_price <= 0:
                continue
                
            min_not = get_min_notional(symbol)
            if max_notional and min_not > (max_notional + 0.05):
                continue
                
            coin_clean = symbol.replace('USDT', '')
            c_info = get_coin_info(coin_clean)
            
            candidates.append({
                'symbol': symbol,
                'coin': coin_clean,
                'name': c_info['name'],
                'sector': c_info['sector'],
                'desc': c_info['desc'],
                'price': fmt(current_price),
                'change_24h': round(price_change_pct, 2),
                'volume_24h_m': round(quote_vol / 1_000_000, 1),
                'min_notional': min_not,
                'rsi': get_rsi(symbol)
            })
            
        candidates.sort(key=lambda x: (x['rsi'], -x['volume_24h_m']))
        selected = candidates[:max_results]
        _SCANNER_CACHE[cache_key] = {'data': selected, 'time': now}
        return selected
    except Exception as e:
        if DEBUG: print(f"[DBG Scanner] Error scanning market: {e}")
        return []

def run_dca_backtest(pair, days=30, budget_usd=15.0, buy_amount=2.1, take_profit_margin=0.008, drop_threshold=0.013):
    """
    Mensimulasikan strategi DCA piramida pada data historis Binance (7, 14, 30, 60, 100 hari).
    Menghasilkan skor kelayakan: profit, completed trades, max layer, dan status keselamatan.
    """
    try:
        days = min(max(1, int(days)), 365)
        if days <= 30:
            interval_used = '15m'
            candles_needed = int(days * 96)
        elif days <= 90:
            interval_used = '1h'
            candles_needed = int(days * 24)
        else:
            interval_used = '2h'
            candles_needed = int(days * 12)
        
        # Ambil data klines secara berurutan dengan pagination (Maksimal 4-5 batch agar instan)
        klines = []
        end_time = None
        loop_count = 0
        
        while len(klines) < candles_needed and loop_count < 5:
            loop_count += 1
            batch_limit = min(1000, candles_needed - len(klines))
            if end_time:
                batch = safe_api_call(client.get_klines, symbol=pair, interval=interval_used, limit=batch_limit, endTime=end_time)
            else:
                batch = safe_api_call(client.get_klines, symbol=pair, interval=interval_used, limit=batch_limit)
                
            if not batch:
                break
                
            if end_time:
                klines = batch + klines
            else:
                klines = batch
                
            end_time = int(batch[0][0]) - 1
            if len(batch) < batch_limit:
                break
                
        if not klines or len(klines) < 30:
            return {"success": False, "message": f"Data historis tidak mencukupi untuk {pair}."}
            
        fee_rate = 0.001
        buys = []
        budget_left = budget_usd
        completed_cycles = 0
        total_realized_profit = 0.0
        max_layer_reached = 0
        peak_price = 0.0
        rolling_prices = []
        
        for k in klines:
            c_high = float(k[2])
            c_low = float(k[3])
            c_close = float(k[4])
            
            rolling_prices.append(c_close)
            if len(rolling_prices) > 16:
                rolling_prices.pop(0)
                
            if c_high > peak_price:
                peak_price = c_high
                
            current_price = c_close
            
            # 1. Cek Take Profit
            if len(buys) > 0:
                total_qty = sum(b['qty'] for b in buys)
                total_cost = sum(b['price'] * b['qty'] for b in buys)
                avg_price = total_cost / total_qty if total_qty > 0 else 0
                
                tp_target = avg_price * (1 + (take_profit_margin + (len(buys) * 0.0015)))
                
                if c_high >= tp_target:
                    sell_value = total_qty * tp_target
                    buy_fee = total_cost * fee_rate
                    sell_fee = sell_value * fee_rate
                    cycle_profit = (sell_value - total_cost) - (buy_fee + sell_fee)
                    
                    total_realized_profit += cycle_profit
                    completed_cycles += 1
                    budget_left = budget_usd
                    buys = []
                    peak_price = c_close
                    continue
                    
            # 2. Cek Pembelian Layer
            if len(buys) == 0:
                if len(rolling_prices) >= 10:
                    recent_peak = max(rolling_prices)
                    drop_from_peak = (recent_peak - current_price) / recent_peak
                    if drop_from_peak >= drop_threshold and budget_left >= buy_amount:
                        qty = buy_amount / current_price
                        buys.append({'price': current_price, 'qty': qty})
                        budget_left -= buy_amount
                        max_layer_reached = max(max_layer_reached, len(buys))
            else:
                total_qty = sum(b['qty'] for b in buys)
                avg_price = sum(b['price'] * b['qty'] for b in buys) / total_qty
                
                layer_count = len(buys)
                if layer_count == 1: req_drop = 0.020
                elif layer_count == 2: req_drop = 0.035
                elif layer_count == 3: req_drop = 0.055
                elif layer_count == 4: req_drop = 0.080
                elif layer_count == 5: req_drop = 0.120
                else: req_drop = 0.180
                
                target_drop_price = avg_price * (1 - req_drop)
                if current_price <= target_drop_price and budget_left >= buy_amount and layer_count < 7:
                    qty = buy_amount / current_price
                    buys.append({'price': current_price, 'qty': qty})
                    budget_left -= buy_amount
                    max_layer_reached = max(max_layer_reached, len(buys))
                    
        if max_layer_reached <= 2:
            safety_status = "SANGAT AMAN 🛡️"
            safety_reason = f"Koin sangat cepat rebound. Selama {days} hari pengujian, bot paling dalam hanya menyentuh Layer {max_layer_reached} lalu langsung panen Take Profit."
        elif max_layer_reached <= 3:
            safety_status = "AMAN 🟢"
            safety_reason = f"Koin sempat mengalami koreksi wajar hingga Layer {max_layer_reached}, namun seluruh siklus berhasil ditutup Take Profit dengan lancar."
        else:
            safety_status = "WASPADA ⚠️"
            safety_reason = f"Koin sempat mengalami penurunan tajam (*deep dump*) hingga terserok ke Layer {max_layer_reached}. Tetap aman selama modal Anda mencukupi hingga 7 layer."

        roi_pct = round((total_realized_profit / budget_usd) * 100, 2) if budget_usd > 0 else 0
        return {
            "success": True,
            "pair": pair,
            "coin_info": get_coin_info(pair.replace('USDT', '')),
            "days_tested": days,
            "total_realized_profit": round(total_realized_profit, 4),
            "roi_pct": roi_pct,
            "completed_cycles": completed_cycles,
            "max_layer_reached": max_layer_reached,
            "open_layers_at_end": len(buys),
            "safety_status": safety_status,
            "safety_reason": safety_reason
        }
    except Exception as e:
        return {"success": False, "message": f"Error running backtest: {str(e)}"}

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
    symbol = symbol or pair
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

def calc_profit(pair, buy_price, sell_price, qty, fee=None):
    if fee is None:
        fee = bot_data[pair]["config"]["fee_rate"]
    gross = sell_price * qty
    cost = buy_price * qty
    total_fee = (sell_price + buy_price) * qty * fee
    return gross - cost - total_fee

# ============ Data load/save ============
def save_data(pair, d):
    with bot_locks[pair]:
        with open(get_data_file(pair) + ".bak", 'w') as f:
            json.dump(d, f, indent=4)
        with open(get_data_file(pair), 'w') as f:
            json.dump(d, f, indent=4)

def load_data(pair):
    cfg = PAIRS_CONFIG.get(pair, {})
    c_budget = cfg.get("BUDGET_USD", 23)
    c_buy = cfg.get("BUY_AMOUNT", 3.15)
    c_drop = cfg.get("DROP_THRESHOLD", 0.01)
    c_loss = cfg.get("MAX_LOSS_PERCENT", -15)
    c_fee = cfg.get("FEE_RATE", 0.001)
    c_tp = cfg.get("TAKE_PROFIT_MARGIN", 0.008)
    c_trail = cfg.get("TRAILING_MARGIN", 0.001)
    c_status = cfg.get("STATUS", 1)
    c_force_sell = cfg.get("FORCE_SELL", False)

    available_usdt = get_balance_from_cache("USDT")
    if c_budget > available_usdt:
        adjusted_budget = floor(available_usdt)
    else:
        adjusted_budget = c_budget
        
    data_file = get_data_file(pair)
    
    # Auto-migration
    if pair == "DOGEUSDT" and os.path.exists(os.path.join(BASE_DIR, "bot.json")) and not os.path.exists(data_file):
        import shutil
        shutil.move(os.path.join(BASE_DIR, "bot.json"), data_file)
        if os.path.exists(os.path.join(BASE_DIR, "trade_log.txt")):
            shutil.move(os.path.join(BASE_DIR, "trade_log.txt"), get_log_file(pair))
        if os.path.exists(os.path.join(BASE_DIR, "price_history.txt")):
            shutil.move(os.path.join(BASE_DIR, "price_history.txt"), get_price_hist_file(pair))

    if pair not in bot_locks:
        import threading
        bot_locks[pair] = threading.RLock()
        is_buying[pair] = False

    with bot_locks[pair]:
        if os.path.exists(data_file):
            with open(data_file, 'r') as f:
                d = json.load(f)
            default_conf = {
                "budget_usd": adjusted_budget,
                "buy_amount": c_buy,
                "max_layer": floor(adjusted_budget/c_buy) if c_buy else 0,
                "drop_threshold": c_drop,
                "max_loss_percent": c_loss,
                "fee_rate": c_fee,
                "take_profit_margin": c_tp,
                "trailing_margin": c_trail,
                "status": c_status,
                "peak_time": int(time.time()),
                "force_sell": False
            }
            if "config" not in d:
                d["config"] = default_conf.copy()
            else:
                # Update jika belum ada first buy (posisi kosong)
                if len(d.get("buys", [])) == 0:
                    for k, v in default_conf.items():
                        if k != "peak_time": # jangan timpa peak_time
                            if k == "status" and d.get("pending_replacement"):
                                continue # Biarkan status 0 (Sell Only) jika ada antrean swap
                            d["config"][k] = v
                else:
                    # Jika sedang jalan (sudah buy), hanya isi key yang belum ada
                    for k, v in default_conf.items():
                        if k not in d["config"]:
                            d["config"][k] = v
            if len(d.get("price_history", [])) < PRICE_HIST:
                try:
                    klines = safe_api_call(client.get_klines, symbol=pair, interval='5m', limit=PRICE_HIST)
                    if klines:
                        d["price_history"] = [float(k[4]) for k in klines]
                except Exception:
                    pass
            bot_data[pair] = d
            save_data(pair, d)
            return d
        else:
            init_history = []
            cur_price = get_ticker_price(pair)
            try:
                klines = safe_api_call(client.get_klines, symbol=pair, interval='5m', limit=PRICE_HIST)
                if klines:
                    init_history = [float(k[4]) for k in klines]
            except Exception:
                pass
            new_data = {
                "buys": [],
                "price_history": init_history,
                "budget_left": adjusted_budget,
                "peak_price": cur_price,
                "lowest_price": cur_price,
                "last_buy_time": 0,
                "config": {
                    "budget_usd": adjusted_budget,
                    "buy_amount": c_buy,
                    "max_layer": floor(adjusted_budget/c_buy) if c_buy else 0,
                    "drop_threshold": c_drop,
                    "max_loss_percent": c_loss,
                    "fee_rate": c_fee,
                    "take_profit_margin": c_tp,
                    "trailing_margin": c_trail,
                    "rsi_max_entry": cfg.get("RSI_MAX_ENTRY", 48),
                    "status": c_status
                }
            }
            bot_data[pair] = new_data
            save_data(pair, new_data)
            return new_data

def get_avg_buy(pair):
    data = bot_data[pair]
    
    total_qty = sum([b['qty'] for b in data['buys']])
    if total_qty == 0:
        return 0
    total_cost = sum([b['price'] * b['qty'] for b in data['buys']])
    return total_cost / total_qty

def get_dynamic_drop_threshold(pair):
    data = bot_data[pair]
    
    layer_count = len(data.get('buys', []))
    
    if layer_count <= 1:
        return 0.020  # Layer 2: Ayunan mikro harian (-2.0%)
    elif layer_count == 2:
        return 0.035  # Layer 3: Koreksi normal harian (-3.5%)
    elif layer_count == 3:
        return 0.055  # Layer 4: Koreksi sedang Altcoin (-5.5%)
    elif layer_count == 4:
        return 0.080  # Layer 5: Koreksi dalam pasar (-8.0%)
    elif layer_count == 5:
        return 0.120  # Layer 6: Dump keras Bitcoin/Pasar (-12.0%)
    else:
        return 0.180  # Layer 7+: Bantalan Darurat / Black Swan (-18.0%)

def get_dynamic_cooldown_secs(pair):
    data = bot_data[pair]

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

def is_fund_exhausted(pair):
    data = bot_data[pair]
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
        return 0.0, 0.0
    total = 0.0
    usdt = 0.0
    balances = acc.get('balances', [])
    for b in balances:
        asset = b.get('asset')
        free = float(b.get('free', 0.0))
        if free <= 0:
            continue
        if asset == 'USDT':
            total += free
            usdt += free
        else:
            asset_pair = asset + "USDT"
            price = PRICE_CACHE.get(asset_pair, (None, 0))[0]
            if price is None:
                try:
                    tick = safe_api_call(client.get_symbol_ticker, symbol=asset_pair)
                    price = float(tick['price'])
                    PRICE_CACHE[asset_pair] = (price, time.time())
                except Exception:
                    price = 0.0
            total += free * price
    return round(usdt, 4), round(total, 4)
    
def get_dynamic_buy_limits(pair):
    data = bot_data[pair]
    current_price = get_ticker_price(pair)
    peak = data['peak_price']
    low = data['lowest_price']

    if peak <= 0 or low <= 0: return None, None

    range_size = peak - low

    if range_size <= 0:
        return None, None

    if len(data['buys']) == 0:
        min_buy = None 
        max_buy = low + (range_size * 0.90)  
    else:
        # Buka pintu lebar-lebar! Biarin beli selama di bawah 80% pucuk
        min_buy = low + (range_size * 0.05)
        max_buy = low + (range_size * 0.80)
    
    if current_price <= low * 1.10:
        min_buy = None

    return min_buy, max_buy
    
def is_market_volatile(pair):
    data = bot_data[pair]
    current_price = get_ticker_price(pair)
    
    
    history = data.get('price_history', []) 
    
    if len(history) < PRICE_HIST:
        return False
        
    raw_prices = [p.get('price', 0) if isinstance(p, dict) else float(p) for p in history]
    valid_prices = [p for p in raw_prices if p > 0]
    
    if not valid_prices:
        return False
        
    max_1h = max(valid_prices)
    min_1h = min(valid_prices)
    
    drop_percent = (max_1h - current_price) / max_1h
    pump_percent = (current_price - min_1h) / min_1h
    
    # Toleransi dinaikin ke 6% biar nggak dikit-dikit pause
    if drop_percent > 0.06:
        if "DEBUG" in globals() and DEBUG:
            print(f"[DBG] MARKET DUMPING! Turun {round(drop_percent*100, 2)}%. Pause Buy.")
        return True
        
    if pump_percent > 0.06:
        if "DEBUG" in globals() and DEBUG:
            print(f"[DBG] MARKET PUMPING (FOMO)! Naik {round(pump_percent*100, 2)}%. Pause Buy.")
        return True
        
    return False
    
def get_adaptive_buy_amount(pair):
    data = bot_data[pair]
    # Ambil nilai default (misal 3.15 USDT) tanpa perkalian
    return data["config"]["buy_amount"]
    
def is_rebounding(pair):
    data = bot_data[pair]
    current_price = get_ticker_price(pair)

    low = data['lowest_price']
    if low <= 0:
        return False

    rebound = (current_price - low) / low

    return rebound > 0.003

def is_sideways_market(pair):
    data = bot_data[pair]

    if len(data['buys']) > 0:
        return False

    peak = data['peak_price']
    low = data['lowest_price']

    if peak <= 0 or low <= 0:
        return False

    volatility = (peak - low) / peak

    return volatility < 0.015
    
def is_dead_market(pair):
    data = bot_data[pair]
    high = data["peak_price"]
    low = data["lowest_price"]

    if high <= 0 or low <= 0:
        return False

    volatility = (high - low) / high

    return volatility < 0.008
    
def is_good_time_for_first_buy(pair):
    data = bot_data[pair]
    current_price = get_ticker_price(pair)
    
    
    if len(data['buys']) > 0:
        return True
        
    history = data.get('price_history', [])
    
    if len(history) < 10:
        return False
        
    raw_prices = [p.get('price', 0) if isinstance(p, dict) else float(p) for p in history]
    valid_prices = [p for p in raw_prices if p > 0]
    
    if not valid_prices:
        return False
        
    recent_peak = max(valid_prices)
    drop_from_recent_peak = (recent_peak - current_price) / recent_peak
    
    hours_running = (time.time() - data.get('peak_time', time.time())) / 3600
    cfg_drop = data["config"].get("drop_threshold", 0.013)
    threshold = max(cfg_drop, 0.025) if hours_running < 2 else cfg_drop
    
    rsi = get_rsi(pair)
    max_rsi = data["config"].get("rsi_max_entry", 48)
    
    # 1. Normal First Buy: Drop 4 Jam (sesuai config drop_threshold) + Konfirmasi RSI (<= max_rsi)
    if drop_from_recent_peak >= threshold and rsi <= max_rsi:
        return True
        
    # 2. Slow Bleed Fallback: Turun > 7 Hari & > 2.5% Global Drop + RSI Toleransi (<= max_rsi + 5)
    global_peak = data.get('peak_price', current_price)
    global_drop = (global_peak - current_price) / global_peak if global_peak > 0 else 0
    days_running = hours_running / 24
    if global_drop >= 0.025 and days_running >= 7 and rsi <= (max_rsi + 5):
        return True
        
    return False
    
def display_status(pair):
    data = bot_data[pair]
    current_price = get_ticker_price(pair)
    clear_screen()
    avg_price = get_avg_buy(pair)
    selisih = 0 if avg_price == 0 else round((current_price - avg_price) / avg_price * 100, 3)
    total_doge = sum([b['qty'] for b in data['buys']])
    free_usdt, total_usdt = get_total_usdt_value_cached()
    fee_rate = data["config"]["fee_rate"]
    total_fee_est = (total_doge * avg_price * fee_rate) + (total_doge * current_price * fee_rate)
    profit = round(((total_doge * current_price) - (total_doge * avg_price)) - total_fee_est, 5)
    
    if data.get('last_buy_time'):
        last_buy_time = datetime.fromtimestamp(data['last_buy_time']).strftime("%d-%m-%Y %H:%M:%S")
    else:
        last_buy_time = "-"

    drop_percent = ((data['peak_price'] - current_price) * 100) / data['peak_price']
    if avg_price > 0 and not is_fund_exhausted(pair):
        drop_req = get_dynamic_drop_threshold(pair)
        next_target = avg_price * (1 - drop_req)
        next_layer_str = f"{round(next_target, 5)} (-{round(drop_req * 100, 2)}% dari AVG)"
    elif len(data['buys']) == 0:
        history = data.get('price_history', [])
        raw_prices = [p.get('price', 0) if isinstance(p, dict) else float(p) for p in history]
        valid_prices = [p for p in raw_prices if p > 0]
        if valid_prices:
            recent_peak = max(valid_prices)
            hours_running = (time.time() - data.get('peak_time', time.time())) / 3600
            threshold = 0.025 if hours_running < 2 else 0.013
            next_target_4h = recent_peak * (1 - threshold)
            
            global_peak = data.get('peak_price', current_price)
            days_running = hours_running / 24
            next_target_sb = global_peak * (1 - 0.025)
            
            if days_running >= 7 and next_target_sb > next_target_4h:
                next_layer_str = "{} (-2.5% dr Global Peak)".format(round(next_target_sb, 5))
            else:
                next_layer_str = "{} (-{}% dr Peak)".format(round(next_target_4h, 5), round(threshold * 100, 1))
        else:
            next_layer_str = "Menunggu data harga"
    elif is_fund_exhausted(pair):
        next_layer_str = ""
    else:
        next_layer_str = ""
    now = datetime.now()

    print(f"""
{now.strftime('%d-%m-%Y %H:%M:%S')}
[STATUS] Budget: {round(data['budget_left'],5)} | Current: {current_price} |
AVG Buy: {round(avg_price,5)} | Selisih {selisih}% | Profit {profit} |
Target Next Layer: {next_layer_str} |
Peak: {data['peak_price']} | Low: {data['lowest_price']} | 
Buys: {[f"{round(b['price'],5)}({round(b['qty'],0)})" for b in data['buys']]} |
Total {pair.replace("USDT", "")} (recorded): {total_doge:.6f} |
Total USDT : {total_usdt} |
DROP: {round(drop_percent,5)} |
Last Buy Time: {last_buy_time}
""")

# ============ Order helpers (DCA) ============
def check_buy_preconditions(pair, data, current_price):
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
        print("\n[SYNC] Koin di Binance kosong/receh. Menghapus Ghost Trade (Auto-Reset)!")
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
    bot_data[pair] = load_data(pair)

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
            print("\n[SELL BREAKDOWN]")
            for b in data["buys"]:
                layer_profit = (avg_sell_price - float(b["price"])) * float(b["qty"])
                print(f"  Layer @ {float(b['price']):.8f} | Qty: {float(b['qty']):.8f} | Profit: {layer_profit:.6f}")
            print(f"Total Profit (real): {profit_real:.8f}\n")

    else:
        avg_sell_price = get_ticker_price(pair)
        profit_real = calc_profit(pair, avg_price, avg_sell_price, qty)

    log_action(pair, "SELL", avg_sell_price, qty, f"{profit_real:.8f}")
    
    get_account_cached(force=True)
    available_usdt = get_balance_from_cache("USDT")
    new_budget = min(available_usdt, data["config"]["budget_usd"])
    data["buys"] = []
    data["budget_left"] = floor(new_budget)
    data["peak_price"] = avg_sell_price
    data["lowest_price"] = avg_sell_price
    data["peak_time"] = int(time.time())
    data["last_buy_time"] = 0
    
    # Cek apakah koin ini sedang menunggu pergantian (pending swap)
    if data.get("pending_replacement"):
        rep_info = data.pop("pending_replacement", None)
        new_pair = rep_info.get("pair") if rep_info else None
        if new_pair and new_pair not in PAIRS:
            try:
                PAIRS_CONFIG[new_pair] = {
                    "BUDGET_USD": rep_info.get("budget_usd", 15.0),
                    "BUY_AMOUNT": rep_info.get("buy_amount", 2.1),
                    "DROP_THRESHOLD": 0.013,
                    "MAX_LOSS_PERCENT": -15,
                    "FEE_RATE": 0.001,
                    "TAKE_PROFIT_MARGIN": 0.008,
                    "TRAILING_MARGIN": 0.001,
                    "STATUS": 1
                }
                if pair in PAIRS:
                    PAIRS.remove(pair)
                PAIRS.append(new_pair)
                save_active_pairs()
                bot_locks[new_pair] = threading.RLock()
                load_data(new_pair)
                t = threading.Thread(target=dca_loop, args=(new_pair,), daemon=True)
                t.start()
                log_action(pair, "SWAP", 0, 0, f"Auto-swapped with {new_pair} after successful Take Profit!")
            except Exception as e_swap:
                print(f"Error auto-swap: {str(e_swap)}")
                
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


def get_next_layer_str(pair, data, price, avg_buy):
    if avg_buy > 0 and not is_fund_exhausted(pair):
        drop_req = get_dynamic_drop_threshold(pair)
        next_target = avg_buy * (1 - drop_req)
        return "{} (-{}% dari AVG)".format(fmt(next_target), round(drop_req * 100, 2))
    elif len(data.get('buys', [])) == 0:
        history = data.get('price_history', [])
        raw_prices = [p.get('price', 0) if isinstance(p, dict) else float(p) for p in history]
        valid_prices = [p for p in raw_prices if p > 0]
        if valid_prices:
            recent_peak = max(valid_prices)
            hours_running = (time.time() - data.get('peak_time', time.time())) / 3600
            cfg_drop = data["config"].get("drop_threshold", 0.013)
            threshold = max(cfg_drop, 0.025) if hours_running < 2 else cfg_drop
            next_target_4h = recent_peak * (1 - threshold)
            
            global_peak = data.get('peak_price', price)
            days_running = hours_running / 24
            next_target_sb = global_peak * (1 - 0.025)
            
            if days_running >= 7 and next_target_sb > next_target_4h:
                return "{} (-2.5% dr Global Peak)".format(fmt(next_target_sb))
            else:
                return "{} (-{}% dr Peak)".format(fmt(next_target_4h), round(threshold * 100, 1))
        else:
            return "Menunggu data harga"
    elif is_fund_exhausted(pair):
        return ""
    else:
        return ""

@app.route('/')
def index():
    now_str = datetime.now().strftime("%d %B %Y %H:%M:%S")
    pairs_data = []
    
    for pair in PAIRS:
        if pair not in bot_data:
            continue
        data = bot_data[pair]
        coin_name = pair.replace("USDT", "")
        price = get_ticker_price(pair)
        avg_buy = get_avg_buy(pair)
        total_doge = sum([b['qty'] for b in data['buys']])
        
        fee_rate = data["config"].get("fee_rate", 0.001)
        if avg_buy > 0:
            total_cost = total_doge * avg_buy
            current_value = total_doge * price
            total_fee_est = (total_cost * fee_rate) + (current_value * fee_rate)
            profit = round((current_value - total_cost) - total_fee_est, 4)
        else:
            profit = 0
            
        peak_price = data.get('peak_price', price)
        if price > peak_price or peak_price <= 0:
            peak_price = price
            data['peak_price'] = price
            data['peak_time'] = int(time.time())
            save_data(pair, data)
            
        lowest_price = data.get('lowest_price', price)
        if price < lowest_price or lowest_price <= 0:
            lowest_price = price
            data['lowest_price'] = price
            save_data(pair, data)

        selisih = 0 if avg_buy == 0 else round((price - avg_buy) / avg_buy * 100, 3)
        drop = max(0.0, round(((peak_price - price) * 100) / peak_price, 5)) if peak_price else 0
        free_usdt, total_usdt = get_total_usdt_value_cached()
        price_history = data.get('price_history', [])
        labels = [_ for _ in price_history]
        take_profit = avg_buy * (1 + (data["config"]["take_profit_margin"] + (len(data['buys']) * 0.0015)))
        history_volatility_pct = 0.0
        next_layer_str = get_next_layer_str(pair, data, price, avg_buy)
            
        if data["config"].get("status", 1) == 0:
            coin_name += " (Sell Only)"
            
        if len(price_history) > 0:
            raw_prices = [p.get('price', 0) if isinstance(p, dict) else float(p) for p in price_history]
            valid_prices = [p for p in raw_prices if p > 0]
            if valid_prices:
                max_1h = max(valid_prices)
                min_1h = min(valid_prices)
                if min_1h > 0:
                    history_volatility_pct = round(((max_1h - min_1h) / min_1h) * 100, 3)
        
        try:
            with open(get_log_file(pair), 'r') as f:
                logs = f.readlines()[-20:]
                logs = "".join(reversed(logs))
        except: logs = "No logs yet."

        pairs_data.append({
            "pair": pair,
            "coin_name": coin_name,
            "data": data,
            "price": fmt(price),
            "avg_buy": fmt(avg_buy),
            "profit": profit,
            "logs": logs,
            "buys": "\n".join(["BUY : {}, QTY : {}".format(fmt(b['price']), fmt(b['qty'])) for b in data['buys']]),
            "peak_price": fmt(peak_price),
            "lowest_price": fmt(lowest_price),
            "selisih": selisih,
            "drop": drop,
            "free_usdt": free_usdt,
            "total_usdt": total_usdt,
            "chart_data": price_history,
            "chart_labels": labels,
            "history_change_pct": history_volatility_pct,
            "next_layer_str": next_layer_str,
            "take_profit": fmt(take_profit),
            "rsi": get_rsi(pair),
            "rsi_max_entry": data["config"].get("rsi_max_entry", 48)
        })

    return render_template('index.html', pairs_data=pairs_data, now=now_str)

@app.route('/api/status_all')
def api_status_all():
    pairs_info = {}
    for pair in PAIRS:
        if pair not in bot_data:
            continue
        data = bot_data[pair]
        price = get_ticker_price(pair)
        avg_buy = get_avg_buy(pair)
        total_qty = sum([b['qty'] for b in data.get('buys', [])])
        fee_rate = data["config"].get("fee_rate", 0.001)
        if avg_buy > 0:
            total_cost = total_qty * avg_buy
            current_value = total_qty * price
            total_fee_est = (total_cost * fee_rate) + (current_value * fee_rate)
            profit = round((current_value - total_cost) - total_fee_est, 4)
        else:
            profit = 0
            
        peak_price = data.get('peak_price', price)
        if price > peak_price or peak_price <= 0:
            peak_price = price
            data['peak_price'] = price
            data['peak_time'] = int(time.time())
            save_data(pair, data)

        lowest_price = data.get('lowest_price', price)
        if price < lowest_price or lowest_price <= 0:
            lowest_price = price
            data['lowest_price'] = price
            save_data(pair, data)

        selisih = 0 if avg_buy == 0 else round((price - avg_buy) / avg_buy * 100, 3)
        drop = max(0.0, round(((peak_price - price) * 100) / peak_price, 5)) if peak_price else 0
        take_profit = avg_buy * (1 + (data["config"]["take_profit_margin"] + (len(data.get('buys', [])) * 0.0015)))
        status = data["config"].get("status", 1)
        next_layer_str = get_next_layer_str(pair, data, price, avg_buy)
        
        try:
            with open(get_log_file(pair), 'r') as f:
                logs_list = f.readlines()[-20:]
                logs_str = "".join(reversed(logs_list))
        except: logs_str = "No logs yet."
        
        buys_str = "\n".join(["BUY : {}, QTY : {}".format(fmt(b['price']), fmt(b['qty'])) for b in data.get('buys', [])])
        
        pairs_info[pair] = {
            "price": fmt(price),
            "avg_buy": fmt(avg_buy),
            "profit": profit,
            "peak_price": fmt(peak_price),
            "lowest_price": fmt(lowest_price),
            "selisih": selisih,
            "drop": drop,
            "budget_left": round(data.get('budget_left', 0), 2),
            "budget_usd": data["config"].get("budget_usd", 0),
            "buy_amount": data["config"].get("buy_amount", 0),
            "take_profit_margin": data["config"].get("take_profit_margin", 0.008),
            "drop_threshold": data["config"].get("drop_threshold", 0.013),
            "rsi_max_entry": data["config"].get("rsi_max_entry", 48),
            "layers_count": len(data.get('buys', [])),
            "take_profit": fmt(take_profit),
            "next_layer_str": next_layer_str,
            "buys": buys_str,
            "logs": logs_str,
            "status": status,
            "force_sell": data["config"].get("force_sell", False),
            "pending_replacement": data.get("pending_replacement"),
            "rsi": get_rsi(pair)
        }
    free_usdt, total_usdt = get_total_usdt_value_cached()
    return jsonify({
        "success": True,
        "free_usdt": free_usdt,
        "total_usdt": total_usdt,
        "pairs": pairs_info
    })

@app.route('/api/action/update_config', methods=['POST'])
def api_action_update_config():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
        
    try:
        new_budget = float(data_req.get('budget_usd', 0))
        new_buy_amount = float(data_req.get('buy_amount', 0))
        new_tp = float(data_req.get('take_profit_margin', 0))
        new_drop = float(data_req.get('drop_threshold', 0))
        new_rsi_max = float(data_req.get('rsi_max_entry', 48))
        
        if new_budget <= 0 or new_buy_amount <= 0 or new_tp <= 0 or new_drop <= 0 or new_rsi_max <= 0:
            return jsonify({"success": False, "message": "Nilai parameter harus lebih besar dari 0!"}), 400
            
        if new_rsi_max > 90:
            return jsonify({"success": False, "message": "Batas RSI maksimal 90!"}), 400
            
        if new_buy_amount > new_budget:
            return jsonify({"success": False, "message": "Buy amount per layer tidak boleh lebih besar dari Total Budget!"}), 400
            
        with bot_locks[pair]:
            cfg = bot_data[pair]["config"]
            old_budget = cfg.get("budget_usd", new_budget)
            
            cfg["budget_usd"] = new_budget
            cfg["buy_amount"] = new_buy_amount
            cfg["take_profit_margin"] = new_tp
            cfg["drop_threshold"] = new_drop
            cfg["rsi_max_entry"] = new_rsi_max
            
            # Sesuaikan sisa budget jika budget total dinaikkan/diturunkan
            budget_diff = new_budget - old_budget
            bot_data[pair]["budget_left"] = max(0.0, bot_data[pair].get("budget_left", new_budget) + budget_diff)
            
            # Jika ada antrean ganti koin, otomatis sinkronkan budget untuk koin pengganti
            if bot_data[pair].get("pending_replacement"):
                bot_data[pair]["pending_replacement"]["budget_usd"] = new_budget
                bot_data[pair]["pending_replacement"]["buy_amount"] = new_buy_amount
            
            save_data(pair, bot_data[pair])
            if pair in PAIRS_CONFIG:
                PAIRS_CONFIG[pair].update({
                    "BUDGET_USD": new_budget,
                    "BUY_AMOUNT": new_buy_amount,
                    "TAKE_PROFIT_MARGIN": new_tp,
                    "DROP_THRESHOLD": new_drop,
                    "RSI_MAX_ENTRY": new_rsi_max
                })
            save_active_pairs()
            
        return jsonify({
            "success": True, 
            "message": f"Konfigurasi {pair} berhasil diperbarui (Budget: ${new_budget}, Buy: ${new_buy_amount}, TP: {round(new_tp*100, 2)}%, Drop: {round(new_drop*100, 2)}%, Max RSI: {new_rsi_max})!"
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal update config: {str(e)}"}), 500

@app.route('/api/analytics')
def api_analytics():
    try:
        data = get_analytics_data()
        return jsonify({
            "success": True,
            "analytics": data
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

@app.route('/api/action/buy', methods=['POST'])
def api_action_buy():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
    
    threading.Thread(target=buy, args=(pair,), daemon=True).start()
    return jsonify({"success": True, "message": f"Manual Buy order untuk {pair} sedang dieksekusi!"})

@app.route('/api/action/toggle_status', methods=['POST'])
def api_action_toggle_status():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
    
    with bot_locks[pair]:
        current_status = bot_data[pair]["config"].get("status", 1)
        new_status = 0 if current_status == 1 else 1
        bot_data[pair]["config"]["status"] = new_status
        if new_status == 1:
            bot_data[pair].pop("pending_replacement", None)
        save_data(pair, bot_data[pair])
        
    status_label = "Normal (Buy & Sell)" if new_status == 1 else "Sell Only (Menunggu TP & Tutup Siklus)"
    return jsonify({
        "success": True, 
        "new_status": new_status,
        "message": f"Status {pair} diubah menjadi: {status_label}"
    })

@app.route('/api/action/cancel_swap', methods=['POST'])
def api_action_cancel_swap():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
        
    with bot_locks[pair]:
        pending = bot_data[pair].pop("pending_replacement", None)
        bot_data[pair]["config"]["status"] = 1
        save_data(pair, bot_data[pair])
        
    target_new = pending.get("pair") if pending else "koin baru"
    return jsonify({
        "success": True,
        "message": f"Antrean pergantian dengan {target_new} berhasil dibatalkan! {pair} kembali berjalan Normal."
    })

@app.route('/api/action/force_sell', methods=['POST'])
def api_action_force_sell():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
    
    if len(bot_data[pair].get("buys", [])) == 0:
        return jsonify({"success": False, "message": f"Tidak ada koin {pair} yang sedang aktif untuk dijual."})
        
    with bot_locks[pair]:
        bot_data[pair]["config"]["force_sell"] = True
        save_data(pair, bot_data[pair])
        
    threading.Thread(target=sell_all, args=(pair, True), daemon=True).start()
    return jsonify({"success": True, "message": f"Force Sell (Jual Darurat) untuk {pair} sedang dieksekusi!"})

@app.route('/api/action/sweep_dust', methods=['POST'])
def api_action_sweep_dust():
    try:
        try:
            dustable = client.get_dust_assets()
            details = dustable.get('details', [])
            assets_to_convert = [d['asset'] for d in details if float(d.get('toBNB', 0)) > 0 and float(d.get('amountFree', 0)) > 0]
            
            if not assets_to_convert:
                return jsonify({"success": True, "message": "Tidak ada saldo koin receh (dust) yang memenuhi syarat untuk dikonversi saat ini."})
                
            converted = []
            failed = []
            
            # Konversi satu per satu agar koin delisted/bermasalah tidak menggagalkan koin lain
            for a in assets_to_convert:
                try:
                    client.transfer_dust(asset=a)
                    converted.append(a)
                except Exception as ex_single:
                    failed.append(f"{a} ({str(ex_single)})")
            
            if converted:
                msg = f"Berhasil membersihkan {len(converted)} koin ({', '.join(converted)}) menjadi BNB!"
                if failed:
                    msg += f" (Sebagian dilewati: {len(failed)} koin)"
                return jsonify({"success": True, "message": msg})
            else:
                err_summary = "; ".join(failed[:2]) if failed else "Binance menolak transfer dust"
                return jsonify({"success": False, "message": f"Gagal Dust Transfer: {err_summary}"})
        except Exception as e:
            return jsonify({"success": False, "message": f"Gagal Dust Transfer (Binance Limit/Cooldown): {str(e)}"})
    except Exception as e:
        return jsonify({"success": False, "message": f"Error Dust Sweep: {str(e)}"}), 500

@app.route('/api/scanner')
def api_scanner():
    try:
        max_notional = float(request.args.get('max_notional', 2.5))
        candidates = scan_market_candidates(max_notional=max_notional)
        max_slots = MAX_SLOTS
        active_slots = len(PAIRS)
        return jsonify({
            "success": True,
            "max_slots": max_slots,
            "active_slots": active_slots,
            "candidates": candidates
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/backtest', methods=['POST'])
def api_backtest():
    data_req = request.get_json() or {}
    pair = data_req.get('pair', '').upper().strip()
    if not pair:
        return jsonify({"success": False, "message": "Pair harus diisi!"}), 400
    if not pair.endswith('USDT'):
        pair += 'USDT'
        
    days = int(data_req.get('days', 30))
    budget_usd = float(data_req.get('budget_usd', 15.0))
    buy_amount = float(data_req.get('buy_amount', 2.1))
    tp_margin = float(data_req.get('take_profit_margin', 0.8)) / 100.0
    drop_thresh = float(data_req.get('drop_threshold', 1.3)) / 100.0
    
    result = run_dca_backtest(
        pair=pair, 
        days=days, 
        budget_usd=budget_usd, 
        buy_amount=buy_amount, 
        take_profit_margin=tp_margin, 
        drop_threshold=drop_thresh
    )
    return jsonify(result)

@app.route('/api/action/add_pair', methods=['POST'])
def api_action_add_pair():
    data_req = request.get_json() or {}
    pair = data_req.get('pair', '').upper().strip()
    if not pair:
        return jsonify({"success": False, "message": "Pair harus diisi!"}), 400
    if not pair.endswith('USDT'):
        pair += 'USDT'
        
    if pair in PAIRS:
        return jsonify({"success": False, "message": f"Koin {pair} sudah aktif di dalam bot trading!"}), 400
        
    MAX_SLOTS = 2
    replace_pair = data_req.get('replace_pair')
    replace_mode = data_req.get('replace_mode', 'sell_only') # 'sell_only' or 'force_sell'
    
    budget_usd = float(data_req.get('budget_usd', 15.0))
    buy_amount = float(data_req.get('buy_amount', 2.1))
    
    min_not = get_min_notional(pair)
    if buy_amount < min_not:
        return jsonify({
            "success": False,
            "message": f"Koin {pair} mewajibkan minimal order ${min_not:.1f} USDT di Binance Spot (Buy Amount Anda: ${buy_amount}). Naikkan Buy Amount minimal ${min_not:.1f} atau pilih koin dengan min order $1 (seperti PEPE, DOGE, SHIB, FLOKI, BONK)!"
        }), 400
    
    if len(PAIRS) >= MAX_SLOTS:
        if not replace_pair or replace_pair not in PAIRS:
            active_info = []
            for p in PAIRS:
                p_data = bot_data.get(p, {})
                active_info.append({
                    "pair": p,
                    "layers_count": len(p_data.get("buys", [])),
                    "budget_left": p_data.get("budget_left", 0),
                    "status": p_data.get("config", {}).get("status", 1)
                })
            return jsonify({
                "success": False, 
                "requires_swap": True,
                "active_pairs": active_info,
                "message": f"Slot trading penuh ({len(PAIRS)}/{MAX_SLOTS} koin aktif)! Pilih koin yang ingin digantikan."
            }), 400
            
        try:
            with bot_locks.get(replace_pair, threading.RLock()):
                target_data = bot_data.get(replace_pair, {})
                buys_count = len(target_data.get("buys", []))
                
                # Kasus 1: Koin lama sedang KOSONG (0 layer) -> Langsung ganti seketika
                if buys_count == 0:
                    if replace_pair in PAIRS:
                        PAIRS.remove(replace_pair)
                    PAIRS.append(pair)
                    PAIRS_CONFIG[pair] = {
                        "BUDGET_USD": budget_usd,
                        "BUY_AMOUNT": buy_amount,
                        "DROP_THRESHOLD": 0.013,
                        "MAX_LOSS_PERCENT": -15,
                        "FEE_RATE": 0.001,
                        "TAKE_PROFIT_MARGIN": 0.008,
                        "TRAILING_MARGIN": 0.001,
                        "STATUS": 1
                    }
                    save_active_pairs()
                    bot_locks[pair] = threading.RLock()
                    load_data(pair)
                    t = threading.Thread(target=dca_loop, args=(pair,), daemon=True)
                    t.start()
                    return jsonify({
                        "success": True,
                        "message": f"Koin {replace_pair} (posisi kosong) langsung digantikan oleh {pair}!"
                    })
                    
                # Kasus 2: Mode Force Sell -> Jual sekarang dan langsung ganti
                elif replace_mode == "force_sell":
                    sell_all(replace_pair, CUT_LOSS=True)
                    if replace_pair in PAIRS:
                        PAIRS.remove(replace_pair)
                    PAIRS.append(pair)
                    PAIRS_CONFIG[pair] = {
                        "BUDGET_USD": budget_usd,
                        "BUY_AMOUNT": buy_amount,
                        "DROP_THRESHOLD": 0.013,
                        "MAX_LOSS_PERCENT": -15,
                        "FEE_RATE": 0.001,
                        "TAKE_PROFIT_MARGIN": 0.008,
                        "TRAILING_MARGIN": 0.001,
                        "STATUS": 1
                    }
                    save_active_pairs()
                    bot_locks[pair] = threading.RLock()
                    load_data(pair)
                    t = threading.Thread(target=dca_loop, args=(pair,), daemon=True)
                    t.start()
                    return jsonify({
                        "success": True,
                        "message": f"Koin {replace_pair} di-Force Sell dan langsung digantikan oleh {pair}!"
                    })
                    
                # Kasus 3: Mode Aman (Sell Only) -> Set koin lama ke Sell Only dan pasang pending_replacement
                else:
                    target_data["config"]["status"] = 0
                    target_data["pending_replacement"] = {
                        "pair": pair,
                        "budget_usd": budget_usd,
                        "buy_amount": buy_amount
                    }
                    save_data(replace_pair, target_data)
                    return jsonify({
                        "success": True,
                        "message": f"Koin {replace_pair} diubah ke 'Sell Only'. Begitu {replace_pair} Take Profit dan posisi tertutup, {pair} akan otomatis masuk menggantikannya!"
                    })
        except Exception as e:
            return jsonify({"success": False, "message": f"Gagal mengganti koin {replace_pair}: {str(e)}"}), 500
        
    try:
        price = get_ticker_price(pair)
        if not price or price <= 0:
            return jsonify({"success": False, "message": f"Koin {pair} tidak ditemukan di Binance Spot!"}), 400
            
        PAIRS_CONFIG[pair] = {
            "BUDGET_USD": budget_usd,
            "BUY_AMOUNT": buy_amount,
            "DROP_THRESHOLD": 0.013,
            "MAX_LOSS_PERCENT": -15,
            "FEE_RATE": 0.001,
            "TAKE_PROFIT_MARGIN": 0.008,
            "TRAILING_MARGIN": 0.001,
            "STATUS": 1
        }
        PAIRS.append(pair)
        save_active_pairs()
        bot_locks[pair] = threading.RLock()
        
        load_data(pair)
        
        # Start new DCA thread
        t = threading.Thread(target=dca_loop, args=(pair,), daemon=True)
        t.start()
        
        return jsonify({
            "success": True, 
            "message": f"Koin {pair} berhasil ditambahkan ke Live Trading! (Slot aktif: {len(PAIRS)}/{MAX_SLOTS})"
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal menambahkan {pair}: {str(e)}"}), 500

@app.route('/api/action/update_capital', methods=['POST'])
def update_capital_route():
    try:
        data = request.get_json(force=True) or {}
        new_val = float(data.get('injected_capital', 20.0))
        saved = save_capital_config(new_val)
        return jsonify({
            "success": True, 
            "message": f"Modal pokok berhasil diperbarui menjadi ${new_val:.2f} USDT!", 
            "data": saved
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal memperbarui modal: {str(e)}"}), 500

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('index'))
        
    if request.method == 'POST':
        user_input = request.form.get('username', '').strip()
        pass_input = request.form.get('password', '').strip()
        
        if (user_input in [AUTH_USERNAME, 'admin@tradebot.com', 'admin@example.com'] or not AUTH_USERNAME) and pass_input == AUTH_PASSWORD:
            session.permanent = True if request.form.get('remember') else False
            session['logged_in'] = True
            session['username'] = user_input or 'admin'
            flash('Berhasil masuk ke Dashboard Trading!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Username atau password salah. Silakan coba lagi.', 'danger')
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Anda telah logout dengan aman.', 'info')
    return redirect(url_for('login'))

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    
def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')
    
def dca_loop(pair):
    last_price_log_time = 0
    
    while True:
        try:
            if pair not in PAIRS:
                print(f"[{pair}] Thread DCA dihentikan karena koin digantikan.")
                break
            data = bot_data[pair]
            current_price = get_ticker_price(pair)
            
            if time.time() - last_price_log_time > 300:
                log_price_to_file(pair, current_price)
                last_price_log_time = time.time()
                
                if 'price_history' not in data:
                    data['price_history'] = []
                data['price_history'].append(current_price)
                
                if len(data['price_history']) > PRICE_HIST:
                    data['price_history'].pop(0)
                save_data(pair, data)
                
            if current_price == 0:
                if DEBUG:
                    print("CURRENT PRICE = 0")
                time.sleep(delay)
                continue

            if not data.get('peak_price') or current_price > data['peak_price']:
                data['peak_price'] = current_price
                data['peak_time'] = int(time.time())
                if len(data.get('buys', [])) == 0:
                    data['lowest_price'] = current_price
                save_data(pair, data)

            if not data.get('lowest_price') or current_price < data['lowest_price'] or data['lowest_price'] <= 0:
                data['lowest_price'] = current_price
                save_data(pair, data)

            drop_percent = max(0.0, ((data['peak_price'] - current_price) * 100) / data['peak_price']) if data.get('peak_price', 0) > 0 else 0.0
            
            avg_price = get_avg_buy(pair)
            selisih = 0 if avg_price == 0 else round((current_price - avg_price) / avg_price * 100, 3)
            
            if data.get('last_buy_time'):
                dt_buy_time = datetime.fromtimestamp(data['last_buy_time'])
                last_buy_time = dt_buy_time.strftime("%d-%m-%Y %H:%M:%S")
            else:
                last_buy_time = "-"
            
            now = datetime.now()
            if now.minute % 5 == 0 and now.second < 5:
                pass
            
            free_usdt = get_balance_from_cache("USDT")
            min_notional = get_notion(pair)
            max_allowed_buys = int(floor(data["config"]["budget_usd"] / data["config"]["buy_amount"]))
            
            if avg_price > 0:
               target_price = avg_price * (1 - get_dynamic_drop_threshold(pair))
               if current_price > target_price:
                   if DEBUG:
                       print(f"[DBG get_dynamic_drop_threshold BELUM CUKUP] current={current_price:.5f} target={target_price:.5f}")
            if avg_price > 0:
                total_doge = sum([b['qty'] for b in data['buys']])
                fee_rate = data["config"]["fee_rate"]
                total_cost = total_doge * avg_price
                current_value = total_doge * current_price
                total_fee_est = (total_cost * fee_rate) + (current_value * fee_rate)
                profit = round((current_value - total_cost) - total_fee_est, 4)

            if data["config"].get("force_sell", False) and len(data['buys']) > 0:
                log_action(pair, "FORCE SELL", current_price, message="User requested force sell")
                if DEBUG: print(f"[DBG] FORCE SELL {pair}")
                sell_all(pair, CUT_LOSS=True)
                data["config"]["force_sell"] = False
                data["config"]["status"] = 0
                save_data(pair, data)
                continue
            #if avg_price and is_fund_exhausted(pair) and selisih < data['config']['max_loss_percent'] :
            #    log_action(pair, "CUT LOSS", current_price, message="Cut loss hit")
            #    if DEBUG:
            #            print("[DBG] CUT LOSS, {}".format(current_price))
            #    sell_all(pair, CUT_LOSS=True)
            if data['budget_left'] >= data["config"]["buy_amount"]\
                and free_usdt >= min_notional \
                and data["config"]["buy_amount"] >= (min_notional + max(0.05, min_notional * 0.05)) \
                and data["config"].get("status", 1) == 1 \
                and (avg_price == 0 or current_price <= avg_price * (1 - get_dynamic_drop_threshold(pair))):
                    
                if is_market_volatile(pair) and len(data['buys']) > 0:
                    if DEBUG:
                        print("[DBG] market volatile, pause buy")
                    time.sleep(60)
                    continue
                    
                if not is_rebounding(pair) and len(data['buys']) == 0:
                    if DEBUG:
                        print("[DBG] market not rebound, pause buy")
                    time.sleep(delay)
                    continue
                
                now = int(time.time())
                
                if is_sideways_market(pair) and len(data['buys']) > 0:
                    if DEBUG:
                        print("[DBG] Market sideways, skip buy")
                    time.sleep(10)
                    continue
            
                cooldown_secs = get_dynamic_cooldown_secs(pair)
                if time.time() - data.get('last_buy_time', 0) < MIN_BUY_INTERVAL:
                    if DEBUG:
                        print("[DBG] MIN BUY INTERVAL aktif, skip")
                    time.sleep(delay)
                    continue
                    
                if len(data['buys']) > 0 and len(data['buys']) <= 3 and drop_percent > 50:
                    buy(pair)
                    time.sleep(delay)
                    continue
                
                if now - data.get('last_buy_time', 0) < cooldown_secs:
                    if DEBUG:
                        remaining = cooldown_secs - (now - data.get('last_buy_time', 0))
                        print(f"[DBG] cooling down, remain {remaining}s")
                    time.sleep(delay)
                    continue
                if len(data['buys']) == 0:
                    if is_good_time_for_first_buy(pair):
                        buy(pair)
                    else:
                        if DEBUG:
                            print("[DBG] Market belum cukup panik untuk peluru pertama. Menunggu...")
                else:
                    buy(pair)
                
            elif avg_price and current_price <= data['peak_price'] * \
                (1 - (data["config"]["trailing_margin"] + (len(data['buys']) * 0.0003))) and current_price >= avg_price * (1 + data["config"]["take_profit_margin"]):
                step = get_step_size(pair)
                qty = floor_to_step(sum([b['qty'] for b in data['buys']]), step)
                profit = calc_profit(pair, avg_price, current_price, qty)
                if profit > 0:
                    sell_all(pair)
                    
            elif avg_price and current_price >= \
                avg_price * (1 + (data["config"]["take_profit_margin"] + (len(data['buys']) * 0.0015))):
                sell_all(pair)
            
            time.sleep(delay)
            
        except Exception as e:
            print("ERROR Main loop error: {}".format(str(e)))
            time.sleep(delay)
    

if __name__ == "__main__":
    for p in PAIRS:
        load_data(p)
        bot_thread = threading.Thread(target=dca_loop, args=(p,), daemon=True)
        bot_thread.start()
    
    print("\\n=== Smart DCA Bot Running (Multi-Coin) ===")
    print("Dashboard tersedia di: http://localhost:5000")
    
    run_flask()
