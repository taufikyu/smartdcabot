import os
import time
import json
import logging
import sys
import random
import re
import glob
from math import floor, log10
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from decimal import Decimal, ROUND_UP, getcontext
getcontext().prec = 18
from datetime import datetime

# Load environment variables from .env file if present
def _load_env_file():
    try:
        from dotenv import load_dotenv
        _curr = os.path.dirname(os.path.abspath(__file__))
        load_dotenv(os.path.join(_curr, '.env'))
        load_dotenv(os.path.join(os.path.dirname(_curr), '.env'))
        load_dotenv()
    except Exception:
        pass
    # Fallback native parsing if keys still missing
    if not os.getenv('BINANCE_API_KEY'):
        _candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'),
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'),
            '.env'
        ]
        for c in _candidates:
            if os.path.exists(c):
                try:
                    with open(c, 'r', encoding='utf-8') as ef:
                        for line in ef:
                            line = line.strip()
                            if line and not line.startswith('#') and '=' in line:
                                k, v = line.split('=', 1)
                                k = k.strip()
                                v = v.strip().strip('"').strip("'")
                                if k and not os.environ.get(k):
                                    os.environ[k] = v
                except Exception:
                    pass

_load_env_file()

# ============ CONFIG ============
API_KEY = os.getenv('BINANCE_API_KEY') or os.getenv('API_KEY', '')
API_SECRET = os.getenv('BINANCE_API_SECRET') or os.getenv('API_SECRET', '')

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

# Inisialisasi Database SQLite
try:
    import db
except ImportError:
    from bot import db
db.init_db()

def save_active_pairs():
    try:
        db.db_save_active_pairs(PAIRS, PAIRS_CONFIG)
    except Exception as e:
        print(f"Error saving active pairs: {e}")

def load_active_pairs():
    global PAIRS, PAIRS_CONFIG
    try:
        p_list, p_cfg = db.db_load_active_pairs()
        if p_list:
            PAIRS = p_list
        if p_cfg:
            PAIRS_CONFIG.update(p_cfg)
    except Exception as e:
        print(f"Error loading active pairs from db: {e}")

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

def format_buys_log(buys):
    if not buys:
        return "Belum ada layer aktif (Standby / Menunggu sinyal Buy)"
    lines = []
    total_qty = 0.0
    total_cost = 0.0
    for idx, b in enumerate(buys, start=1):
        price = float(b.get('price', 0))
        qty = float(b.get('qty', 0))
        val = price * qty
        total_qty += qty
        total_cost += val
        lines.append(f"[Layer {idx}] BUY : {fmt(price)} | QTY : {fmt(qty)} | ${val:.2f} USDT")
    
    avg_price = total_cost / total_qty if total_qty > 0 else 0
    lines.append("-" * 52)
    lines.append(f"TOTAL: {len(buys)} Layer Terisi | Modal Terpakai: ${total_cost:.2f} USDT | Avg Buy: {fmt(avg_price)}")
    return "\n".join(lines)

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
_EXCHANGE_INFO_CACHE = {"ts": 0, "loaded": False}

def refresh_exchange_filters_cache():
    now = time.time()
    if _EXCHANGE_INFO_CACHE["loaded"] and (now - _EXCHANGE_INFO_CACHE["ts"]) < 3600:
        return
    try:
        info = safe_api_call(client.get_exchange_info)
        for s in info.get('symbols', []):
            sym = s.get('symbol')
            for f in s.get('filters', []):
                if f.get('filterType') in ('NOTIONAL', 'MIN_NOTIONAL'):
                    _cached_notional[sym] = float(f.get('minNotional', 0))
                elif f.get('filterType') == 'LOT_SIZE':
                    _cached_step[sym] = float(f.get('stepSize', 0.1))
        _EXCHANGE_INFO_CACHE["loaded"] = True
        _EXCHANGE_INFO_CACHE["ts"] = now
    except Exception as e:
        if DEBUG: print(f"ERROR refresh_exchange_filters_cache: {e}")

def get_min_notional(symbol):
    if not symbol: return 1.0
    if symbol in _cached_notional:
        return _cached_notional[symbol]
    refresh_exchange_filters_cache()
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
    return 1.0

def get_notion(symbol):
    return get_min_notional(symbol)

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

def get_global_settings():
    path = os.path.join(BASE_DIR, "global_settings.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
                d.setdefault("auto_pilot", True)
                d.setdefault("auto_pilot_idle_rotation", True)
                d.setdefault("auto_pilot_idle_hours", 12.0)
                d.setdefault("idle_cooldown_pairs", {})
                d.setdefault("auto_rescue", True)
                d.setdefault("auto_rescue_days", 4)
                d.setdefault("auto_rescue_tp", 0)
                d.setdefault("btc_guard", True)
                return d
        except Exception:
            pass
    return {
        "auto_pilot": True,
        "auto_pilot_idle_rotation": True,
        "auto_pilot_idle_hours": 12.0,
        "idle_cooldown_pairs": {},
        "auto_compound": False,
        "btc_guard": True,
        "auto_rescue": True,
        "auto_rescue_days": 4,
        "auto_rescue_tp": 0,
        "max_slots": 3,
        "locked_pairs": []
    }

def save_global_settings(s_dict):
    path = os.path.join(BASE_DIR, "global_settings.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(s_dict, f, indent=4)
    return s_dict

def record_idle_cooldown(pair):
    try:
        s = get_global_settings()
        cooldowns = s.get("idle_cooldown_pairs", {})
        now = time.time()
        # Clean up entries older than 48 hours
        cooldowns = {p: ts for p, ts in cooldowns.items() if (now - ts) < 172800}
        cooldowns[pair] = int(now)
        s["idle_cooldown_pairs"] = cooldowns
        save_global_settings(s)
    except Exception as e:
        if DEBUG: print(f"[DBG Cooldown] Error record_idle_cooldown: {e}")


# 7-Layer Smart Pyramid Weights (Total: 100.0%)
SMART_LAYER_WEIGHTS = [0.078, 0.091, 0.113, 0.139, 0.174, 0.196, 0.209]

def get_smart_pyramid_weights(budget_usd, min_notional=1.0):
    """
    Menghitung bobot Smart Piramida adaptif.
    Jika alokasi Layer 1 < min_notional (default 1.0 USDT), kurangi jumlah layer
    secara bertahap (dari 7 ke 6, 5, 4, 3, 2, 1) sampai Layer 1 mencapai minimal min_notional.
    Bobot dinormalisasi sehingga totalnya selalu 100% (1.000).
    """
    try:
        budget = float(budget_usd)
    except (ValueError, TypeError):
        budget = 15.0
    target_min = max(1.0, float(min_notional))
    
    for n in range(len(SMART_LAYER_WEIGHTS), 0, -1):
        sub = SMART_LAYER_WEIGHTS[:n]
        sub_sum = sum(sub)
        if sub_sum <= 0:
            continue
        weights = [round(w / sub_sum, 4) for w in sub]
        weights[-1] = round(1.0 - sum(weights[:-1]), 4)
        l1_amt = round(budget * weights[0], 2)
        if l1_amt >= target_min or n == 1:
            return weights
    return [1.0]


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
    except (ValueError, TypeError):
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
    seen_transactions = set()
    
    log_files = [f for f in glob.glob(os.path.join(BASE_DIR, "trade_log_*.txt")) 
                 if not any(x in os.path.basename(f).lower() for x in ['backup', '(1)', '(2)', 'copy', 'rescuepair', 'recycle_test', 'testusdt', 'compusdt', 'tstusdt', 'testpair'])]
    if not log_files:
        main_log = os.path.join(BASE_DIR, "trade_log.txt")
        if os.path.exists(main_log): log_files = [main_log]
        
    total_realized_profit = 0.0
    total_trades_count = 0
    
    sell_pattern = re.compile(
        r'\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\]\s+'
        r'(SELL|PARTIAL_TP|MANUAL_RECYCLE|FORCED_SELL_CUTLOSS|FORCE SELL|CUT LOSS|TAKE PROFIT)\s+'
        r'\|\s+Price:\s+([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+'
        r'\|\s+Qty:\s+([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+'
        r'\|\s+Profit:\s+([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)'
        r'(?:\s+\|\s*(.*))?'
    )
    
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
                        p_action = match.group(3)
                        p_price = float(match.group(4))
                        p_qty = float(match.group(5))
                        p_profit = float(match.group(6))
                        p_msg = (match.group(7) or '').strip() if match.lastindex >= 7 else ''
                        
                        tx_key = (dt_date, dt_time, pair_from_file, round(p_price, 8), round(p_qty, 8))
                        if tx_key in seen_transactions:
                            continue
                        seen_transactions.add(tx_key)
                        
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
                            'action': p_action,
                            'price': p_price,
                            'qty': p_qty,
                            'profit': round(p_profit, 6),
                            'message': p_msg
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
_SCANNER_TTL = 180.0  # Cache hasil scan selama 3 menit untuk hemat API Binance

_AUTOPILOT_CACHE = {}
_AUTOPILOT_TTL = 180.0  # Cache evaluasi pemenang autopilot selama 3 menit

def scan_market_candidates(min_volume_usd=1_000_000, max_notional=2.5, max_results=12,
                           budget_usd=15.0, buy_amount=2.1, dca_mode="smart",
                           drop_threshold=0.013, take_profit_margin=0.008):
    """
    Memindai pasar Binance Spot dengan Precision Unified Engine 2.0:
    - Menggunakan konfigurasi DCA real (budget, buy_amount, dca_mode, drop_threshold, TP) dalam simulasi backtest.
    - Menyaring koin sehat (Vol > $1M, Momentum -7% s/d +5%, Min-Notional <= max_notional, Anti-Delist).
    - Single-Fetch Multi-Timeframe (1D & 3D) Backtest Evaluation.
    - Menghitung Precision Score, Frekuensi Panen (Siklus Take Profit), & Anti-Nyangkut Penalty.
    - Smart Character Badging ('👑 Rekomendasi Auto-Pilot', '🛡️ Minim Resiko', '⚡ Lebih Agresif', '⚖️ Seimbang').
    - In-Memory Cache 180s (3 menit) untuk menghemat API Binance.
    """
    now = time.time()
    cache_key = f"{min_volume_usd}_{max_notional}_{max_results}_{budget_usd}_{buy_amount}_{dca_mode}_{drop_threshold}_{take_profit_margin}"
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
            # Momentum Sehat untuk DCA Osilasi: -7.0% s/d +5.0%
            if not (-7.0 <= price_change_pct <= 5.0):
                continue
                
            current_price = float(t.get('lastPrice', 0))
            if current_price <= 0:
                continue
                
            min_not = get_min_notional(symbol)
            if max_notional and min_not > (max_notional + 0.05):
                continue
                
            coin_clean = symbol.replace('USDT', '')
            c_info = get_coin_info(coin_clean)
            vol_m = round(quote_vol / 1_000_000, 1)
            
            candidates.append({
                'symbol': symbol,
                'coin': coin_clean,
                'name': c_info['name'],
                'sector': c_info['sector'],
                'desc': c_info['desc'],
                'price': fmt(current_price),
                'change_24h': round(price_change_pct, 2),
                'volume_24h_m': vol_m,
                'min_notional': min_not,
                'raw_vol': quote_vol
            })
            
        # Urutkan kandidat potensial berdasarkan likuiditas volume
        candidates.sort(key=lambda x: -x['raw_vol'])
        top_pool = candidates[:10]
        
        scored_candidates = []
        for cand in top_pool:
            cand_sym = cand['symbol']
            try:
                # Single-fetch 864 candle (3 hari) per koin
                klines_3d = safe_api_call(client.get_klines, symbol=cand_sym, interval='5m', limit=864)
                if not klines_3d or len(klines_3d) < 50:
                    cand['score'] = 0.0
                    cand['rsi'] = get_rsi(cand_sym)
                    scored_candidates.append(cand)
                    continue
                    
                klines_1d = klines_3d[-288:] if len(klines_3d) >= 288 else klines_3d
                
                # Backtest 1D (24 Jam) & 3D (72 Jam) menggunakan parameter DCA koin
                sim_1d = run_dca_backtest(
                    pair=cand_sym, days=1, budget_usd=float(budget_usd), buy_amount=float(buy_amount),
                    take_profit_margin=float(take_profit_margin), drop_threshold=float(drop_threshold), dca_mode=str(dca_mode).lower(),
                    is_autopilot=True, preloaded_klines=klines_1d
                )
                sim_3d = run_dca_backtest(
                    pair=cand_sym, days=3, budget_usd=float(budget_usd), buy_amount=float(buy_amount),
                    take_profit_margin=float(take_profit_margin), drop_threshold=float(drop_threshold), dca_mode=str(dca_mode).lower(),
                    is_autopilot=True, preloaded_klines=klines_3d
                )
                
                rsi = get_rsi(cand_sym)
                cand['rsi'] = rsi
                
                open_1d = sim_1d.get('open_layers_at_end', 0) if sim_1d else 0
                open_3d = sim_3d.get('open_layers_at_end', 0) if sim_3d else 0
                max_3d = sim_3d.get('max_layer_reached', 0) if sim_3d else 0
                roi_1d = sim_1d.get('roi_pct', 0.0) if sim_1d else 0.0
                roi_3d = sim_3d.get('roi_pct', 0.0) if sim_3d else 0.0
                cyc_1d = sim_1d.get('completed_cycles', 0) if sim_1d else 0
                cyc_3d = sim_3d.get('completed_cycles', 0) if sim_3d else 0
                prof_1d = sim_1d.get('total_realized_profit', 0.0) if sim_1d else 0.0
                
                vol_bonus = log10(max(1.0, cand['volume_24h_m'])) * 1.5
                
                # Entry-Ready RSI Scoring (Prioritize healthy pullback 30.0 <= RSI <= 48.0)
                if 35.0 <= rsi <= 48.0:
                    rsi_bonus = 3.5
                elif 30.0 <= rsi < 35.0:
                    rsi_bonus = 2.5
                elif 48.0 < rsi <= 55.0:
                    rsi_bonus = 1.0
                elif rsi < 28.0:
                    rsi_bonus = -3.0  # Anti-falling knife / extreme dump penalty
                elif rsi > 65.0:
                    rsi_bonus = -3.0  # Anti-pucuk / overbought penalty
                else:
                    rsi_bonus = 0.0
                
                # Strict Anti-Nyangkut Penalty
                layer_penalty = (open_1d * 4.0) + (open_3d * 2.0) + (max_3d * 0.5)
                if open_1d >= 3 or (sim_1d and "OVER-LAYER" in sim_1d.get('safety_status', '')):
                    layer_penalty += 40.0
                    
                score = (roi_1d * 8.0) + (roi_3d * 3.0) + (cyc_1d * 1.8) + (cyc_3d * 0.6) + vol_bonus + rsi_bonus - layer_penalty
                
                cand['score'] = round(score, 2)
                cand['cycles_1d'] = cyc_1d
                cand['cycles_3d'] = cyc_3d
                cand['profit_1d'] = round(prof_1d, 4)
                cand['open_1d'] = open_1d
                cand['open_3d'] = open_3d
                cand['sim_1d'] = sim_1d
                cand['sim_3d'] = sim_3d
                scored_candidates.append(cand)
            except Exception as e_c:
                if DEBUG: print(f"[DBG Scanner] Error evaluating {cand_sym}: {e_c}")
                cand['score'] = 0.0
                cand['rsi'] = get_rsi(cand_sym)
                scored_candidates.append(cand)
                
        # Urutkan berdasarkan Skor Precision Auto-Pilot Tertinggi
        scored_candidates.sort(key=lambda x: (-x.get('score', 0), -x.get('raw_vol', 0)))
        
        # Berikan Smart Karakter Badges
        for idx, cand in enumerate(scored_candidates):
            vol_m = cand.get('volume_24h_m', 0.0)
            cyc = cand.get('cycles_1d', 0)
            open_1d = cand.get('open_1d', 0)
            
            if idx == 0 and open_1d < 2:
                cand['tag'] = "👑 Rekomendasi Auto-Pilot"
                cand['tag_type'] = "autopilot"
                cand['tag_desc'] = "Pilihan #1 Auto-Pilot dengan skor tertinggi perpaduan cuan, siklus cepat, dan anti-nyangkut."
            elif vol_m >= 15.0 and cyc <= 3:
                cand['tag'] = "🛡️ Minim Resiko"
                cand['tag_type'] = "lowrisk"
                cand['tag_desc'] = "Likuiditas sangat besar ($15M+), pergerakan stabil dan aman dari fluktuasi liar."
            elif cyc >= 4 or (cyc >= 3 and vol_m < 10.0):
                cand['tag'] = "⚡ Lebih Agresif"
                cand['tag_type'] = "aggressive"
                cand['tag_desc'] = "Osilasi harga cepat memicu banyak siklus Take Profit dalam 24 jam."
            else:
                cand['tag'] = "⚖️ Seimbang"
                cand['tag_type'] = "balanced"
                cand['tag_desc'] = "Kombinasi likuiditas sehat dan osilasi harga moderat."
                
        selected = scored_candidates[:max_results]
        _SCANNER_CACHE[cache_key] = {'data': selected, 'time': now}
        return selected
    except Exception as e:
        if DEBUG: print(f"[DBG Scanner] Error scanning market: {e}")
        return []

# ============ UNIFIED DCA CORE ENGINE (SHARED WITH AUTO-PILOT & SIMULATOR) ============

def calculate_dca_drop_requirement(layer_count, drop_threshold=0.013, volatility=0.0):
    """
    Rumus Tunggal: Persentase penurunan harga yang dibutuhkan untuk serok layer ke-(layer_count).
    Mendukung Dynamic Layer Stretching saat volatilitas tinggi (volatility > 0.12) atau layer dalam (>= 5).
    Tersinkronisasi 100% antara Live Trading, Backtest Simulator, dan Auto-Pilot Evaluator.
    """
    base_scale = max(0.003, float(drop_threshold)) / 0.013
    stretch_mult = 1.0
    try:
        vol = float(volatility or 0.0)
        if vol > 0.20:
            stretch_mult = 1.25
        elif vol > 0.12:
            stretch_mult = 1.15
    except Exception:
        stretch_mult = 1.0

    if layer_count <= 1:
        return 0.020 * base_scale
    elif layer_count == 2:
        return 0.035 * base_scale
    elif layer_count == 3:
        return 0.055 * base_scale
    elif layer_count == 4:
        return 0.080 * base_scale * stretch_mult
    elif layer_count == 5:
        return 0.125 * base_scale * stretch_mult
    elif layer_count == 6:
        return 0.185 * base_scale * stretch_mult
    else:
        return (0.185 + (layer_count - 6) * 0.075) * base_scale * stretch_mult

def calculate_dca_layer_amount(layer_idx, budget_usd, buy_amount=2.1, dca_mode="smart", min_notional=1.0):
    """
    Rumus Tunggal: Besaran nominal serok USDT untuk layer ke-(layer_idx).
    Mendukung mode Smart Piramida adaptif (dinamis mengurangi layer jika L1 < min_notional)
    dan Flat DCA dengan validasi minimum notional.
    """
    target_min = max(1.0, float(min_notional))
    if str(dca_mode).lower() == "flat":
        return max(target_min, float(buy_amount))
        
    weights = get_smart_pyramid_weights(budget_usd, target_min)
    if layer_idx < len(weights):
        w = weights[layer_idx]
    else:
        w = weights[-1]
    amt = round(float(budget_usd) * w, 2)
    return max(target_min, amt)

def calculate_dca_tp_target(avg_price, layer_count=1, take_profit_margin=0.008):
    """
    Rumus Tunggal: Target harga Take Profit berdasarkan harga rata-rata (AVG Buy),
    margin TP dasar, dan eskalasi dinamis per kedalaman layer (+0.15% per layer).
    """
    if avg_price <= 0:
        return 0.0
    return avg_price * (1 + (float(take_profit_margin) + (int(layer_count) * 0.0015)))

def select_autopilot_candidate(current_pair=None, budget_usd=15.0, buy_amount=2.1, dca_mode="smart", drop_threshold=0.013, take_profit_margin=0.008):
    """
    Memilih koin juara untuk rotasi Auto-Pilot menggunakan Precision Engine 2.0 (100% Selaras dengan Config Koin Saat Ini).
    """
    now = time.time()
    # Jika current_pair diberikan, ambil config real koin lama agar scanning selaras dengan strategi user
    if current_pair:
        if current_pair in bot_data:
            p_cfg = bot_data[current_pair].get("config", {})
            budget_usd = float(p_cfg.get("budget_usd", p_cfg.get("BUDGET_USD", budget_usd)))
            buy_amount = float(p_cfg.get("buy_amount", p_cfg.get("BUY_AMOUNT", buy_amount)))
            dca_mode = str(p_cfg.get("dca_mode", p_cfg.get("DCA_MODE", dca_mode))).lower()
            drop_threshold = float(p_cfg.get("drop_threshold", p_cfg.get("DROP_THRESHOLD", drop_threshold)))
            take_profit_margin = float(p_cfg.get("take_profit_margin", p_cfg.get("TAKE_PROFIT_MARGIN", take_profit_margin)))
        elif current_pair in PAIRS_CONFIG:
            p_cfg = PAIRS_CONFIG[current_pair]
            budget_usd = float(p_cfg.get("BUDGET_USD", p_cfg.get("budget_usd", budget_usd)))
            buy_amount = float(p_cfg.get("BUY_AMOUNT", p_cfg.get("buy_amount", buy_amount)))
            dca_mode = str(p_cfg.get("DCA_MODE", p_cfg.get("dca_mode", dca_mode))).lower()
            drop_threshold = float(p_cfg.get("DROP_THRESHOLD", p_cfg.get("drop_threshold", drop_threshold)))
            take_profit_margin = float(p_cfg.get("TAKE_PROFIT_MARGIN", p_cfg.get("take_profit_margin", take_profit_margin)))

    cache_key = f"{current_pair}_{budget_usd}_{buy_amount}_{dca_mode}_{drop_threshold}_{take_profit_margin}"
    if cache_key in _AUTOPILOT_CACHE and (now - _AUTOPILOT_CACHE[cache_key]['time']) < _AUTOPILOT_TTL:
        return _AUTOPILOT_CACHE[cache_key]['sym'], _AUTOPILOT_CACHE[cache_key]['cand']
        
    try:
        max_not = max(2.5, buy_amount * 1.2) if buy_amount else 2.5
        candidates = scan_market_candidates(
            min_volume_usd=1_000_000, max_notional=max_not, max_results=8,
            budget_usd=budget_usd, buy_amount=buy_amount, dca_mode=dca_mode,
            drop_threshold=drop_threshold, take_profit_margin=take_profit_margin
        )
        if not candidates:
            return None, None
            
        g_settings = get_global_settings()
        cooldowns = g_settings.get("idle_cooldown_pairs", {})
        active_cooldowns = {p: ts for p, ts in cooldowns.items() if (now - ts) < 86400}

        for cand in candidates:
            cand_sym = cand.get('symbol')
            if not cand_sym or cand_sym in PAIRS or cand_sym == current_pair:
                continue
            # Hindari koin yang baru dirotasi keluar karena idle (cooldown 24 jam)
            if cand_sym in active_cooldowns:
                continue
            # Hindari koin dengan nyangkut berlebih
            if cand.get('open_1d', 0) >= 3:
                continue
                
            _AUTOPILOT_CACHE[cache_key] = {'sym': cand_sym, 'cand': cand, 'time': now}
            return cand_sym, cand
            
        # Fallback jika semua koin terfilter
        for cand in candidates:
            cand_sym = cand.get('symbol')
            if cand_sym and cand_sym not in PAIRS and cand_sym != current_pair:
                if cand_sym in active_cooldowns:
                    continue
                return cand_sym, cand
        return None, None
    except Exception as e_ap:
        if DEBUG: print(f"[DBG AutoPilot] Error in select_autopilot_candidate: {e_ap}")
        return None, None

def run_dca_backtest(pair, days=30, budget_usd=15.0, buy_amount=2.1, take_profit_margin=0.008, drop_threshold=0.013, dca_mode="flat", is_autopilot=False, start_date=None, end_date=None, preloaded_klines=None):
    """
    Mensimulasikan strategi DCA (Smart Piramida / Flat) pada data historis Binance (7, 14, 30, 60, 100 hari atau custom date range).
    - Mode Simulator Interaktif: Menghitung hingga layer lanjutan (> nominal_layers) untuk memetakan risiko dump ekstrem & modal tambahan yang dibutuhkan.
    - Mode Auto-Pilot: Tetap ketat pada batas nominal layer (budget_usd / buy_amount).
    """
    try:
        klines = []
        is_custom_range = bool(start_date and end_date)
        interval_used = '5m'
        
        if preloaded_klines:
            klines = preloaded_klines
            interval_used = '5m'
        elif is_custom_range:
            try:
                dt_start = datetime.strptime(str(start_date).strip()[:10], "%Y-%m-%d")
                dt_end = datetime.strptime(str(end_date).strip()[:10] + " 23:59:59", "%Y-%m-%d %H:%M:%S")
                if dt_end < dt_start:
                    return {"success": False, "message": "Tanggal akhir tidak boleh lebih awal dari tanggal mulai!"}
                
                start_ts = int(dt_start.timestamp() * 1000)
                end_ts = int(dt_end.timestamp() * 1000)
                total_duration_days = max(1, int(round((end_ts - start_ts) / (86400 * 1000))))
                days = total_duration_days
                
                if days <= 2:
                    interval_used = '1m'
                elif days <= 14:
                    interval_used = '5m'
                elif days <= 45:
                    interval_used = '15m'
                elif days <= 120:
                    interval_used = '1h'
                else:
                    interval_used = '2h'
                    
                curr_start = start_ts
                loop_count = 0
                while curr_start < end_ts and loop_count < 10:
                    loop_count += 1
                    batch = safe_api_call(client.get_klines, symbol=pair, interval=interval_used, startTime=curr_start, endTime=end_ts, limit=1000)
                    if not batch:
                        break
                    klines.extend(batch)
                    curr_start = int(batch[-1][0]) + 1
                    if len(batch) < 1000 or curr_start >= end_ts:
                        break
            except Exception as e_date:
                return {"success": False, "message": f"Format tanggal tidak valid: {str(e_date)}"}
        else:
            days = min(max(1, int(days)), 365)
            if days <= 2:
                interval_used = '1m'
                candles_needed = int(days * 1440)
            elif days <= 14:
                interval_used = '5m'
                candles_needed = int(days * 288)
            elif days <= 45:
                interval_used = '15m'
                candles_needed = int(days * 96)
            elif days <= 120:
                interval_used = '1h'
                candles_needed = int(days * 24)
            else:
                interval_used = '2h'
                candles_needed = int(days * 12)
            
            # Ambil data klines secara berurutan dengan pagination (Maksimal 4-5 batch agar instan)
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
                    
        if not klines or len(klines) < 10:
            return {"success": False, "message": f"Data historis tidak mencukupi untuk {pair} pada periode yang dipilih."}
            
        start_ts = int(klines[0][0])
        end_ts = int(klines[-1][0])
        start_date_str = datetime.fromtimestamp(start_ts / 1000).strftime("%d %b %Y %H:%M")
        end_date_str = datetime.fromtimestamp(end_ts / 1000).strftime("%d %b %Y %H:%M")
        min_period_price = min(float(k[3]) for k in klines)

        fee_rate = 0.001
        buys = []
        budget_left = budget_usd
        completed_cycles = 0
        total_realized_profit = 0.0
        max_layer_reached = 0
        deepest_layer_price = 0.0
        cur_cycle_cost = 0.0
        max_cycle_cost = 0.0
        peak_price = 0.0
        rolling_prices = []

        actual_dca_mode = dca_mode
        if actual_dca_mode == "smart":
            smart_weights = get_smart_pyramid_weights(budget_usd, 1.0)
            nominal_layers = len(smart_weights)
        else:
            effective_buy = max(1.0, buy_amount)
            nominal_layers = max(1, floor(budget_usd / effective_buy)) if effective_buy > 0 else 7

        max_sim_layers = nominal_layers if is_autopilot else 15

        def get_sim_drop_req(layer_count):
            return calculate_dca_drop_requirement(layer_count, drop_threshold)

        def get_sim_layer_amount(layer_idx):
            return calculate_dca_layer_amount(layer_idx, budget_usd, buy_amount, actual_dca_mode)
        
        warmup_limit = 5 if interval_used == '1m' else 10
        for k in klines:
            c_high = float(k[2])
            c_low = float(k[3])
            c_close = float(k[4])
            
            rolling_prices.append(c_close)
            if len(rolling_prices) > 16:
                rolling_prices.pop(0)
                
            if c_high > peak_price:
                peak_price = c_high
                
            # 1. Cek Take Profit
            if len(buys) > 0:
                total_qty = sum(b['qty'] for b in buys)
                total_cost = sum(b['price'] * b['qty'] for b in buys)
                avg_price = total_cost / total_qty if total_qty > 0 else 0
                
                tp_target = calculate_dca_tp_target(avg_price, len(buys), take_profit_margin)
                
                if c_high >= tp_target:
                    sell_value = total_qty * tp_target
                    buy_fee = total_cost * fee_rate
                    sell_fee = sell_value * fee_rate
                    cycle_profit = (sell_value - total_cost) - (buy_fee + sell_fee)
                    
                    total_realized_profit += cycle_profit
                    completed_cycles += 1
                    budget_left = budget_usd
                    cur_cycle_cost = 0.0
                    buys = []
                    peak_price = c_close
                    continue
                    
            # 2. Cek Pembelian Layer
            cur_needed = get_sim_layer_amount(len(buys))
            if len(buys) == 0:
                if len(rolling_prices) >= warmup_limit:
                    recent_peak = max(rolling_prices)
                    target_first_buy = recent_peak * (1 - drop_threshold)
                    if c_low <= target_first_buy:
                        buy_price = target_first_buy
                        qty = cur_needed / buy_price
                        buys.append({'price': buy_price, 'qty': qty})
                        budget_left -= cur_needed
                        cur_cycle_cost += cur_needed
                        max_cycle_cost = max(max_cycle_cost, cur_cycle_cost)
                        if len(buys) > max_layer_reached:
                            max_layer_reached = len(buys)
                            deepest_layer_price = buy_price
                        elif len(buys) == max_layer_reached:
                            deepest_layer_price = min(deepest_layer_price, buy_price) if deepest_layer_price > 0 else buy_price
                            
                        # Instant TP check if high in the same candle reached TP target
                        tp_target = calculate_dca_tp_target(buy_price, 1, take_profit_margin)
                        if c_high >= tp_target:
                            sell_value = qty * tp_target
                            buy_fee = cur_needed * fee_rate
                            sell_fee = sell_value * fee_rate
                            cycle_profit = (sell_value - cur_needed) - (buy_fee + sell_fee)
                            total_realized_profit += cycle_profit
                            completed_cycles += 1
                            budget_left = budget_usd
                            cur_cycle_cost = 0.0
                            buys = []
                            peak_price = c_close
            else:
                total_qty = sum(b['qty'] for b in buys)
                avg_price = sum(b['price'] * b['qty'] for b in buys) / total_qty
                
                layer_count = len(buys)
                req_drop = get_sim_drop_req(layer_count)
                target_drop_price = avg_price * (1 - req_drop)
                
                can_buy = (layer_count < max_sim_layers)
                if is_autopilot:
                    can_buy = can_buy and (budget_left >= cur_needed)

                if c_low <= target_drop_price and can_buy:
                    buy_price = target_drop_price
                    qty = cur_needed / buy_price
                    buys.append({'price': buy_price, 'qty': qty})
                    budget_left -= cur_needed
                    cur_cycle_cost += cur_needed
                    max_cycle_cost = max(max_cycle_cost, cur_cycle_cost)
                    if len(buys) > max_layer_reached:
                        max_layer_reached = len(buys)
                        deepest_layer_price = buy_price
                    elif len(buys) == max_layer_reached:
                        deepest_layer_price = min(deepest_layer_price, buy_price) if deepest_layer_price > 0 else buy_price
                    
        extra_layers = max(0, max_layer_reached - nominal_layers)
        extra_capital_needed = max(0.0, max_cycle_cost - budget_usd)

        if max_layer_reached <= min(2, nominal_layers):
            safety_status = "SANGAT AMAN 🛡️"
            safety_reason = f"Koin sangat cepat rebound. Selama {days} hari pengujian, bot paling dalam hanya menyentuh Layer {max_layer_reached} (dari kapasitas {nominal_layers} layer modal Anda) lalu langsung panen Take Profit."
        elif max_layer_reached <= nominal_layers:
            safety_status = "AMAN 🟢"
            safety_reason = f"Koin sempat mengalami koreksi hingga Layer {max_layer_reached} dari alokasi {nominal_layers} layer modal Anda, dan seluruh siklus berhasil ditutup Take Profit dengan lancar."
        else:
            safety_status = f"OVER-LAYER ⚠️ (Layer {max_layer_reached}/{nominal_layers})"
            safety_reason = f"Koin mengalami penurunan tajam (*deep dump*) hingga menyentuh Layer {max_layer_reached} (melebihi batas modal {nominal_layers} layer). Butuh tambahan modal sekitar +${extra_capital_needed:.2f} USDT agar bot tidak berhenti menyerok saat crash."

        roi_pct = round((total_realized_profit / budget_usd) * 100, 2) if budget_usd > 0 else 0
        return {
            "success": True,
            "pair": pair,
            "coin_info": get_coin_info(pair.replace('USDT', '')),
            "days_tested": days,
            "start_date": start_date_str,
            "end_date": end_date_str,
            "date_range_str": f"{start_date_str} s/d {end_date_str}",
            "min_period_price": min_period_price,
            "min_period_price_fmt": fmt(min_period_price),
            "deepest_layer_price": deepest_layer_price,
            "deepest_layer_price_fmt": fmt(deepest_layer_price) if deepest_layer_price > 0 else "-",
            "total_realized_profit": round(total_realized_profit, 4),
            "roi_pct": roi_pct,
            "completed_cycles": completed_cycles,
            "max_layer_reached": max_layer_reached,
            "nominal_layers": nominal_layers,
            "extra_layers": extra_layers,
            "extra_capital_needed": round(extra_capital_needed, 2),
            "max_cycle_cost": round(max_cycle_cost, 2),
            "open_layers_at_end": len(buys),
            "safety_status": safety_status,
            "safety_reason": safety_reason,
            "is_autopilot": is_autopilot
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
        err_msg = str(e)
        if '-1021' in err_msg or 'Timestamp' in err_msg or 'recvWindow' in err_msg:
            try:
                server_time = client.get_server_time()
                time_offset = server_time['serverTime'] - int(time.time() * 1000)
                client.timestamp_offset = time_offset
                acc = client.get_account()
                ACCOUNT_CACHE['account'] = acc
                ACCOUNT_CACHE['balances'] = acc.get('balances', [])
                ACCOUNT_CACHE['ts'] = time.time()
                return acc
            except Exception as e2:
                print(f"[API ERROR] Time sync recovery failed: {e2}")
        print(f"[API ERROR] get_account_cached failed: {e}")
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

def get_notion(symbol):
    return get_min_notional(symbol)

def get_step_size(symbol):
    if not symbol: return 0.1
    if symbol in _cached_step:
        return _cached_step[symbol]
    refresh_exchange_filters_cache()
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
    if pair not in bot_locks:
        import threading
        bot_locks[pair] = threading.RLock()
    with bot_locks[pair]:
        try:
            db.db_save_pair_state(pair, d)
        except Exception as e:
            if DEBUG: print(f"Error db_save_pair_state({pair}): {e}")

def get_initial_peak_and_low(pair, cur_price=None):
    """
    Menghitung Peak Price & Lowest Price awal saat koin baru aktif (Fresh Start / Swap).
    - Menganalisis 36 candle 5m (3 jam terakhir).
    - Peak = Highest High 3 jam terakhir, dicap max +5% dari harga sekarang agar tidak terdistorsi wick anomali.
    - Lowest = Harga sekarang (agar konfirmasi rebound +0.3% mulai dihitung sejak koin masuk).
    """
    if not cur_price or cur_price <= 0:
        cur_price = get_ticker_price(pair)
    if not cur_price or cur_price <= 0:
        return 1.0, 1.0
    local_peak = cur_price
    try:
        klines = safe_api_call(client.get_klines, symbol=pair, interval='5m', limit=36)
        if klines and len(klines) >= 6:
            highs = [float(k[2]) for k in klines if float(k[2]) > 0]
            if highs:
                raw_peak = max(highs)
                # Cap peak tidak lebih dari +5% di atas cur_price dan tidak boleh < cur_price
                capped_peak = min(raw_peak, cur_price * 1.05)
                local_peak = max(capped_peak, cur_price)
    except Exception as e:
        if DEBUG: print(f"[DBG Fresh Peak] Error get_initial_peak_and_low({pair}): {e}")
        local_peak = cur_price
    return round(local_peak, 8), round(cur_price, 8)

def init_fresh_pair_data(pair, budget_usd, buy_amount, dca_mode="smart",
                         drop_threshold=0.013, take_profit_margin=0.008,
                         max_loss_percent=-15.0, fee_rate=0.001,
                         trailing_margin=0.001, rsi_max_entry=48.0):
    """
    Menginisialisasi state koin baru atau koin hasil rotasi swap:
    - Menghapus artefak lama (buys = [], budget_left = full, last_buy = 0, idle_since = now)
    - Menghitung Peak Price awal (Highest High 3 jam terakhir, dicap max +5%)
    - Menetapkan Lowest Price awal = Harga saat ini (agar konfirmasi rebound +0.3% dihitung sejak entry)
    - Mengambil riwayat candle 5m untuk RSI & DCA evaluator
    - Mewarisi seluruh parameter konfigurasi trading secara konsisten
    """
    cur_price = get_ticker_price(pair)
    init_peak, init_low = get_initial_peak_and_low(pair, cur_price)
    now_ts = int(time.time())
    
    init_hist = []
    try:
        klines = safe_api_call(client.get_klines, symbol=pair, interval='5m', limit=PRICE_HIST)
        if klines:
            init_hist = [{"time": datetime.fromtimestamp(k[0]/1000).strftime("%H:%M"), "price": float(k[4])} for k in klines]
    except Exception:
        init_hist = []

    fresh_data = {
        "buys": [],
        "price_history": init_hist,
        "budget_left": float(budget_usd),
        "peak_price": init_peak,
        "lowest_price": init_low,
        "peak_time": now_ts,
        "lowest_price_time": now_ts,
        "last_buy_time": 0,
        "idle_since": now_ts,
        "pending_replacement": None,
        "config": {
            "budget_usd": float(budget_usd),
            "buy_amount": float(buy_amount),
            "max_layer": floor(float(budget_usd) / float(buy_amount)) if float(buy_amount) > 0 else 0,
            "drop_threshold": float(drop_threshold),
            "max_loss_percent": float(max_loss_percent),
            "fee_rate": float(fee_rate),
            "take_profit_margin": float(take_profit_margin),
            "trailing_margin": float(trailing_margin),
            "status": 1,
            "dca_mode": str(dca_mode).lower(),
            "rsi_max_entry": float(rsi_max_entry),
            "force_sell": False
        }
    }
    bot_data[pair] = fresh_data
    save_data(pair, fresh_data)
    if DEBUG:
        print(f"[FRESH INIT] {pair} diinisialisasi: Peak={init_peak}, Low={init_low}, Budget={budget_usd}, Mode={dca_mode}, Drop={drop_threshold}, TP={take_profit_margin}")
    return fresh_data

def load_data(pair):
    cfg = PAIRS_CONFIG.get(pair, {})
    c_budget = cfg.get("BUDGET_USD", 15.0)
    c_buy = cfg.get("BUY_AMOUNT", 2.1)
    c_drop = cfg.get("DROP_THRESHOLD", 0.01)
    c_loss = cfg.get("MAX_LOSS_PERCENT", -15)
    c_fee = cfg.get("FEE_RATE", 0.001)
    c_tp = cfg.get("TAKE_PROFIT_MARGIN", 0.008)
    c_trail = cfg.get("TRAILING_MARGIN", 0.001)
    c_status = cfg.get("STATUS", 1)
    c_force_sell = cfg.get("FORCE_SELL", False)

    available_usdt = get_balance_from_cache("USDT")
    if c_budget > available_usdt:
        adjusted_budget = round(available_usdt, 2)
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
            "dca_mode": cfg.get("DCA_MODE", cfg.get("dca_mode", "smart")),
            "rsi_max_entry": cfg.get("RSI_MAX_ENTRY", cfg.get("rsi_max_entry", 48.0)),
            "peak_time": int(time.time()),
            "force_sell": False
        }
        # 1. Load from DB first
        d = None
        try:
            d = db.db_load_pair_state(pair, default_config=default_conf)
        except Exception as e:
            if DEBUG: print(f"Error db_load_pair_state({pair}): {e}")
            d = None
            
        # 2. Fallback to JSON if SQLite was empty and JSON exists
        if not d or (not d.get("buys") and (d.get("peak_price", 0) <= 0) and os.path.exists(data_file)):
            try:
                with open(data_file, 'r') as f:
                    d = json.load(f)
            except Exception:
                pass

        if d and ("config" in d or "buys" in d):
            if "config" not in d:
                d["config"] = default_conf.copy()
            else:
                # Update jika belum ada first buy (posisi kosong)
                if len(d.get("buys", [])) == 0:
                    for k, v in default_conf.items():
                        if k != "peak_time":
                            if k == "status" and d.get("pending_replacement"):
                                continue
                            d["config"][k] = v
                else:
                    # Jika sedang jalan (sudah buy), hanya isi key yang belum ada
                    for k, v in default_conf.items():
                        if k not in d["config"]:
                            d["config"][k] = v
            if len(d.get("buys", [])) == 0:
                if not d.get("idle_since"):
                    d["idle_since"] = int(d.get("peak_time", time.time()))
                if not d.get("peak_price") or d.get("peak_price", 0) <= 0 or not d.get("lowest_price") or d.get("lowest_price", 0) <= 0:
                    p_peak, p_low = get_initial_peak_and_low(pair)
                    d["peak_price"] = p_peak
                    d["lowest_price"] = p_low
                    d["peak_time"] = int(time.time())
                    d["lowest_price_time"] = int(time.time())
            if len(d.get("price_history", [])) < PRICE_HIST:
                try:
                    klines = safe_api_call(client.get_klines, symbol=pair, interval='5m', limit=PRICE_HIST)
                    if klines:
                        d["price_history"] = [{"time": datetime.fromtimestamp(k[0]/1000).strftime("%H:%M"), "price": float(k[4])} for k in klines]
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
                    init_history = [{"time": datetime.fromtimestamp(k[0]/1000).strftime("%H:%M"), "price": float(k[4])} for k in klines]
            except Exception:
                pass
            init_peak, init_low = get_initial_peak_and_low(pair, cur_price)
            new_data = {
                "buys": [],
                "price_history": init_history,
                "budget_left": adjusted_budget,
                "peak_price": init_peak,
                "lowest_price": init_low,
                "peak_time": int(time.time()),
                "lowest_price_time": int(time.time()),
                "last_buy_time": 0,
                "idle_since": int(time.time()),
                "config": default_conf
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
    cfg_drop = data["config"].get("drop_threshold", 0.013)
    peak = float(data.get('peak_price', 0.0) or 0.0)
    low = float(data.get('lowest_price', 0.0) or 0.0)
    volatility = ((peak - low) / peak) if (peak > 0 and low > 0) else 0.0
    return calculate_dca_drop_requirement(layer_count, cfg_drop, volatility=volatility)

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
    b_left = float(data.get('budget_left', 0.0))
    cfg = data.get('config', {})
    dca_mode = str(cfg.get('dca_mode', cfg.get('DCA_MODE', 'flat'))).lower()
    min_notional = get_notion(pair)
    # Toleransi floating point & pembulatan desimal ($0.05)
    if b_left < min_notional:
        if DEBUG:
            print('FUND EXHAUSTED (b_left < min_notional)')
        return True
        
    if dca_mode == "smart":
        budget = float(cfg.get("budget_usd", cfg.get("BUDGET_USD", 15.0)))
        layer_idx = len(data.get("buys", []))
        weights = get_smart_pyramid_weights(budget, min_notional)
        if layer_idx >= len(weights):
            if DEBUG:
                print('FUND EXHAUSTED (all smart pyramid layers filled)')
            return True
        next_amt = calculate_dca_layer_amount(layer_idx, budget, dca_mode=dca_mode, min_notional=min_notional)
        if (next_amt - b_left) > 0.05:
            if DEBUG:
                print('FUND EXHAUSTED (next smart layer amount exceeds budget_left)')
            return True
    else:
        buy_amt = float(cfg.get('buy_amount', 0.0))
        if (buy_amt - b_left) > 0.05:
            if DEBUG:
                print('FUND EXHAUSTED (flat buy_amt exceeds budget_left)')
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

_BTC_DUMP_CACHE = {"is_dumping": False, "drop_pct": 0.0, "time": 0}

def is_btc_dumping():
    """
    Mendeteksi apakah Bitcoin (BTCUSDT) sedang mengalami Flash Crash / Dump tajam (> 3.5% dalam 1 jam).
    Menggunakan caching 30 detik agar tidak membebani API Binance.
    """
    global _BTC_DUMP_CACHE
    now = time.time()
    if now - _BTC_DUMP_CACHE.get("time", 0) < 30:
        return _BTC_DUMP_CACHE.get("is_dumping", False)
        
    try:
        klines = safe_api_call(client.get_klines, symbol="BTCUSDT", interval="1h", limit=2)
        if klines and len(klines) >= 2:
            prev_candle = klines[-2]
            curr_candle = klines[-1]
            high_price = max(float(prev_candle[2]), float(curr_candle[2]))
            curr_price = float(curr_candle[4])
            if high_price > 0:
                drop_pct = ((high_price - curr_price) / high_price) * 100
                is_dumping = drop_pct >= 3.5
                _BTC_DUMP_CACHE = {"is_dumping": is_dumping, "drop_pct": round(drop_pct, 2), "time": now}
                return is_dumping
        _BTC_DUMP_CACHE["time"] = now
    except Exception as e:
        if DEBUG: print(f"[DBG BTC Dump] Error checking BTC status: {e}")
        _BTC_DUMP_CACHE["time"] = now
        
    return _BTC_DUMP_CACHE.get("is_dumping", False)

def get_btc_guard_status():
    """
    Mengambil status live BTC Crash Guard beserta persentase drop BTC 1 jam terakhir.
    """
    is_dumping = is_btc_dumping()
    drop_pct = _BTC_DUMP_CACHE.get("drop_pct", 0.0)
    is_enabled = bool(get_global_settings().get("btc_guard", True))
    return {
        "enabled": is_enabled,
        "is_dumping": is_dumping,
        "drop_pct": drop_pct,
        "threshold_pct": 3.5
    }

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
    
def get_unallocated_usdt(for_pair=None):
    """
    Menghitung sisa kas USDT riil di Binance yang benar-benar bebas,
    dengan memotong sisa dana yang sudah di-reserve (budget_left) oleh koin lain
    yang sedang memiliki posisi beli aktif (buys > 0).
    """
    total_available = get_balance_from_cache("USDT")
    reserved_by_others = 0.0
    for other_p, d in bot_data.items():
        if other_p != for_pair and len(d.get('buys', [])) > 0:
            reserved_by_others += max(0.0, float(d.get('budget_left', 0.0)))
            
    unallocated = max(0.0, total_available - reserved_by_others)
    return unallocated

def get_adaptive_buy_amount(pair):
    data = bot_data[pair]
    cfg = data["config"]
    dca_mode = cfg.get("dca_mode", cfg.get("DCA_MODE", "smart"))
    budget = float(cfg.get("budget_usd", cfg.get("BUDGET_USD", 15.0)))
    configured_buy_amt = float(cfg.get("buy_amount", cfg.get("BUY_AMOUNT", 2.1)))
    layer_idx = len(data.get("buys", []))
    available = get_balance_from_cache("USDT")
    unallocated = get_unallocated_usdt(pair)
    min_notional = get_notion(pair)
    safe_min = max(1.0, min_notional)

    calculated_amt = calculate_dca_layer_amount(layer_idx, budget, configured_buy_amt, dca_mode, min_notional=min_notional)
    
    # Sisa kas yang tersedia untuk layer ini
    effective_avail = unallocated if layer_idx == 0 else available
    if calculated_amt > effective_avail:
        calculated_amt = max(safe_min, effective_avail)
        
    return max(safe_min, calculated_amt)
    
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
    
    # Jika price_history belum cukup (koin baru masuk via autopilot swap < 50 menit),
    # fallback ke peak_price global agar bot tidak stuck menunggu 50 menit
    cfg_drop = data["config"].get("drop_threshold", 0.013)
    rsi = get_rsi(pair)
    max_rsi = data["config"].get("rsi_max_entry", 48)
    
    if len(history) < 10:
        global_peak_fb = data.get('peak_price', current_price)
        if global_peak_fb and global_peak_fb > 0:
            drop_fb = (global_peak_fb - current_price) / global_peak_fb
            if drop_fb >= cfg_drop and rsi <= max_rsi:
                return True
        return False
        
    raw_prices = [p.get('price', 0) if isinstance(p, dict) else float(p) for p in history]
    valid_prices = [p for p in raw_prices if p > 0]
    
    if not valid_prices:
        return False
        
    recent_peak = max(valid_prices)
    drop_from_recent_peak = (recent_peak - current_price) / recent_peak
    
    hours_running = (time.time() - data.get('peak_time', time.time())) / 3600
    threshold = cfg_drop
    
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
            threshold = data["config"].get("drop_threshold", 0.013)
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
def check_buy_preconditions(pair, data, current_price, force=False):
    now_ts = time.time()
    if not force and now_ts - data.get('last_buy_time', 0) < MIN_BUY_INTERVAL:
        if DEBUG: print("[DBG] BUY ditolak - kena cooldown internal")
        return False
    if not force and is_dead_market(pair):
        if DEBUG: print("Market dead, skip buy")
        return False
    if current_price == 0:
        if DEBUG: print("[DBG BUY] price == 0, skip")
        return False
        
    min_buy, max_buy = get_dynamic_buy_limits(pair)
    if not force and min_buy and current_price < min_buy:
        if DEBUG: print(f"[DBG BUY] below dynamic min_buy {fmt(min_buy)}, skip")
        return False
    if not force and max_buy and current_price > max_buy:
        if DEBUG: print(f"[DBG BUY] above dynamic max_buy {fmt(max_buy)}, skip")
        return False
        
    available = get_balance_from_cache("USDT")
    unallocated = get_unallocated_usdt(pair)
    is_first_buy = len(data.get('buys', [])) == 0
    min_notional = get_notion(pair)
    
    if available < 0.000001 or data['budget_left'] < 0.000001:
        if DEBUG: print("[DBG BUY] no funds available, skip. available=", available, "budget_left=", data['budget_left'])
        return False
        
    # Jika first buy tapi seluruh kas sudah di-reserve oleh koin lain yang sedang aktif:
    if not force and is_first_buy and unallocated < min_notional:
        if DEBUG: print(f"[DBG BUY] first buy tertahan: unallocated cash ({unallocated:.2f}) < min_notional ({min_notional:.2f}). Kas terkunci untuk koin lain.")
        return False
        
    # Circuit Breaker: Tahan SEMUA pembelian (First Buy maupun Layer Lanjutan) jika BTC Crash Guard aktif dan BTC sedang dump
    if not force and get_global_settings().get("btc_guard", True) and is_btc_dumping():
        layer_label = "First Buy" if is_first_buy else f"Layer {len(data.get('buys', [])) + 1}"
        if DEBUG: print(f"[DBG BUY] BTC Crash Guard AKTIF: BTC sedang dump >3.5%. Menahan {layer_label} {pair} sampai badai reda.")
        return False
        
    return True

def calculate_usable_buy_quote(pair, data, current_price):
    available = get_balance_from_cache("USDT")
    unallocated = get_unallocated_usdt(pair)
    is_first_buy = len(data.get('buys', [])) == 0
    min_notional = get_notion(pair)
    desired_buy_amount = get_adaptive_buy_amount(pair)
    
    SAFE_BUFFER = max(0.05, min_notional * 0.05)
    min_required = min_notional + SAFE_BUFFER
    
    if desired_buy_amount < min_required:
        if DEBUG: print(f"[DBG BUY] desired {desired_buy_amount:.6f} < min_required {min_required:.6f}, abort!")
        return 0
        
    effective_cash = unallocated if is_first_buy else available
    usable_quote = min(desired_buy_amount, effective_cash, data['budget_left'])
    
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
            except Exception:
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
    data['lowest_price'] = avg_fill_price
    data['lowest_price_time'] = int(time.time())
    data['idle_since'] = 0
    
    save_data(pair, data)

    log_action(pair, "BUY", avg_fill_price, total_qty,
               message=f"cost={total_cost:.8f} USDT | fee={total_fee_usdt:.8f} | net={net_cost:.8f} | usable_quote={usable_quote:.6f}")

    get_account_cached(force=True)

def buy(pair, force=False):
    data = bot_data[pair]
    if is_buying[pair]:
        if DEBUG: print("[DBG] BUY LOCK aktif, skip")
        return False
        
    current_price = get_ticker_price(pair)
    if not check_buy_preconditions(pair, data, current_price, force=force):
        return False
        
    is_buying[pair] = True
    try:
        usable_quote = calculate_usable_buy_quote(pair, data, current_price)
        if usable_quote <= 0:
            return False
            
        fills = execute_buy_order(pair, usable_quote)
        if not fills:
            if DEBUG: print("[DBG BUY] order filled no fills, skip")
            log_action(pair, "BUY_FAILED", 0, 0, message="no fills returned")
            return False
            
        process_buy_fills_and_update_state(pair, data, fills, usable_quote, current_price)
        time.sleep(1)
        return True

    except Exception as e:
        if DEBUG: print("[DBG BUY] order_market_buy failed:", e)
        log_action(pair, "BUY_ERROR", 0, 0, message=str(e))
        return False
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
    full_budget = float(data["config"].get("budget_usd", 15.0))
    data["buys"] = []
    data["budget_left"] = round(full_budget, 2)
    data["peak_price"] = current_price
    data["lowest_price"] = current_price
    data["peak_time"] = int(time.time())
    data["last_buy_time"] = 0
    data["idle_since"] = int(time.time())
    save_data(pair, data)
    bot_data[pair] = load_data(pair)
    try:
        get_account_cached(force=True)
    except Exception as e_acc:
        if DEBUG: print(f"[SYNC] Error refresh account in ghost reset: {e_acc}")

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
                except Exception:
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

    # 1. IMMEDIATE STATE RESET (Detik ke-0): Bersihkan memori dan simpan ke disk seketika
    full_budget = float(data["config"].get("budget_usd", 15.0))
    data["buys"] = []
    data["budget_left"] = round(full_budget, 2)
    data["peak_price"] = avg_sell_price
    data["lowest_price"] = avg_sell_price
    data["peak_time"] = int(time.time())
    data["last_buy_time"] = 0
    data["idle_since"] = int(time.time())
    save_data(pair, data)

    # 2. Catat log SELL resmi ke Trade Logs
    log_action(pair, "SELL", avg_sell_price, qty, profit_real)

    # 3. Pending Config / Revert Config Application
    try:
        if data.get("pending_config"):
            p_cfg = data.pop("pending_config", None)
            if p_cfg:
                data["config"]["budget_usd"] = float(p_cfg["budget_usd"])
                data["config"]["buy_amount"] = float(p_cfg["buy_amount"])
                data["config"]["take_profit_margin"] = float(p_cfg["take_profit_margin"])
                data["config"]["drop_threshold"] = float(p_cfg["drop_threshold"])
                data["config"]["rsi_max_entry"] = float(p_cfg.get("rsi_max_entry", 48))
                data["config"]["dca_mode"] = str(p_cfg.get("dca_mode", "flat")).lower()
                data["budget_left"] = round(float(p_cfg["budget_usd"]), 2)
                if pair in PAIRS_CONFIG:
                    PAIRS_CONFIG[pair].update({
                        "BUDGET_USD": float(p_cfg["budget_usd"]),
                        "BUY_AMOUNT": float(p_cfg["buy_amount"]),
                        "TAKE_PROFIT_MARGIN": float(p_cfg["take_profit_margin"]),
                        "DROP_THRESHOLD": float(p_cfg["drop_threshold"]),
                        "RSI_MAX_ENTRY": float(p_cfg.get("rsi_max_entry", 48)),
                        "DCA_MODE": str(p_cfg.get("dca_mode", "flat")).lower()
                    })
                save_active_pairs()
                save_data(pair, data)
                log_action(pair, "CONFIG_APPLIED", 0, 0, message=f"Pending config otomatis aktif setelah Closed Buy! Budget: ${p_cfg['budget_usd']}, Mode: {p_cfg.get('dca_mode', 'flat')}")
        elif data.get("revert_config_after_sell"):
            rev_cfg = data.pop("revert_config_after_sell", None)
            if rev_cfg:
                data["config"].update(rev_cfg)
                new_b = float(rev_cfg.get("budget_usd", data["config"].get("budget_usd", 15.0)))
                data["budget_left"] = round(new_b, 4)
                if pair in PAIRS_CONFIG:
                    PAIRS_CONFIG[pair].update({
                        "BUDGET_USD": new_b,
                        "BUY_AMOUNT": float(rev_cfg.get("buy_amount", PAIRS_CONFIG[pair].get("BUY_AMOUNT", 2.1))),
                        "TAKE_PROFIT_MARGIN": float(rev_cfg.get("take_profit_margin", PAIRS_CONFIG[pair].get("TAKE_PROFIT_MARGIN", 0.008))),
                        "DROP_THRESHOLD": float(rev_cfg.get("drop_threshold", PAIRS_CONFIG[pair].get("DROP_THRESHOLD", 0.013))),
                        "RSI_MAX_ENTRY": float(rev_cfg.get("rsi_max_entry", PAIRS_CONFIG[pair].get("RSI_MAX_ENTRY", 48))),
                        "DCA_MODE": str(rev_cfg.get("dca_mode", PAIRS_CONFIG[pair].get("DCA_MODE", "flat"))).lower()
                    })
                save_active_pairs()
                save_data(pair, data)
                log_action(pair, "AUTO_REVERT", 0, 0, message=f"Config koin otomatis kembali ke parameter normal setelah Take Profit! (Budget: ${new_b} USDT)")
    except Exception as e_rev:
        if DEBUG: print(f"[SELL] Error apply revert/pending config: {e_rev}")

    # 4. Auto-Compound Reinvestment
    try:
        g_settings = get_global_settings()
        if g_settings.get("auto_compound", False) and profit_real > 0:
            added_profit = round(profit_real * 0.5, 2)
            cur_b = float(data["config"].get("budget_usd", 15.0))
            new_budget_cfg = round(cur_b + added_profit, 2)
            cur_buy = float(data["config"].get("buy_amount", 2.1))
            new_buy_amount = round(cur_buy * (new_budget_cfg / cur_b), 2)
            data["config"]["budget_usd"] = new_budget_cfg
            data["config"]["buy_amount"] = new_buy_amount
            d_mode = str(data["config"].get("dca_mode", "smart")).lower()
            comp_layers = len(get_smart_pyramid_weights(new_budget_cfg, 1.0)) if d_mode == "smart" else 7
            data["config"]["max_layer"] = comp_layers
            data["budget_left"] = round(new_budget_cfg, 2)
            if pair in PAIRS_CONFIG:
                PAIRS_CONFIG[pair]["BUDGET_USD"] = new_budget_cfg
                PAIRS_CONFIG[pair]["BUY_AMOUNT"] = new_buy_amount
                PAIRS_CONFIG[pair]["MAX_LAYER"] = comp_layers
            save_active_pairs()
            save_data(pair, data)
            log_action(pair, "COMPOUND", 0, 0, f"Auto-compounded +{added_profit} USDT into budget! New Budget: ${new_budget_cfg} USDT, Mode: {d_mode.upper()} ({comp_layers} Layers)")
    except Exception as e_comp:
        if DEBUG: print(f"[SELL] Error auto-compound: {e_comp}")

    # 5. Auto-Pilot Rotator Candidate Selection
    try:
        g_settings = get_global_settings()
        is_autopilot_on = g_settings.get("auto_pilot", False)
        is_locked = pair in g_settings.get("locked_pairs", [])
        if not data.get("pending_replacement") and is_autopilot_on and not is_locked:
            cfg = data.get("config", {})
            b_usd = cfg.get("budget_usd", 15.0)
            b_amt = cfg.get("buy_amount", 2.1)
            d_mode = cfg.get("dca_mode", "smart")
            d_drop = cfg.get("drop_threshold", 0.013)
            d_tp = cfg.get("take_profit_margin", 0.008)

            best_sym, best_cand = select_autopilot_candidate(
                current_pair=pair,
                budget_usd=b_usd,
                buy_amount=b_amt,
                dca_mode=d_mode,
                drop_threshold=d_drop,
                take_profit_margin=d_tp
            )
            if best_sym and best_sym not in PAIRS:
                data["pending_replacement"] = {
                    "pair": best_sym,
                    "budget_usd": b_usd,
                    "buy_amount": b_amt,
                    "dca_mode": d_mode,
                    "drop_threshold": d_drop,
                    "take_profit_margin": d_tp,
                    "max_loss_percent": cfg.get("max_loss_percent", -15.0),
                    "rsi_max_entry": cfg.get("rsi_max_entry", 48.0),
                    "trailing_margin": cfg.get("trailing_margin", 0.001)
                }
                log_action(pair, "AUTOPILOT_TRIGGER", 0, 0, f"Auto-Pilot memilih koin juara {best_sym} berdasarkan evaluasi simulasi DCA 24 Jam!")
    except Exception as e_scan:
        if DEBUG: print(f"[DBG AutoPilot] Error scanning next coin: {e_scan}")

    # 6. Pending Swap Execution
    try:
        if data.get("pending_replacement"):
            rep_info = data.pop("pending_replacement", None)
            new_pair = rep_info.get("pair") if rep_info else None
            if new_pair and new_pair not in PAIRS:
                execute_autopilot_swap(
                    pair, new_pair,
                    budget_usd=rep_info.get("budget_usd"),
                    buy_amount=rep_info.get("buy_amount"),
                    reason="TAKE_PROFIT",
                    dca_mode=rep_info.get("dca_mode"),
                    drop_threshold=rep_info.get("drop_threshold"),
                    take_profit_margin=rep_info.get("take_profit_margin"),
                    max_loss_percent=rep_info.get("max_loss_percent"),
                    rsi_max_entry=rep_info.get("rsi_max_entry"),
                    trailing_margin=rep_info.get("trailing_margin")
                )
    except Exception as e_swap:
        if DEBUG: print(f"Error auto-swap: {str(e_swap)}")

    save_data(pair, data)
    try:
        get_account_cached(force=True)
    except Exception:
        pass

def execute_autopilot_swap(old_pair, new_pair, budget_usd=None, buy_amount=None, reason="TAKE_PROFIT", dca_mode=None, idle_hours=0.0, **kwargs):
    """
    Eksekusi rotasi pair Auto-Pilot (Take Profit atau Idle Rotator).
    Mewarisi 100% parameter konfigurasi trading dari koin lama (old_pair).
    """
    try:
        if not new_pair or new_pair in PAIRS:
            return False
            
        old_cfg = {}
        if old_pair and old_pair in bot_data:
            old_cfg = bot_data[old_pair].get("config", {}).copy()
        elif old_pair and old_pair in PAIRS_CONFIG:
            old_cfg = PAIRS_CONFIG[old_pair].copy()

        final_budget = float(budget_usd if budget_usd is not None else old_cfg.get("budget_usd", old_cfg.get("BUDGET_USD", 15.0)))
        final_buy = float(buy_amount if buy_amount is not None else old_cfg.get("buy_amount", old_cfg.get("BUY_AMOUNT", 2.1)))
        final_mode = str(dca_mode if dca_mode is not None else old_cfg.get("dca_mode", old_cfg.get("DCA_MODE", "smart"))).lower()
        final_drop = float(kwargs.get("drop_threshold", old_cfg.get("drop_threshold", old_cfg.get("DROP_THRESHOLD", 0.013))))
        final_tp = float(kwargs.get("take_profit_margin", old_cfg.get("take_profit_margin", old_cfg.get("TAKE_PROFIT_MARGIN", 0.008))))
        final_loss = float(kwargs.get("max_loss_percent", old_cfg.get("max_loss_percent", old_cfg.get("MAX_LOSS_PERCENT", -15.0))))
        final_fee = float(kwargs.get("fee_rate", old_cfg.get("fee_rate", old_cfg.get("FEE_RATE", 0.001))))
        final_trail = float(kwargs.get("trailing_margin", old_cfg.get("trailing_margin", old_cfg.get("TRAILING_MARGIN", 0.001))))
        final_rsi = float(kwargs.get("rsi_max_entry", old_cfg.get("rsi_max_entry", old_cfg.get("RSI_MAX_ENTRY", 48.0))))

        PAIRS_CONFIG[new_pair] = {
            "BUDGET_USD": final_budget,
            "BUY_AMOUNT": final_buy,
            "DROP_THRESHOLD": final_drop,
            "MAX_LOSS_PERCENT": final_loss,
            "FEE_RATE": final_fee,
            "TAKE_PROFIT_MARGIN": final_tp,
            "TRAILING_MARGIN": final_trail,
            "STATUS": 1,
            "DCA_MODE": final_mode,
            "RSI_MAX_ENTRY": final_rsi
        }
        if old_pair in PAIRS:
            PAIRS.remove(old_pair)
        PAIRS.append(new_pair)
        save_active_pairs()
        bot_locks[new_pair] = threading.RLock()
        init_fresh_pair_data(
            new_pair,
            budget_usd=final_budget,
            buy_amount=final_buy,
            dca_mode=final_mode,
            drop_threshold=final_drop,
            take_profit_margin=final_tp,
            max_loss_percent=final_loss,
            fee_rate=final_fee,
            trailing_margin=final_trail,
            rsi_max_entry=final_rsi
        )
        t = threading.Thread(target=dca_loop, args=(new_pair,), daemon=True)
        t.start()
        
        if reason == "IDLE_ROTATION":
            log_action(old_pair, "SWAP_IDLE", 0, 0, f"Auto-swapped with {new_pair} setelah standby {idle_hours:.1f} jam tanpa sinyal Open Buy.")
            log_action(new_pair, "AUTOPILOT_START", 0, 0, f"Memulai bot untuk {new_pair} (Auto-Pilot Idle Rotator menggantikan {old_pair}, mewarisi Config {final_mode.upper()} Drop:{final_drop*100}% TP:{final_tp*100}%)")
        else:
            log_action(old_pair, "SWAP", 0, 0, f"Auto-swapped with {new_pair} after successful Take Profit!")
            log_action(new_pair, "AUTOPILOT_START", 0, 0, f"Memulai bot untuk {new_pair} (Auto-Pilot TP Rotator menggantikan {old_pair}, mewarisi Config {final_mode.upper()} Drop:{final_drop*100}% TP:{final_tp*100}%)")
        return True
    except Exception as e_swap:
        if DEBUG: print(f"Error execute_autopilot_swap: {e_swap}")
        return False

def check_and_execute_idle_rotation(pair):
    """
    Auto-Pilot Idle Rotator:
    Jika koin nganggur/standby tanpa Open Buy (0 layers) selama >= auto_pilot_idle_hours (default: 12 jam),
    rotasi otomatis ke koin juara lain dengan momentum lebih aktif tanpa resiko cut loss.
    """
    if pair not in bot_data:
        return False
    data = bot_data[pair]
    if len(data.get('buys', [])) > 0:
        return False
    
    g_settings = get_global_settings()
    if not g_settings.get("auto_pilot", False):
        return False
    if not g_settings.get("auto_pilot_idle_rotation", True):
        return False
    if pair in g_settings.get("locked_pairs", []):
        return False
    if data.get("pending_replacement"):
        return False
    if data.get("config", {}).get("status", 1) == 0:
        return False
        
    now = time.time()
    idle_since = float(data.get('idle_since', 0.0))
    if idle_since <= 0:
        idle_since = float(data.get('peak_time', now))
        data['idle_since'] = int(idle_since)
        save_data(pair, data)
        
    idle_hours_threshold = float(g_settings.get("auto_pilot_idle_hours", 12.0))
    elapsed_hours = (now - idle_since) / 3600.0
    if elapsed_hours < idle_hours_threshold:
        return False
        
    # Batasi scan candidate agar tidak spam API (min 300s antar scan attempt)
    if now - data.get('_last_idle_scan_attempt', 0) < 300:
        return False
    data['_last_idle_scan_attempt'] = now
    
    cfg = data.get("config", {})
    b_usd = cfg.get("budget_usd", 15.0)
    b_amt = cfg.get("buy_amount", 2.1)
    d_mode = cfg.get("dca_mode", "smart")
    d_drop = cfg.get("drop_threshold", 0.013)
    d_tp = cfg.get("take_profit_margin", 0.008)
    
    if DEBUG:
        print(f"[IDLE ROTATOR] Koin {pair} menganggur selama {elapsed_hours:.1f} jam (Batas: {idle_hours_threshold} jam). Memulai evaluasi scanner koin juara...")
        
    best_sym, best_cand = select_autopilot_candidate(
        current_pair=pair,
        budget_usd=b_usd,
        buy_amount=b_amt,
        dca_mode=d_mode,
        drop_threshold=d_drop,
        take_profit_margin=d_tp
    )
    
    if not best_sym or best_sym in PAIRS:
        if DEBUG:
            print(f"[IDLE ROTATOR] Tidak ada kandidat koin baru yang cocok untuk merotasi {pair}.")
        return False
        
    # Catat cooldown anti-pingpong untuk pair lama
    record_idle_cooldown(pair)
    
    # Eksekusi swap mewarisi config pair lama sepenuhnya!
    success = execute_autopilot_swap(
        pair, best_sym,
        budget_usd=b_usd,
        buy_amount=b_amt,
        reason="IDLE_ROTATION",
        dca_mode=d_mode,
        idle_hours=elapsed_hours,
        drop_threshold=d_drop,
        take_profit_margin=d_tp,
        max_loss_percent=cfg.get("max_loss_percent", -15.0),
        rsi_max_entry=cfg.get("rsi_max_entry", 48.0),
        trailing_margin=cfg.get("trailing_margin", 0.001)
    )
    return success

def sell_all(pair, CUT_LOSS=False):
    if pair not in bot_locks:
        bot_locks[pair] = threading.RLock()
    with bot_locks[pair]:
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
            print("ERROR SELL gagal: {}".format(str(e)))

def preview_manual_buy(pair):
    if pair not in bot_data:
        return {"success": False, "message": f"Pair {pair} tidak ditemukan."}
    data = bot_data[pair]
    buys = data.get('buys', [])
    current_price = get_ticker_price(pair)
    if current_price <= 0:
        return {"success": False, "message": f"Gagal mendapatkan harga pasar realtime untuk {pair}."}
        
    next_layer_num = len(buys) + 1
    free_usdt = get_balance_from_cache("USDT")
    unallocated = get_unallocated_usdt(pair)
    budget_left = float(data.get('budget_left', 0.0))
    is_first_buy = len(buys) == 0
    effective_cash = unallocated if is_first_buy else free_usdt
    min_notional = get_notion(pair)
    step = get_step_size(pair)
    fee_rate = float(data.get("config", {}).get("fee_rate", 0.001))
    tp_margin = float(data.get("config", {}).get("take_profit_margin", 0.008))
    
    desired_amt = get_adaptive_buy_amount(pair)
    usable_quote = min(desired_amt, effective_cash, budget_left)
    
    can_buy = True
    reason = ""
    if is_fund_exhausted(pair):
        can_buy = False
        reason = f"Budget koin (${budget_left:.2f}) atau saldo kas USDT (${free_usdt:.2f}) sudah habis."
    elif usable_quote < min_notional:
        can_buy = False
        reason = f"Estimasi serokan (${usable_quote:.2f}) di bawah batas minimum Binance (${min_notional:.2f} USDT)."
        
    planned_buy_amount = usable_quote if can_buy else desired_amt
    est_qty = floor_to_step(planned_buy_amount / current_price, step) if current_price > 0 else 0.0
    
    # Kalkulasi AVG Buy lama & baru
    old_cost = sum(float(b['price']) * float(b['qty']) for b in buys)
    old_qty = sum(float(b['qty']) for b in buys)
    old_avg = (old_cost / old_qty) if old_qty > 0 else 0.0
    
    new_cost = old_cost + planned_buy_amount
    new_qty = old_qty + est_qty
    new_avg = (new_cost / new_qty) if new_qty > 0 else current_price
    
    # Target Full Take Profit (Seluruh koin setelah serok baru)
    new_tp_price = calculate_dca_tp_target(new_avg, next_layer_num, tp_margin)
    full_tp_margin_pct = ((new_tp_price - new_avg) / new_avg * 100.0) if new_avg > 0 else (tp_margin * 100.0)
    full_tp_gross = new_tp_price * new_qty
    full_tp_est_fee = (full_tp_gross + new_cost) * fee_rate
    full_tp_net_profit = full_tp_gross - new_cost - full_tp_est_fee
    
    # Target Partial Take Profit (Layer ini saja jika dijual tersendiri via Auto-Rescue / Sell Last Layer)
    gs = get_global_settings()
    rescue_tp_cfg = float(gs.get("auto_rescue_tp", 0))
    layer_tp_margin = rescue_tp_cfg if rescue_tp_cfg > 0 else tp_margin
    partial_tp_price = current_price * (1.0 + layer_tp_margin)
    partial_tp_margin_pct = layer_tp_margin * 100.0
    partial_tp_gross = partial_tp_price * est_qty
    partial_tp_est_fee = (partial_tp_gross + planned_buy_amount) * fee_rate
    partial_tp_net_profit = partial_tp_gross - planned_buy_amount - partial_tp_est_fee
    
    return {
        "success": True,
        "pair": pair,
        "current_price": current_price,
        "next_layer_num": next_layer_num,
        "buy_amount": round(planned_buy_amount, 2),
        "est_qty": est_qty,
        "free_usdt": round(free_usdt, 2),
        "budget_left": round(budget_left, 2),
        "can_buy": can_buy,
        "reason": reason,
        "old_avg": old_avg,
        "new_avg": new_avg,
        "full_tp_price": new_tp_price,
        "full_tp_margin_pct": round(full_tp_margin_pct, 2),
        "full_tp_net_profit": round(full_tp_net_profit, 4),
        "partial_tp_price": partial_tp_price,
        "partial_tp_margin_pct": round(partial_tp_margin_pct, 2),
        "partial_tp_net_profit": round(partial_tp_net_profit, 4)
    }

def preview_manual_sell(pair):
    if pair not in bot_data:
        return {"success": False, "message": f"Pair {pair} tidak ditemukan."}
    data = bot_data[pair]
    buys = data.get('buys', [])
    if not buys:
        return {"success": False, "message": f"Koin {pair} tidak memiliki posisi aktif untuk dijual."}
        
    current_price = get_ticker_price(pair)
    if current_price <= 0:
        return {"success": False, "message": f"Gagal mendapatkan harga pasar realtime untuk {pair}."}
        
    step = get_step_size(pair)
    min_notional = get_notion(pair)
    fee_rate = float(data.get("config", {}).get("fee_rate", 0.001))
    
    used_cost = sum(float(b['price']) * float(b['qty']) for b in buys)
    recorded_qty = sum(float(b['qty']) for b in buys)
    avg_buy = (used_cost / recorded_qty) if recorded_qty > 0 else 0.0
    
    coin = pair.replace("USDT", "")
    get_account_cached(force=True)
    live_qty = get_balance_from_cache(coin)
    qty_to_use = live_qty if live_qty > 0 else recorded_qty
    qty_to_sell = floor_to_step(qty_to_use, step)
    
    estimated_val = qty_to_sell * current_price
    est_fee = (estimated_val + used_cost) * fee_rate
    net_profit = estimated_val - used_cost - est_fee
    gain_pct = ((estimated_val - used_cost) / used_cost * 100.0) if used_cost > 0 else 0.0
    is_profit = net_profit >= 0
    
    return {
        "success": True,
        "pair": pair,
        "layers_count": len(buys),
        "current_price": current_price,
        "avg_buy": avg_buy,
        "qty": qty_to_sell,
        "used_cost": round(used_cost, 2),
        "estimated_val": round(estimated_val, 2),
        "net_profit": round(net_profit, 4),
        "gain_pct": round(gain_pct, 2),
        "is_profit": is_profit,
        "min_notional": min_notional,
        "is_notional_valid": estimated_val >= min_notional
    }

def preview_sell_last_layer(pair):
    if pair not in bot_data:
        return {"success": False, "message": f"Pair {pair} tidak ditemukan."}
    data = bot_data[pair]
    buys = data.get('buys', [])
    if not buys:
        return {"success": False, "message": f"Koin {pair} tidak memiliki posisi aktif."}
        
    last_layer = buys[-1]
    buy_price = float(last_layer.get('price', 0.0))
    layer_qty = float(last_layer.get('qty', 0.0))
    if buy_price <= 0 or layer_qty <= 0:
        return {"success": False, "message": "Data layer terakhir tidak valid."}
        
    current_price = get_ticker_price(pair)
    if current_price <= 0:
        return {"success": False, "message": f"Gagal mendapatkan harga pasar realtime untuk {pair}."}
        
    step = get_step_size(pair)
    min_notional = get_notion(pair)
    qty_to_sell = floor_to_step(layer_qty, step)
    notional = qty_to_sell * current_price
    
    layer_time = float(last_layer.get("time", 0))
    if not layer_time:
        layer_time = float(data.get("last_buy_time", 0))
    if not layer_time:
        layer_time = float(data.get("lowest_price_time", 0))
    layer_age_sec = time.time() - layer_time if layer_time > 0 else 0
    layer_age_days = round(layer_age_sec / 86400, 1)
    
    gs = get_global_settings()
    rescue_days = float(gs.get("auto_rescue_days", 4))
    meets_days = layer_age_sec >= (rescue_days * 86400)
    
    fee_rate = float(data.get("config", {}).get("fee_rate", 0.001))
    cost = buy_price * qty_to_sell
    gross = qty_to_sell * current_price
    est_fee = (gross + cost) * fee_rate
    net_profit = gross - cost - est_fee
    gain_pct = (current_price - buy_price) / buy_price if buy_price > 0 else 0.0
    cost_restored = float(last_layer.get("quote_spent") or cost)
    
    return {
        "success": True,
        "pair": pair,
        "layer_num": len(buys),
        "total_layers": len(buys),
        "buy_price": buy_price,
        "current_price": current_price,
        "qty": qty_to_sell,
        "notional": round(notional, 2),
        "min_notional": min_notional,
        "is_notional_valid": notional >= min_notional,
        "layer_age_days": layer_age_days,
        "threshold_days": rescue_days,
        "meets_days": meets_days,
        "gain_pct": round(gain_pct * 100, 2),
        "net_profit": round(net_profit, 4),
        "is_profit": net_profit >= 0,
        "cost_restored": round(cost_restored, 2),
        "is_auto_revert": bool(data.get("revert_config_after_sell"))
    }

def execute_sell_last_layer(pair, is_manual=True):
    if pair not in bot_data:
        return {"success": False, "message": f"Pair {pair} tidak ditemukan."}
    data = bot_data[pair]
    buys = data.get('buys', [])
    if not buys:
        return {"success": False, "message": f"Tidak ada layer aktif untuk {pair}."}
        
    current_price = get_ticker_price(pair)
    if current_price <= 0:
        return {"success": False, "message": f"Gagal mendapatkan harga pasar untuk {pair}."}
        
    last_layer = buys[-1]
    buy_price = float(last_layer.get('price', 0.0))
    layer_qty = float(last_layer.get('qty', 0.0))
    if buy_price <= 0 or layer_qty <= 0:
        return {"success": False, "message": "Data harga atau jumlah koin pada layer terakhir tidak valid."}
        
    step = get_step_size(pair)
    min_qty = get_min_qty(pair)
    min_notional = get_notion(pair)
    qty_to_sell = floor_to_step(layer_qty, step)
    notional = qty_to_sell * current_price
    
    if qty_to_sell < min_qty or notional < min_notional:
        return {"success": False, "message": f"Nilai serokan layer ini (${notional:.2f}) kurang dari minimum transaksi Binance (${min_notional:.2f})."}
        
    lock = bot_locks.get(pair)
    if not lock:
        bot_locks[pair] = threading.RLock()
        lock = bot_locks[pair]
        
    with lock:
        fills = execute_sell_order(pair, qty_to_sell)
        if not fills:
            return {"success": False, "message": f"Order market sell Binance gagal dieksekusi untuk {pair}."}
            
        total_sell = sum(float(f['price']) * float(f['qty']) for f in fills)
        actual_qty = sum(float(f['qty']) for f in fills)
        avg_sell_price = total_sell / actual_qty if actual_qty else current_price
        
        cost = buy_price * actual_qty
        fee_rate = float(data.get("config", {}).get("fee_rate", 0.001))
        net_profit = total_sell - cost - ((total_sell + cost) * fee_rate)
        gain_pct = (avg_sell_price - buy_price) / buy_price if buy_price > 0 else 0.0
        
        recycled_num = len(buys)
        data['buys'].pop()
        
        cost_restored = float(last_layer.get("quote_spent") or cost)
        full_budget = float(data.get("config", {}).get("budget_usd", 15.0))
        data['budget_left'] = min(full_budget, round(float(data.get('budget_left', 0.0)) + cost_restored, 4))
        
        if len(data['buys']) == 0:
            data['peak_price'] = avg_sell_price if avg_sell_price > 0 else current_price
            data['lowest_price'] = avg_sell_price if avg_sell_price > 0 else current_price
            data['peak_time'] = int(time.time())
            data['lowest_price_time'] = int(time.time())
            data['last_buy_time'] = 0
            data['idle_since'] = int(time.time())
        else:
            data['lowest_price'] = current_price
            data['lowest_price_time'] = int(time.time())
            
        # Auto-Compound Handling for Partial Sell / Sell Last Layer
        gs = get_global_settings()
        is_compound_on = gs.get("auto_compound", False)
        compounded_profit = 0.0
        if is_compound_on and net_profit > 0:
            compounded_profit = round(net_profit, 4)
            if data.get("revert_config_after_sell"):
                rev_cfg = data["revert_config_after_sell"]
                rev_base_b = float(rev_cfg.get("budget_usd", data["config"].get("budget_usd", 15.0)))
                new_rev_b = round(rev_base_b + compounded_profit, 4)
                rev_cfg["budget_usd"] = new_rev_b
                d_mode = str(rev_cfg.get("dca_mode", "smart")).lower()
                max_l = float(len(get_smart_pyramid_weights(new_rev_b, 1.0))) if d_mode == "smart" else float(rev_cfg.get("max_layer", 7.0))
                rev_cfg["max_layer"] = max_l
                rev_cfg["buy_amount"] = max(1.10, round(new_rev_b / max_l, 2))
                target_budget_display = new_rev_b
            else:
                curr_b = float(data.get("config", {}).get("budget_usd", 15.0))
                new_b = round(curr_b + compounded_profit, 4)
                d_mode = str(data.get("config", {}).get("dca_mode", "smart")).lower()
                max_l = float(len(get_smart_pyramid_weights(new_b, 1.0))) if d_mode == "smart" else float(data.get("config", {}).get("max_layer", 7.0))
                new_buy_amount = max(1.10, round(new_b / max_l, 2))
                data["config"]["budget_usd"] = new_b
                data["config"]["buy_amount"] = new_buy_amount
                data["config"]["max_layer"] = max_l
                used_cost = sum(float(b.get("quote_spent") or (float(b.get("qty", 0)) * float(b.get("price", 0)))) for b in data.get("buys", []))
                data["budget_left"] = max(0.0, round(new_b - used_cost, 4))
                if pair in PAIRS_CONFIG:
                    PAIRS_CONFIG[pair]["BUDGET_USD"] = new_b
                    PAIRS_CONFIG[pair]["BUY_AMOUNT"] = new_buy_amount
                save_active_pairs()
                target_budget_display = new_b
                
            log_action(pair, "COMPOUND", avg_sell_price, actual_qty, profit=compounded_profit,
                       message=f"Auto-compounded partial TP +${compounded_profit:.4f} USDT into {pair} budget! New Budget: ${target_budget_display:.2f}")

        # Auto-Revert Handling
        reverted_budget_info = None
        if data.get("revert_config_after_sell"):
            rev_cfg = data.pop("revert_config_after_sell", None)
            if rev_cfg:
                data["config"].update(rev_cfg)
                new_b = float(rev_cfg.get("budget_usd", data["config"].get("budget_usd", 15.0)))
                used_cost = sum(float(b.get("quote_spent") or (float(b.get("qty", 0)) * float(b.get("price", 0)))) for b in data.get("buys", []))
                data["budget_left"] = max(0.0, round(new_b - used_cost, 4))
                if pair in PAIRS_CONFIG:
                    PAIRS_CONFIG[pair].update({
                        "BUDGET_USD": new_b,
                        "BUY_AMOUNT": float(rev_cfg.get("buy_amount", PAIRS_CONFIG[pair].get("BUY_AMOUNT", 2.1))),
                        "TAKE_PROFIT_MARGIN": float(rev_cfg.get("take_profit_margin", PAIRS_CONFIG[pair].get("TAKE_PROFIT_MARGIN", 0.008))),
                        "DROP_THRESHOLD": float(rev_cfg.get("drop_threshold", PAIRS_CONFIG[pair].get("DROP_THRESHOLD", 0.013))),
                        "RSI_MAX_ENTRY": float(rev_cfg.get("rsi_max_entry", PAIRS_CONFIG[pair].get("RSI_MAX_ENTRY", 48))),
                        "DCA_MODE": str(rev_cfg.get("dca_mode", PAIRS_CONFIG[pair].get("DCA_MODE", "flat"))).lower()
                    })
                save_active_pairs()
                reverted_budget_info = new_b
                log_action(pair, "CONFIG_REVERTED", avg_sell_price, actual_qty, profit=net_profit,
                           message=f"Config otomatis dikembalikan ke settingan asal setelah jual Layer {recycled_num}! Budget: ${new_b:.2f}")

        # Jika seluruh layer telah terjual habis (posisi kosong), terapkan pending config atau eksekusi pending swap
        if len(data['buys']) == 0:
            if data.get("pending_config"):
                p_cfg = data.pop("pending_config", None)
                if p_cfg:
                    data["config"]["budget_usd"] = float(p_cfg["budget_usd"])
                    data["config"]["buy_amount"] = float(p_cfg["buy_amount"])
                    data["config"]["take_profit_margin"] = float(p_cfg["take_profit_margin"])
                    data["config"]["drop_threshold"] = float(p_cfg["drop_threshold"])
                    data["config"]["rsi_max_entry"] = float(p_cfg.get("rsi_max_entry", 48))
                    data["config"]["dca_mode"] = str(p_cfg.get("dca_mode", "flat")).lower()
                    data["budget_left"] = round(float(p_cfg["budget_usd"]), 2)
                    if pair in PAIRS_CONFIG:
                        PAIRS_CONFIG[pair].update({
                            "BUDGET_USD": float(p_cfg["budget_usd"]),
                            "BUY_AMOUNT": float(p_cfg["buy_amount"]),
                            "TAKE_PROFIT_MARGIN": float(p_cfg["take_profit_margin"]),
                            "DROP_THRESHOLD": float(p_cfg["drop_threshold"]),
                            "RSI_MAX_ENTRY": float(p_cfg.get("rsi_max_entry", 48)),
                            "DCA_MODE": str(p_cfg.get("dca_mode", "flat")).lower()
                        })
                    save_active_pairs()
                    log_action(pair, "CONFIG_APPLIED", avg_sell_price, actual_qty,
                               message=f"Pending config otomatis aktif setelah posisi closed! Budget: ${p_cfg['budget_usd']}, Mode: {p_cfg.get('dca_mode', 'flat')}")
            
            if data.get("pending_replacement"):
                rep_info = data.pop("pending_replacement", None)
                new_pair = rep_info.get("pair") if rep_info else None
                if new_pair and new_pair not in PAIRS:
                    execute_autopilot_swap(
                        pair, new_pair,
                        budget_usd=rep_info.get("budget_usd"),
                        buy_amount=rep_info.get("buy_amount"),
                        reason="TAKE_PROFIT",
                        dca_mode=rep_info.get("dca_mode"),
                        drop_threshold=rep_info.get("drop_threshold"),
                        take_profit_margin=rep_info.get("take_profit_margin"),
                        max_loss_percent=rep_info.get("max_loss_percent"),
                        rsi_max_entry=rep_info.get("rsi_max_entry"),
                        trailing_margin=rep_info.get("trailing_margin")
                    )

        save_data(pair, data)
        get_account_cached(force=True)
        
        action_name = "MANUAL_RECYCLE" if is_manual else "PARTIAL_TP"
        pnl_label = "UNTUNG" if net_profit >= 0 else "RUGI"
        log_action(pair, action_name, avg_sell_price, actual_qty, profit=net_profit,
                   message=f"Layer {recycled_num} {pair} berhasil dijual ({'Manual' if is_manual else 'Auto-Exit'})! Status={pnl_label} PnL={net_profit:+.4f} USDT ({gain_pct*100:+.2f}%) | Modal Kembali=+${cost_restored:.2f} USDT | Sisa Layer={len(data['buys'])}")
                   
        msg = f"Layer {recycled_num} {pair} berhasil dijual ({pnl_label} {net_profit:+.4f} USDT)! Modal kembali +${cost_restored:.2f} USDT."
        if compounded_profit > 0:
            msg += f" Auto-compound: +${compounded_profit:.4f} USDT masuk ke budget."
        if reverted_budget_info:
            msg += f" Config berhasil di-revert ke ${reverted_budget_info:.2f}."
            
        return {
            "success": True,
            "message": msg,
            "profit": round(net_profit, 4),
            "compounded": round(compounded_profit, 4),
            "cost_restored": round(cost_restored, 2),
            "remaining_layers": len(data['buys'])
        }

def recycle_deep_layer(pair, current_price=None):
    """
    Fitur 3: Layer Recycling (Partial Scalping / Auto-Exit).
    Mengeksekusi penjualan otomatis untuk layer terakhir jika:
    1. Koin memiliki minimal 2 layer aktif.
    2. Layer terakhir sudah mengendap >= auto_rescue_days (default 4 hari) DAN posisinya UNTUNG (gain_pct >= take_profit_margin),
       ATAU koin memegang >= 6 layer dan sedang memantul profit >= +3.5%.
    """
    if pair not in bot_data:
        return False
    data = bot_data[pair]
    buys = data.get('buys', [])
    if len(buys) < 2:
        return False
        
    if current_price is None or current_price <= 0:
        current_price = get_ticker_price(pair)
    if current_price <= 0:
        return False
        
    last_layer = buys[-1]
    buy_price = float(last_layer.get('price', 0.0))
    layer_qty = float(last_layer.get('qty', 0.0))
    if buy_price <= 0 or layer_qty <= 0:
        return False
        
    gain_pct = (current_price - buy_price) / buy_price
    
    # Cek durasi mengendap layer terakhir
    layer_time = float(last_layer.get("time", 0))
    if not layer_time:
        layer_time = float(data.get("last_buy_time", 0))
    if not layer_time:
        layer_time = float(data.get("lowest_price_time", 0))
    layer_age_sec = time.time() - layer_time if layer_time > 0 else 0
    
    gs = get_global_settings()
    if not gs.get("auto_rescue", True):
        return False
    rescue_days = float(gs.get("auto_rescue_days", 4))
    is_aged = layer_age_sec >= (rescue_days * 86400)
    
    cfg_tp = float(data.get("config", {}).get("take_profit_margin", 0.008))
    target_gain = max(cfg_tp, 0.003)
    
    cfg_rescue_tp = gs.get("auto_rescue_tp", 0)
    try:
        cfg_rescue_tp_val = float(cfg_rescue_tp)
    except (ValueError, TypeError):
        cfg_rescue_tp_val = 0.0
    rescue_tp = cfg_rescue_tp_val if cfg_rescue_tp_val > 0 else target_gain
    
    # Syarat Otomatis:
    # 1. Layer sudah mengendap >= auto_rescue_days (sesuai config) DAN posisinya UNTUNG (gain_pct >= target_gain)
    # 2. Atau koin memegang serokan darurat Auto-Rescue dan gain_pct >= rescue_tp (Sesuai TP / Config)
    # 3. Atau koin pegang >= 6 layer dan gain_pct >= rescue_tp (Pantulan Deep Scalping)
    should_recycle = False
    if is_aged and gain_pct >= target_gain:
        should_recycle = True
    elif data.get("revert_config_after_sell") and gain_pct >= rescue_tp:
        should_recycle = True
    elif len(buys) >= 6 and gain_pct >= rescue_tp:
        should_recycle = True
        
    if not should_recycle:
        return False
        
    # Jangan eksekusi jika harga sudah tembus target sell keseluruhan (biarkan sell_all menjual penuh)
    avg_price = get_avg_buy(pair)
    overall_tp = avg_price * (1.0 + cfg_tp) if avg_price > 0 else 0.0
    if overall_tp > 0 and current_price >= overall_tp:
        return False
        
    res = execute_sell_last_layer(pair, is_manual=False)
    return res.get("success", False)
            
    return False

def check_and_execute_auto_rescue(pair, current_price=None):
    """
    Autonomous Auto-Rescue Engine (Penyelamat Mandiri Otomatis).
    Kriteria Pemicu:
    1. Pengaturan global auto_rescue = True
    2. Koin memiliki posisi aktif (len(buys) >= 1)
    3. Dana koin habis (is_fund_exhausted(pair) == True)
    4. Koin belum memiliki revert_config_after_sell aktif (mencegah suntikan ganda)
    5. Waktu sejak serokan terakhir >= auto_rescue_days * 86400 (default: 4 hari / 96 jam)
    6. Saldo bebas dompet Binance (free_usdt) >= 2.50 USDT
    7. Harga saat ini sudah drop minimal -3.0% dari harga layer terakhir
    8. Konfirmasi Rebound: harga sudah memantul minimal +0.5% dari lowest_price & BTC aman
    """
    gs = get_global_settings()
    if not gs.get("auto_rescue", True):
        return False

    if pair not in bot_data:
        return False

    data = bot_data[pair]
    buys = data.get("buys", [])
    if len(buys) == 0:
        return False

    # Syarat 1: Modal koin harus sudah habis
    if not is_fund_exhausted(pair):
        return False

    # Syarat 2: Jangan suntik ganda jika koin sedang memegang status Auto-Revert aktif
    if data.get("revert_config_after_sell"):
        return False

    # Syarat 3: Hitung durasi mengendap (floating time)
    last_buy_ts = data.get("last_buy_time", 0)
    if not last_buy_ts and buys:
        last_buy_ts = buys[-1].get("time", 0)
    if not last_buy_ts:
        last_buy_ts = data.get("lowest_price_time", data.get("peak_time", 0))

    if not last_buy_ts:
        return False

    rescue_days = float(gs.get("auto_rescue_days", 4))
    floating_seconds = time.time() - float(last_buy_ts)
    if floating_seconds < (rescue_days * 86400):
        return False

    # Syarat 4: Cek saldo kas bebas dompet Binance
    free_usdt, _ = get_total_usdt_value_cached()
    MIN_WALLET_REQ = 2.50
    if free_usdt < MIN_WALLET_REQ:
        if DEBUG:
            print(f"[AUTO-RESCUE] {pair} syarat hari terpenuhi tapi saldo Binance (${free_usdt:.2f}) < ${MIN_WALLET_REQ:.2f}")
        return False

    if current_price is None or current_price <= 0:
        current_price = get_ticker_price(pair)
    if current_price <= 0:
        return False

    # Syarat 5: Harga harus minimal -3.0% di bawah harga serokan layer terakhir
    last_buy_price = float(buys[-1].get("price", 0.0))
    if last_buy_price > 0 and current_price > (last_buy_price * 0.97):
        if DEBUG:
            print(f"[AUTO-RESCUE] {pair} harga belum drop >= 3% dari layer terakhir ({current_price} > {last_buy_price * 0.97})")
        return False

    # Syarat 6: BTC Crash Guard
    if is_btc_dumping():
        if DEBUG:
            print(f"[AUTO-RESCUE] {pair} ditahan karena BTC sedang dump!")
        return False

    # Syarat 7: Konfirmasi Rebound: Memantul minimal +0.5% dari lowest_price lokal
    lowest_p = float(data.get("lowest_price") or current_price)
    if lowest_p > 0 and current_price < (lowest_p * 1.005):
        if DEBUG:
            print(f"[AUTO-RESCUE] {pair} menunggu konfirmasi pantulan +0.5% dari low ({lowest_p})")
        return False

    # Semua syarat terpenuhi! Eksekusi Penyelamatan Mandiri
    lock = bot_locks.get(pair)
    if not lock:
        bot_locks[pair] = threading.RLock()
        lock = bot_locks[pair]

    with lock:
        curr_cfg = dict(data.get("config", {}))
        # 1. Snapshot konfigurasi asal untuk Auto-Revert
        data["revert_config_after_sell"] = {
            "budget_usd": float(curr_cfg.get("budget_usd", 15.0)),
            "buy_amount": float(curr_cfg.get("buy_amount", 2.1)),
            "take_profit_margin": float(curr_cfg.get("take_profit_margin", 0.008)),
            "drop_threshold": float(curr_cfg.get("drop_threshold", 0.013)),
            "rsi_max_entry": float(curr_cfg.get("rsi_max_entry", 48.0)),
            "dca_mode": str(curr_cfg.get("dca_mode", "flat")).lower()
        }

        min_notion = get_notion(pair)
        emergency_cost = max(round(min_notion * 1.10, 2), 2.15)
        emergency_cost = min(emergency_cost, round(free_usdt - 0.10, 2))
        if emergency_cost < min_notion:
            data.pop("revert_config_after_sell", None)
            return False

        old_budget = float(curr_cfg.get("budget_usd", 15.0))
        data["config"]["budget_usd"] = round(old_budget + emergency_cost, 2)
        data["budget_left"] = emergency_cost
        save_data(pair, data)

        log_action(pair, "AUTO_RESCUE", current_price, 0,
                   message=f"🛟 AUTO-RESCUE DIAKTIFKAN: Koin nyangkut {floating_seconds/86400:.1f} hari! Modal darurat +${emergency_cost:.2f} disuntikkan dari dompet Binance (Auto-Revert aktif ke ${old_budget:.2f} setelah Sell).")

        # Eksekusi pembelian serok darurat
        success = buy(pair, force=True)
        if not success:
            # Jika pembelian gagal, kembalikan data config
            data["config"].update(data.pop("revert_config_after_sell", {}))
            data["budget_left"] = 0.0
            save_data(pair, data)
            return False

        return True

def get_next_layer_str(pair, data, price, avg_buy):
    buys_count = len(data.get('buys', []))
    next_layer_num = buys_count + 1

    if avg_buy > 0 and not is_fund_exhausted(pair):
        drop_req = get_dynamic_drop_threshold(pair)
        next_target = avg_buy * (1 - drop_req)
        return "Layer {}: {} (-{}% dr AVG)".format(next_layer_num, fmt(next_target), round(drop_req * 100, 2))
    elif buys_count == 0:
        history = data.get('price_history', [])
        raw_prices = [p.get('price', 0) if isinstance(p, dict) else float(p) for p in history]
        valid_prices = [p for p in raw_prices if p > 0]
        if valid_prices:
            recent_peak = max(valid_prices)
            hours_running = (time.time() - data.get('peak_time', time.time())) / 3600
            cfg_drop = data["config"].get("drop_threshold", 0.013)
            threshold = cfg_drop
            next_target_4h = recent_peak * (1 - threshold)
            
            global_peak = data.get('peak_price', price)
            days_running = hours_running / 24
            next_target_sb = global_peak * (1 - 0.025)
            
            if days_running >= 7 and next_target_sb > next_target_4h:
                return "Layer 1: {} (-2.5% dr Global Peak)".format(fmt(next_target_sb))
            else:
                return "Layer 1: {} (-{}% dr Peak)".format(fmt(next_target_4h), round(threshold * 100, 1))
        else:
            return "Layer 1: Menunggu data harga"
    elif is_fund_exhausted(pair):
        return f"Maksimal ({buys_count} Layer Terisi)"
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
            profit_pct = round((profit / total_cost) * 100, 2) if total_cost > 0 else 0.0
            
            # Estimasi keuntungan bersih saat Take Profit (setelah 2x fee buy + sell)
            tp_price_val = calculate_dca_tp_target(avg_buy, len(data.get('buys', [])), data["config"].get("take_profit_margin", 0.008))
            tp_val_est = total_doge * tp_price_val
            tp_fee_est = (total_cost * fee_rate) + (tp_val_est * fee_rate)
            tp_profit = round((tp_val_est - total_cost) - tp_fee_est, 4)
            tp_profit_pct = round((tp_profit / total_cost) * 100, 2) if total_cost > 0 else 0.0
        else:
            profit = 0
            profit_pct = 0.0
            tp_profit = 0.0
            tp_profit_pct = 0.0
            
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
        chart_prices = []
        chart_labels = []
        now_ts = int(time.time())
        hist_len = len(price_history)
        for idx, p in enumerate(price_history):
            if isinstance(p, dict):
                p_val = float(p.get('price', 0.0))
                t_val = str(p.get('time', ''))
                if not t_val:
                    mins_ago = (hist_len - 1 - idx) * 5
                    t_val = datetime.fromtimestamp(now_ts - mins_ago * 60).strftime("%H:%M")
                chart_prices.append(p_val)
                chart_labels.append(t_val)
            else:
                try:
                    p_val = float(p)
                except (ValueError, TypeError):
                    p_val = 0.0
                mins_ago = (hist_len - 1 - idx) * 5
                t_val = datetime.fromtimestamp(now_ts - mins_ago * 60).strftime("%H:%M")
                chart_prices.append(p_val)
                chart_labels.append(t_val)
        take_profit = calculate_dca_tp_target(avg_buy, len(data['buys']), data["config"]["take_profit_margin"])
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
        except Exception:
            logs = "No logs yet."

        total_cost = sum([float(b['qty']) * float(b['price']) for b in data.get('buys', [])]) if data.get('buys') else 0.0
        current_value = total_doge * price if avg_buy > 0 else 0.0
        used_cost = round(total_cost, 2)
        current_val = round(current_value, 2)

        cfg_rescue_tp = get_global_settings().get("auto_rescue_tp", 0)
        try:
            cfg_rescue_tp_val = float(cfg_rescue_tp)
        except (ValueError, TypeError):
            cfg_rescue_tp_val = 0.0
        c_tp = float(data.get("config", {}).get("take_profit_margin", 0.008))
        rescue_tp_rate = cfg_rescue_tp_val if cfg_rescue_tp_val > 0 else c_tp
        
        is_ar_on = bool(get_global_settings().get("auto_rescue", True))
        scalp_target_str = ""
        if is_ar_on and (len(data.get('buys', [])) >= 6 or data.get('revert_config_after_sell')):
            last_layer = data['buys'][-1]
            scalp_target = float(last_layer.get('price', 0.0)) * (1.0 + rescue_tp_rate)
            label_suffix = f"+{rescue_tp_rate*100:.1f}%" + (" (Sesuai TP)" if cfg_rescue_tp_val <= 0 else "")
            scalp_target_str = f"Target Scalp L{len(data['buys'])}: {fmt(scalp_target)} ({label_suffix})"

        floating_days = round((time.time() - float(data.get("last_buy_time") or (data["buys"][-1].get("time", 0) if data.get("buys") else data.get("peak_time", time.time())))) / 86400, 1) if data.get("buys") else 0.0
        is_rescue_ready = is_fund_exhausted(pair) and (floating_days >= float(get_global_settings().get("auto_rescue_days", 4))) and not data.get("revert_config_after_sell")

        is_idle = len(data.get('buys', [])) == 0
        idle_since = float(data.get('idle_since') or data.get('peak_time', time.time())) if is_idle else 0
        idle_hours = round((time.time() - idle_since) / 3600.0, 1) if is_idle else 0.0
        g_s = get_global_settings()
        max_idle_hours = float(g_s.get("auto_pilot_idle_hours", 12.0))
        is_idle_rot_enabled = bool(g_s.get("auto_pilot", False)) and bool(g_s.get("auto_pilot_idle_rotation", True))

        pairs_data.append({
            "pair": pair,
            "coin_name": coin_name,
            "data": data,
            "price": fmt(price),
            "avg_buy": fmt(avg_buy),
            "profit": profit,
            "profit_pct": profit_pct,
            "tp_profit": tp_profit,
            "tp_profit_pct": tp_profit_pct,
            "used_cost": used_cost,
            "current_val": current_val,
            "logs": logs,
            "buys": format_buys_log(data.get('buys', [])),
            "peak_price": fmt(peak_price),
            "lowest_price": fmt(lowest_price),
            "selisih": selisih,
            "drop": drop,
            "free_usdt": free_usdt,
            "total_usdt": total_usdt,
            "chart_data": chart_prices,
            "chart_labels": chart_labels,
            "history_change_pct": history_volatility_pct,
            "next_layer_str": next_layer_str,
            "take_profit": fmt(take_profit),
            "scalp_target_str": scalp_target_str,
            "rsi": get_rsi(pair),
            "rsi_max_entry": data["config"].get("rsi_max_entry", 48),
            "floating_days": floating_days,
            "is_rescue_ready": is_rescue_ready,
            "is_idle": is_idle,
            "idle_hours": idle_hours,
            "max_idle_hours": max_idle_hours,
            "is_idle_rot_enabled": is_idle_rot_enabled
        })

    return render_template('index.html', pairs_data=pairs_data, now=now_str, btc_status=get_btc_guard_status())

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
            profit_pct = round((profit / total_cost) * 100, 2) if total_cost > 0 else 0.0
            
            # Estimasi keuntungan bersih saat Take Profit (setelah 2x fee buy + sell)
            tp_price_val = calculate_dca_tp_target(avg_buy, len(data.get('buys', [])), data["config"].get("take_profit_margin", 0.008))
            tp_val_est = total_qty * tp_price_val
            tp_fee_est = (total_cost * fee_rate) + (tp_val_est * fee_rate)
            tp_profit = round((tp_val_est - total_cost) - tp_fee_est, 4)
            tp_profit_pct = round((tp_profit / total_cost) * 100, 2) if total_cost > 0 else 0.0
        else:
            total_cost = 0.0
            current_value = 0.0
            profit = 0
            profit_pct = 0.0
            tp_profit = 0.0
            tp_profit_pct = 0.0
            
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
        take_profit = calculate_dca_tp_target(avg_buy, len(data.get('buys', [])), data["config"].get("take_profit_margin", 0.008))
        status = data["config"].get("status", 1)
        next_layer_str = get_next_layer_str(pair, data, price, avg_buy)
        
        try:
            with open(get_log_file(pair), 'r') as f:
                logs_list = f.readlines()[-20:]
                logs_str = "".join(reversed(logs_list))
        except Exception:
            logs_str = "No logs yet."
        
        buys_str = format_buys_log(data.get('buys', []))
        
        cfg_rescue_tp = get_global_settings().get("auto_rescue_tp", 0)
        try:
            cfg_rescue_tp_val = float(cfg_rescue_tp)
        except (ValueError, TypeError):
            cfg_rescue_tp_val = 0.0
        c_tp = float(data.get("config", {}).get("take_profit_margin", 0.008))
        rescue_tp_rate = cfg_rescue_tp_val if cfg_rescue_tp_val > 0 else c_tp
        
        is_ar_on = bool(get_global_settings().get("auto_rescue", True))
        scalp_target_str = ""
        if is_ar_on and (len(data.get('buys', [])) >= 6 or data.get('revert_config_after_sell')):
            last_layer = data['buys'][-1]
            scalp_target = float(last_layer.get('price', 0.0)) * (1.0 + rescue_tp_rate)
            label_suffix = f"+{rescue_tp_rate*100:.1f}%" + (" (Sesuai TP)" if cfg_rescue_tp_val <= 0 else "")
            scalp_target_str = f"Target Scalp L{len(data['buys'])}: {fmt(scalp_target)} ({label_suffix})"

        is_idle = len(data.get('buys', [])) == 0
        idle_since = float(data.get('idle_since') or data.get('peak_time', time.time())) if is_idle else 0
        idle_hours = round((time.time() - idle_since) / 3600.0, 1) if is_idle else 0.0
        g_s = get_global_settings()
        max_idle_hours = float(g_s.get("auto_pilot_idle_hours", 12.0))
        is_idle_rot_enabled = bool(g_s.get("auto_pilot", False)) and bool(g_s.get("auto_pilot_idle_rotation", True))

        pairs_info[pair] = {
            "price": fmt(price),
            "avg_buy": fmt(avg_buy),
            "profit": profit,
            "profit_pct": profit_pct,
            "tp_profit": tp_profit,
            "tp_profit_pct": tp_profit_pct,
            "used_cost": round(total_cost, 2),
            "current_val": round(current_value, 2),
            "peak_price": fmt(peak_price),
            "lowest_price": fmt(lowest_price),
            "selisih": selisih,
            "drop": drop,
            "budget_left": round(data.get('budget_left', 0), 2),
            "budget_usd": data["config"].get("budget_usd", data["config"].get("BUDGET_USD", 0)),
            "buy_amount": data["config"].get("buy_amount", data["config"].get("BUY_AMOUNT", 0)),
            "take_profit_margin": data["config"].get("take_profit_margin", data["config"].get("TAKE_PROFIT_MARGIN", 0.008)),
            "drop_threshold": data["config"].get("drop_threshold", data["config"].get("DROP_THRESHOLD", 0.01)),
            "rsi_max_entry": data["config"].get("rsi_max_entry", data["config"].get("RSI_MAX_ENTRY", 48)),
            "layers_count": len(data.get('buys', [])),
            "take_profit": fmt(take_profit),
            "next_layer_str": next_layer_str,
            "scalp_target_str": scalp_target_str,
            "buys": buys_str,
            "logs": logs_str,
            "status": status,
            "force_sell": data["config"].get("force_sell", False),
            "pending_replacement": data.get("pending_replacement"),
            "pending_config": data.get("pending_config"),
            "revert_config_after_sell": data.get("revert_config_after_sell"),
            "rsi": get_rsi(pair),
            "dca_mode": data["config"].get("dca_mode", data["config"].get("DCA_MODE", "flat")),
            "is_locked": pair in get_global_settings().get("locked_pairs", []),
            "floating_days": round((time.time() - float(data.get("last_buy_time") or (data["buys"][-1].get("time", 0) if data.get("buys") else data.get("peak_time", time.time())))) / 86400, 1) if data.get("buys") else 0.0,
            "is_rescue_ready": is_fund_exhausted(pair) and ((time.time() - float(data.get("last_buy_time") or (data["buys"][-1].get("time", 0) if data.get("buys") else data.get("peak_time", time.time())))) >= (float(get_global_settings().get("auto_rescue_days", 4)) * 86400)) and not data.get("revert_config_after_sell"),
            "is_idle": is_idle,
            "idle_hours": idle_hours,
            "max_idle_hours": max_idle_hours,
            "is_idle_rot_enabled": is_idle_rot_enabled
        }
    free_usdt, total_usdt = get_total_usdt_value_cached()
    return jsonify({
        "success": True,
        "now": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "free_usdt": free_usdt,
        "total_usdt": total_usdt,
        "global_settings": get_global_settings(),
        "btc_status": get_btc_guard_status(),
        "pairs": pairs_info
    })

@app.route('/api/global_settings')
def api_get_global_settings():
    return jsonify({"success": True, "settings": get_global_settings()})

@app.route('/api/action/toggle_autopilot', methods=['POST'])
def api_action_toggle_autopilot():
    try:
        s = get_global_settings()
        s["auto_pilot"] = not s.get("auto_pilot", False)
        save_global_settings(s)
        status_txt = "AKTIF 🚀" if s["auto_pilot"] else "NONAKTIF ⏸️"
        return jsonify({"success": True, "message": f"Mode Auto-Pilot Rotator sekarang {status_txt}!", "settings": s})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/action/update_autopilot_settings', methods=['POST'])
def api_action_update_autopilot_settings():
    try:
        data_req = request.get_json(force=True) or {}
        s = get_global_settings()
        if 'idle_rotation' in data_req:
            s['auto_pilot_idle_rotation'] = bool(data_req['idle_rotation'])
        if 'idle_hours' in data_req:
            h = float(data_req['idle_hours'])
            if 1 <= h <= 72:
                s['auto_pilot_idle_hours'] = h
            else:
                return jsonify({"success": False, "message": "Batas jam idle harus antara 1 sampai 72 jam!"}), 400
        save_global_settings(s)
        rot_status = "AKTIF" if s.get("auto_pilot_idle_rotation", True) else "NONAKTIF"
        return jsonify({
            "success": True,
            "message": f"Pengaturan Auto-Pilot diperbarui (Idle Rotator: {rot_status}, Ambang: {s.get('auto_pilot_idle_hours', 12)} jam)!",
            "settings": s
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/action/toggle_autocompound', methods=['POST'])
def api_action_toggle_autocompound():
    try:
        s = get_global_settings()
        s["auto_compound"] = not s.get("auto_compound", False)
        save_global_settings(s)
        status_txt = "AKTIF 💰" if s["auto_compound"] else "NONAKTIF ⏸️"
        return jsonify({"success": True, "message": f"Auto-Compounding Profit sekarang {status_txt}!", "settings": s})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/action/toggle_btc_guard', methods=['POST'])
def api_action_toggle_btc_guard():
    try:
        s = get_global_settings()
        s["btc_guard"] = not s.get("btc_guard", True)
        save_global_settings(s)
        status_txt = "AKTIF 🛡️" if s["btc_guard"] else "NONAKTIF ⏸️"
        return jsonify({"success": True, "message": f"BTC Crash Guard sekarang {status_txt}!", "settings": s})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/action/toggle_autorescue', methods=['POST'])
def api_action_toggle_autorescue():
    try:
        s = get_global_settings()
        s["auto_rescue"] = not s.get("auto_rescue", True)
        save_global_settings(s)
        status_txt = "AKTIF" if s["auto_rescue"] else "NONAKTIF"
        return jsonify({"success": True, "message": f"Autonomous Auto-Rescue sekarang {status_txt}!", "settings": s})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/action/update_autorescue_days', methods=['POST'])
def api_action_update_autorescue_days():
    try:
        data_req = request.get_json(force=True) or {}
        days = data_req.get('days')
        tp = data_req.get('tp')
        s = get_global_settings()
        msg_parts = []
        if days is not None:
            days = float(days)
            if days < 1 or days > 30:
                return jsonify({"success": False, "message": "Ambang batas hari harus antara 1 sampai 30 hari!"}), 400
            s["auto_rescue_days"] = days
            msg_parts.append(f"ambang {days:g} hari")
        if tp is not None:
            tp = float(tp)
            if tp != 0 and (tp < 0.005 or tp > 0.10):
                return jsonify({"success": False, "message": "Target TP harus antara 0.5% sampai 10% (atau 0 untuk Sesuai TP)!"}), 400
            s["auto_rescue_tp"] = tp
            msg_parts.append("target TP Sesuai TP Koin" if tp == 0 else f"target TP {tp*100:g}%")
        save_global_settings(s)
        msg = f"Pengaturan Auto-Rescue berhasil disetel ({', '.join(msg_parts)})!" if msg_parts else "Pengaturan Auto-Rescue berhasil disimpan!"
        return jsonify({"success": True, "message": msg, "settings": s})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/action/toggle_lock_pair', methods=['POST'])
def api_action_toggle_lock_pair():
    try:
        data_req = request.get_json(force=True) or {}
        pair = data_req.get('pair')
        if not pair:
            return jsonify({"success": False, "message": "Pair required"}), 400
        s = get_global_settings()
        locked = s.get("locked_pairs", [])
        if pair in locked:
            locked.remove(pair)
            is_locked = False
            msg = f"Koin {pair} sekarang DILEPAS (Bisa dirotasi oleh Auto-Pilot) 🔓"
        else:
            locked.append(pair)
            is_locked = True
            msg = f"Koin {pair} sekarang DIKUNCI (Tidak akan dirotasi) 🔒"
        s["locked_pairs"] = locked
        save_global_settings(s)
        return jsonify({"success": True, "message": msg, "is_locked": is_locked, "settings": s})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/action/update_slots', methods=['POST'])
def api_action_update_slots():
    try:
        data_req = request.get_json(force=True) or {}
        new_slots = int(data_req.get('max_slots', 3))
        if new_slots < 1 or new_slots > 10:
            return jsonify({"success": False, "message": "Jumlah slot harus antara 1 sampai 10!"}), 400
        s = get_global_settings()
        s["max_slots"] = new_slots
        save_global_settings(s)
        return jsonify({"success": True, "message": f"Kapasitas slot trading berhasil diubah menjadi {new_slots} slot!", "settings": s})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

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
        new_dca_mode = str(data_req.get('dca_mode', 'smart')).lower()
        if new_dca_mode not in ['smart', 'flat']:
            new_dca_mode = 'smart'
        
        if new_budget <= 0 or new_buy_amount <= 0 or new_tp <= 0 or new_drop <= 0 or new_rsi_max <= 0:
            return jsonify({"success": False, "message": "Nilai parameter harus lebih besar dari 0!"}), 400
            
        if new_rsi_max > 90:
            return jsonify({"success": False, "message": "Batas RSI maksimal 90!"}), 400
            
        if new_dca_mode == 'flat' and new_buy_amount > new_budget:
            return jsonify({"success": False, "message": "Buy amount per layer tidak boleh lebih besar dari Total Budget!"}), 400
            
        apply_mode = str(data_req.get('apply_mode', 'queued')).lower()
        auto_revert = bool(data_req.get('auto_revert', False))

        with bot_locks[pair]:
            buys_count = len(bot_data[pair].get("buys", []))
            if new_dca_mode == 'smart':
                adaptive_layers = len(get_smart_pyramid_weights(new_budget, 1.0))
            else:
                adaptive_layers = max(1, int(floor(new_budget / new_buy_amount))) if new_buy_amount > 0 else 7

            cfg_dict = {
                "budget_usd": new_budget,
                "buy_amount": new_buy_amount,
                "take_profit_margin": new_tp,
                "drop_threshold": new_drop,
                "rsi_max_entry": new_rsi_max,
                "dca_mode": new_dca_mode,
                "max_layer": adaptive_layers
            }
            mode_label = "Piramida Cerdas (Smart Multiplier)" if new_dca_mode == 'smart' else "Flat DCA"
            
            if buys_count > 0 and apply_mode != 'instant':
                # Koin sedang memiliki open posisi / floating & user pilih antrean -> Simpan sebagai antrean pending config
                bot_data[pair]["pending_config"] = cfg_dict
                save_data(pair, bot_data[pair])
                return jsonify({
                    "success": True,
                    "is_pending": True,
                    "message": f"Konfigurasi baru {pair} disimpan sebagai ANTRIAN! Karena koin sedang pegang {buys_count} layer, perubahan ini akan otomatis aktif setelah posisi saat ini Take Profit (Closed Buy)."
                })
            else:
                # Koin sedang 0 posisi ATAU user memilih Hard Update (Instant) -> Terapkan langsung seketika
                curr_cfg = dict(bot_data[pair].get("config", {}))
                if auto_revert:
                    bot_data[pair]["revert_config_after_sell"] = {
                        "budget_usd": float(curr_cfg.get("budget_usd", new_budget)),
                        "buy_amount": float(curr_cfg.get("buy_amount", new_buy_amount)),
                        "take_profit_margin": float(curr_cfg.get("take_profit_margin", new_tp)),
                        "drop_threshold": float(curr_cfg.get("drop_threshold", new_drop)),
                        "rsi_max_entry": float(curr_cfg.get("rsi_max_entry", new_rsi_max)),
                        "dca_mode": str(curr_cfg.get("dca_mode", new_dca_mode)).lower()
                    }
                else:
                    bot_data[pair].pop("revert_config_after_sell", None)

                cfg = bot_data[pair]["config"]
                cfg.update(cfg_dict)
                bot_data[pair].pop("pending_config", None)
                
                # Hitung sisa budget: Total Budget baru dikurangi modal yang sudah terbeli di open layer
                used_cost = 0.0
                for b in bot_data[pair].get("buys", []):
                    used_cost += float(b.get("quote_spent") or (float(b.get("qty", 0)) * float(b.get("price", 0))))
                bot_data[pair]["budget_left"] = max(0.0, round(new_budget - used_cost, 4))
                
                # Jika ada antrean ganti koin, otomatis sinkronkan budget untuk koin pengganti
                if bot_data[pair].get("pending_replacement"):
                    bot_data[pair]["pending_replacement"]["budget_usd"] = new_budget
                    bot_data[pair]["pending_replacement"]["buy_amount"] = new_buy_amount
                
                save_data(pair, bot_data[pair])
                if pair not in PAIRS_CONFIG:
                    PAIRS_CONFIG[pair] = {}
                PAIRS_CONFIG[pair].update({
                    "BUDGET_USD": new_budget,
                    "BUY_AMOUNT": new_buy_amount,
                    "TAKE_PROFIT_MARGIN": new_tp,
                    "DROP_THRESHOLD": new_drop,
                    "RSI_MAX_ENTRY": new_rsi_max,
                    "DCA_MODE": new_dca_mode,
                    "MAX_LAYER": adaptive_layers,
                    "STATUS": bot_data[pair]["config"].get("status", 1)
                })
                save_active_pairs()
                
                applied_mode_str = "Hard Update (Langsung Aktif)" if (buys_count > 0 and apply_mode == 'instant') else "Langsung Aktif"
                revert_msg = " [🔄 Auto-Revert Aktif]" if auto_revert else ""
                return jsonify({
                    "success": True, 
                    "is_pending": False,
                    "auto_revert": auto_revert,
                    "revert_config_after_sell": bot_data[pair].get("revert_config_after_sell"),
                    "message": f"Konfigurasi {pair} berhasil diterapkan ({applied_mode_str}){revert_msg}! (Budget: ${new_budget}, Sisa: ${bot_data[pair]['budget_left']}, Mode: {mode_label}, TP: {round(new_tp*100, 2)}%)"
                })
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal update config: {str(e)}"}), 500

@app.route('/api/action/cancel_pending_config', methods=['POST'])
def api_action_cancel_pending_config():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
        
    with bot_locks[pair]:
        if "pending_config" in bot_data[pair]:
            del bot_data[pair]["pending_config"]
            save_data(pair, bot_data[pair])
            return jsonify({"success": True, "message": f"Antrean konfigurasi baru untuk {pair} berhasil dibatalkan."})
        return jsonify({"success": True, "message": f"Tidak ada antrean konfigurasi pada {pair}."})

@app.route('/api/action/cancel_auto_revert', methods=['POST'])
def api_action_cancel_auto_revert():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
        
    with bot_locks[pair]:
        if "revert_config_after_sell" in bot_data[pair]:
            del bot_data[pair]["revert_config_after_sell"]
            save_data(pair, bot_data[pair])
            return jsonify({"success": True, "message": f"Auto-Revert untuk {pair} berhasil dibatalkan. Konfigurasi saat ini dipertahankan permanen."})
        return jsonify({"success": True, "message": f"Tidak ada Auto-Revert aktif pada {pair}."})

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
    
    if is_fund_exhausted(pair):
        return jsonify({"success": False, "message": f"Sisa budget untuk {pair} (${bot_data[pair].get('budget_left', 0):.2f}) sudah habis atau saldo USDT tidak mencukupi untuk serok layer baru!"}), 400
        
    next_layer = len(bot_data[pair].get("buys", [])) + 1
    threading.Thread(target=buy, args=(pair, True), daemon=True).start()
    return jsonify({"success": True, "message": f"Force Buy Layer {next_layer} untuk {pair} sedang dieksekusi di harga pasar!"})

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

@app.route('/api/action/preview_manual_buy', methods=['POST'])
def api_action_preview_manual_buy():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
    res = preview_manual_buy(pair)
    return jsonify(res)

@app.route('/api/action/preview_manual_sell', methods=['POST'])
def api_action_preview_manual_sell():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
    res = preview_manual_sell(pair)
    return jsonify(res)

@app.route('/api/action/preview_sell_last_layer', methods=['POST'])
def api_action_preview_sell_last_layer():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
    res = preview_sell_last_layer(pair)
    return jsonify(res)

@app.route('/api/action/sell_last_layer', methods=['POST'])
def api_action_sell_last_layer():
    data_req = request.get_json() or {}
    pair = data_req.get('pair')
    if not pair or pair not in bot_data:
        return jsonify({"success": False, "message": f"Invalid pair: {pair}"}), 400
    res = execute_sell_last_layer(pair, is_manual=True)
    return jsonify(res)

@app.route('/api/action/sweep_dust', methods=['POST'])
def api_action_sweep_dust():
    try:
        try:
            dustable = client.get_dust_assets()
            details = dustable.get('details', [])
            # 1. USDT (saldo kas) dan BNB (target transfer) dilindungi mutlak
            protected_assets = set(['USDT', 'BNB'])
            # 2. Seluruh koin aktif di tab bot (PAIRS) & koin yang punya posisi/layer DILINDUNGI MUTLAK dari sweep
            protected_assets.update([p.replace('USDT', '') for p in PAIRS])
            for p, pdata in bot_data.items():
                if pdata.get('buys') and len(pdata['buys']) > 0:
                    protected_assets.add(p.replace('USDT', ''))
            
            assets_to_convert = [
                d['asset'] for d in details 
                if float(d.get('toBNB', 0)) > 0 
                and float(d.get('amountFree', 0)) > 0 
                and d['asset'] not in protected_assets
            ]
            
            if not assets_to_convert:
                return jsonify({"success": True, "message": "Tidak ada saldo koin receh (< $1) di luar koin aktif yang memenuhi syarat untuk dikonversi."})
                
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

@app.route('/api/wallet/assets')
def api_wallet_assets():
    try:
        acc = get_account_cached(force=True)
        if not acc:
            return jsonify({"success": False, "message": "Gagal mengambil data akun Binance"}), 500
        
        balances = acc.get('balances', [])
        active_assets = []
        total_usd = 0.0
        free_usdt = 0.0
        
        active_pair_bases = [p.replace('USDT', '') for p in PAIRS]
        
        for b in balances:
            free = float(b.get('free', 0.0))
            locked = float(b.get('locked', 0.0))
            tot = free + locked
            if tot <= 0:
                continue
            asset = b.get('asset', '')
            val_usd = 0.0
            price = 0.0
            if asset == 'USDT':
                val_usd = tot
                price = 1.0
                free_usdt = free
            else:
                asset_pair = f"{asset}USDT"
                try:
                    price = get_ticker_price(asset_pair)
                    val_usd = tot * price
                except Exception:
                    price = 0.0
                    val_usd = 0.0
                    
            if val_usd >= 0.01 or tot >= 0.001 or asset in ['USDT', 'BNB'] or asset in active_pair_bases:
                status_desc = "Kas Bebas (Siap Trading)" if asset == 'USDT' else ("Gas Fee Binance (Diskon Fee 25%)" if asset == 'BNB' else ("Sedang Diprogram Trading Bot" if asset in active_pair_bases else "Aset Koin Sisa"))
                active_assets.append({
                    "asset": asset,
                    "free": free,
                    "locked": locked,
                    "total": tot,
                    "price": price,
                    "val_usd": round(val_usd, 4),
                    "is_active_bot": asset in active_pair_bases,
                    "status_desc": status_desc
                })
                total_usd += val_usd
                
        for a in active_assets:
            a["pct"] = round((a["val_usd"] / total_usd * 100), 2) if total_usd > 0 else 0.0
            
        active_assets.sort(key=lambda x: -x["val_usd"])
        
        return jsonify({
            "success": True,
            "total_usd": round(total_usd, 4),
            "free_usdt": round(free_usdt, 4),
            "assets": active_assets
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/action/sell_bnb_to_usdt', methods=['POST'])
def api_action_sell_bnb_to_usdt():
    try:
        data_req = request.get_json() or {}
        keep_bnb = float(data_req.get('keep_bnb', 0.005))
        sell_amount_bnb = data_req.get('bnb_amount')
        
        get_account_cached(force=True)
        live_bnb = get_balance_from_cache('BNB')
        
        if sell_amount_bnb is not None and float(sell_amount_bnb) > 0:
            bnb_to_sell = float(sell_amount_bnb)
        else:
            bnb_to_sell = max(0.0, live_bnb - keep_bnb)
            
        step = get_step_size('BNBUSDT')
        qty = floor_to_step(bnb_to_sell, step)
        
        if qty <= 0:
            return jsonify({
                "success": False, 
                "message": f"Saldo BNB Anda ({live_bnb:.4f} BNB) sudah di bawah batas minimum yang disimpan ({keep_bnb:.4f} BNB)."
            }), 400
            
        ticker = safe_api_call(client.get_symbol_ticker, symbol='BNBUSDT')
        bnb_price = float(ticker['price'])
        notional = qty * bnb_price
        
        if notional < 5.0:
            return jsonify({
                "success": False, 
                "message": f"Nilai order penjualan BNB (${notional:.2f} USDT) di bawah batas minimum Binance ($5.00 USDT). Minimal jual sekitar ${(5.0 / bnb_price):.4f} BNB."
            }), 400
            
        order = safe_api_call(
            client.order_market_sell,
            symbol='BNBUSDT',
            quantity=qty,
            newOrderRespType='FULL'
        )
        get_account_cached(force=True)
        
        return jsonify({
            "success": True,
            "sold_qty": qty,
            "estimated_usdt": round(notional, 2),
            "message": f"Sukses menjual {qty} BNB menjadi ~${notional:.2f} USDT! Saldo kas bebas USDT Anda sekarang bertambah."
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal menjual BNB: {str(e)}"}), 500

def sync_trades_from_binance_api(target_pair=None):
    """
    Mengambil riwayat transaksi resmi dari Binance API (limit 100 per pair),
    merekonstruksi siklus akumulasi BUY dan Take Profit SELL lengkap dengan profit & fee,
    lalu memperbarui tabel SQLite trade_history secara otomatis tanpa file txt.
    """
    conn = db.get_db_connection()
    c = conn.cursor()

    if target_pair:
        pairs_to_sync = [target_pair]
    else:
        c.execute("SELECT DISTINCT pair FROM pairs_config UNION SELECT DISTINCT pair FROM trade_history")
        known_pairs = [r[0] for r in c.fetchall() if r[0]]
        pairs_to_sync = list(dict.fromkeys(list(PAIRS) + known_pairs + ["PEPEUSDT", "NEIROUSDT", "TUTUSDT", "DOGEUSDT"]))
    
    results = {}
    
    for pair in pairs_to_sync:
        try:
            trades = safe_api_call(client.get_my_trades, symbol=pair, limit=100)
            if not trades:
                results[pair] = 0
                continue
            trades.sort(key=lambda x: x['time'])
            
            # Ambil key transaksi yang sudah ada di database untuk mencegah duplikasi
            c.execute("SELECT trade_date, trade_time, action, price, qty FROM trade_history WHERE pair = ?", (pair,))
            seen_tx = set((r[0], r[1], str(r[2]).strip(), round(float(r[3]), 8), round(float(r[4]), 8)) for r in c.fetchall())
            
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
                    msg = f"cost={quote_qty:.8f} USDT | fee={fee_str} | usable_quote={quote_qty:.6f}"
                else:
                    avg_buy_price = (total_buy_cost / total_buy_qty) if total_buy_qty > 0 else price
                    profit = max(0.0, (price - avg_buy_price) * qty - total_fee_usdt - fee_usdt)
                    msg = f"fee={fee_str}"
                    total_buy_qty = 0.0
                    total_buy_cost = 0.0
                    total_fee_usdt = 0.0
                
                key = (d_str, t_str, action, round(price, 8), round(qty, 8))
                if key not in seen_tx:
                    seen_tx.add(key)
                    c.execute("""
                    INSERT INTO trade_history (trade_date, trade_time, pair, action, price, qty, profit, message, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (d_str, t_str, pair, action, price, qty, profit, msg, int(time.time())))
                    synced_count += 1
                    
            results[pair] = synced_count
        except Exception as e:
            if DEBUG: print(f"Error syncing {pair}: {e}")
            results[pair] = -1
            
    conn.commit()
    conn.close()
    return results

@app.route('/api/action/sync_trades', methods=['POST'])
def api_action_sync_trades():
    try:
        data_req = request.get_json() or {}
        pair = data_req.get('pair')
        results = sync_trades_from_binance_api(pair)
        
        synced_count = sum(v for v in results.values() if v > 0)
        return jsonify({
            "success": True,
            "results": results,
            "message": f"Berhasil menyinkronkan {synced_count} riwayat transaksi live langsung dari Binance!"
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal sinkronisasi Binance: {str(e)}"}), 500

@app.route('/api/scanner')
def api_scanner():
    try:
        max_notional = float(request.args.get('max_notional', 2.5))
        candidates = scan_market_candidates(max_notional=max_notional)
        max_slots = int(get_global_settings().get("max_slots", 3))
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
    try:
        data_req = request.get_json() or {}
        pair = data_req.get('pair', '').upper().strip()
        if not pair:
            return jsonify({"success": False, "message": "Pair harus diisi!"}), 400
        if not pair.endswith('USDT'):
            pair += 'USDT'
        
        days = int(data_req.get('days', 30))
        budget_usd = float(data_req.get('budget_usd', 15.0))
        buy_amount = float(data_req.get('buy_amount', 2.1))
        tp_raw = float(data_req.get('take_profit_margin', 0.008))
        take_profit_margin = (tp_raw / 100.0) if tp_raw >= 0.05 else tp_raw
        drop_raw = float(data_req.get('drop_threshold', 0.013))
        drop_threshold = (drop_raw / 100.0) if drop_raw >= 0.05 else drop_raw
        dca_mode = str(data_req.get('dca_mode', 'flat')).lower()
        is_autopilot = bool(data_req.get('is_autopilot', False))
        start_date = data_req.get('start_date')
        end_date = data_req.get('end_date')
        
        result = run_dca_backtest(
            pair,
            days=days,
            budget_usd=budget_usd,
            buy_amount=buy_amount,
            take_profit_margin=take_profit_margin,
            drop_threshold=drop_threshold,
            dca_mode=dca_mode,
            is_autopilot=is_autopilot,
            start_date=start_date,
            end_date=end_date
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

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
        
    max_slots = int(get_global_settings().get("max_slots", 3))
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
    
    if len(PAIRS) >= max_slots:
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
                "message": f"Slot trading penuh ({len(PAIRS)}/{max_slots} koin aktif)! Pilih koin yang ingin digantikan."
            }), 400
            
        try:
            with bot_locks.get(replace_pair, threading.RLock()):
                target_data = bot_data.get(replace_pair, {})
                buys_count = len(target_data.get("buys", []))
                old_cfg = target_data.get("config", {})
                inherited_mode = old_cfg.get("dca_mode", "smart")
                inherited_drop = old_cfg.get("drop_threshold", 0.013)
                inherited_tp = old_cfg.get("take_profit_margin", 0.008)
                inherited_loss = old_cfg.get("max_loss_percent", -15.0)
                inherited_fee = old_cfg.get("fee_rate", 0.001)
                inherited_trail = old_cfg.get("trailing_margin", 0.001)
                inherited_rsi = old_cfg.get("rsi_max_entry", 48.0)
                
                # Kasus 1: Koin lama sedang KOSONG (0 layer) -> Langsung ganti seketika
                if buys_count == 0:
                    if replace_pair in PAIRS:
                        PAIRS.remove(replace_pair)
                    PAIRS.append(pair)
                    PAIRS_CONFIG[pair] = {
                        "BUDGET_USD": budget_usd,
                        "BUY_AMOUNT": buy_amount,
                        "DROP_THRESHOLD": inherited_drop,
                        "MAX_LOSS_PERCENT": inherited_loss,
                        "FEE_RATE": inherited_fee,
                        "TAKE_PROFIT_MARGIN": inherited_tp,
                        "TRAILING_MARGIN": inherited_trail,
                        "STATUS": 1,
                        "DCA_MODE": inherited_mode,
                        "RSI_MAX_ENTRY": inherited_rsi
                    }
                    save_active_pairs()
                    bot_locks[pair] = threading.RLock()
                    init_fresh_pair_data(
                        pair, budget_usd, buy_amount,
                        dca_mode=inherited_mode,
                        drop_threshold=inherited_drop,
                        take_profit_margin=inherited_tp,
                        max_loss_percent=inherited_loss,
                        fee_rate=inherited_fee,
                        trailing_margin=inherited_trail,
                        rsi_max_entry=inherited_rsi
                    )
                    t = threading.Thread(target=dca_loop, args=(pair,), daemon=True)
                    t.start()
                    return jsonify({
                        "success": True,
                        "message": f"Koin {replace_pair} (posisi kosong) langsung digantikan oleh {pair} (mewarisi config {inherited_mode.upper()} Drop:{inherited_drop*100}% TP:{inherited_tp*100}%)!"
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
                        "DROP_THRESHOLD": inherited_drop,
                        "MAX_LOSS_PERCENT": inherited_loss,
                        "FEE_RATE": inherited_fee,
                        "TAKE_PROFIT_MARGIN": inherited_tp,
                        "TRAILING_MARGIN": inherited_trail,
                        "STATUS": 1,
                        "DCA_MODE": inherited_mode,
                        "RSI_MAX_ENTRY": inherited_rsi
                    }
                    save_active_pairs()
                    bot_locks[pair] = threading.RLock()
                    init_fresh_pair_data(
                        pair, budget_usd, buy_amount,
                        dca_mode=inherited_mode,
                        drop_threshold=inherited_drop,
                        take_profit_margin=inherited_tp,
                        max_loss_percent=inherited_loss,
                        fee_rate=inherited_fee,
                        trailing_margin=inherited_trail,
                        rsi_max_entry=inherited_rsi
                    )
                    t = threading.Thread(target=dca_loop, args=(pair,), daemon=True)
                    t.start()
                    return jsonify({
                        "success": True,
                        "message": f"Koin {replace_pair} di-Force Sell dan langsung digantikan oleh {pair} (mewarisi config {inherited_mode.upper()} Drop:{inherited_drop*100}% TP:{inherited_tp*100}%)!"
                    })
                    
                # Kasus 3: Mode Aman (Sell Only) -> Set koin lama ke Sell Only dan pasang pending_replacement
                else:
                    target_data["config"]["status"] = 0
                    target_data["pending_replacement"] = {
                        "pair": pair,
                        "budget_usd": budget_usd,
                        "buy_amount": buy_amount,
                        "dca_mode": inherited_mode,
                        "drop_threshold": inherited_drop,
                        "take_profit_margin": inherited_tp,
                        "max_loss_percent": inherited_loss,
                        "rsi_max_entry": inherited_rsi,
                        "trailing_margin": inherited_trail
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
        
        init_fresh_pair_data(pair, budget_usd, buy_amount)
        
        # Start new DCA thread
        t = threading.Thread(target=dca_loop, args=(pair,), daemon=True)
        t.start()
        
        return jsonify({
            "success": True, 
            "message": f"Koin {pair} berhasil ditambahkan ke Live Trading! (Slot aktif: {len(PAIRS)}/{max_slots})"
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
                data['price_history'].append({"time": datetime.now().strftime("%H:%M"), "price": float(current_price)})
                
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
                data['lowest_price_time'] = int(time.time())
                save_data(pair, data)

            drop_percent = max(0.0, ((data['peak_price'] - current_price) * 100) / data['peak_price']) if data.get('peak_price', 0) > 0 else 0.0
            
            avg_price = get_avg_buy(pair)
            selisih = 0 if avg_price == 0 else round((current_price - avg_price) / avg_price * 100, 3)
            
            # Fitur: Autonomous Auto-Pilot Idle Rotator (Evaluasi di awal loop agar tidak terblokir continue)
            if len(data.get('buys', [])) == 0:
                if check_and_execute_idle_rotation(pair):
                    break
            
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
            needed_buy_amt = get_adaptive_buy_amount(pair)
            if not is_fund_exhausted(pair) \
                and data['budget_left'] >= min(needed_buy_amt, 1.10)\
                and free_usdt >= min_notional \
                and needed_buy_amt >= min_notional \
                and data["config"].get("status", 1) == 1 \
                and (avg_price == 0 or current_price <= avg_price * (1 - get_dynamic_drop_threshold(pair))):
                    
                if is_market_volatile(pair) and len(data['buys']) > 0:
                    if DEBUG:
                        print("[DBG] market volatile, pause buy")
                    time.sleep(60)
                    continue
                    
                # Rebound Confirmation Guard:
                # 1. Untuk First Buy: menahan jika market belum rebound +0.3% dari dasar
                # 2. Untuk Layer Lanjutan: menahan serok jika harga masih jatuh deras tanpa pantulan mikro (+0.3% dari lowest_price lokal)
                #    Dikecualikan jika harga sudah mengendap/sideways di dasar selama > 15 menit agar tidak ketinggalan kereta pantulan.
                if not is_rebounding(pair):
                    is_first = len(data['buys']) == 0
                    last_low_time = data.get('lowest_price_time', data.get('last_buy_time', 0))
                    if is_first or (time.time() - last_low_time < 900):
                        if DEBUG:
                            layer_label = "First Buy" if is_first else f"Layer {len(data['buys']) + 1}"
                            print(f"[DBG Rebound Guard] Menahan serok {layer_label} ({pair}): harga masih mencari dasar, belum rebound +0.3% dari low {fmt(data.get('lowest_price', 0))}")
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
                
                if time.time() - data.get('last_buy_time', 0) < cooldown_secs:
                    if DEBUG:
                        remaining = cooldown_secs - (time.time() - data.get('last_buy_time', 0))
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
                    
            elif avg_price and current_price >= calculate_dca_tp_target(avg_price, len(data['buys']), data["config"]["take_profit_margin"]):
                sell_all(pair)
            elif len(data.get('buys', [])) >= 2:
                # Fitur 3: Layer Recycling (Partial Scalping / Auto-Exit bagian dari Auto-Rescue)
                recycle_deep_layer(pair, current_price)
            
            # Fitur 4: Autonomous Auto-Rescue Engine (Penyelamat Mandiri Otomatis 4 Hari)
            if is_fund_exhausted(pair):
                check_and_execute_auto_rescue(pair, current_price)
            
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
