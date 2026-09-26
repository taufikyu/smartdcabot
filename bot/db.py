import os
import sqlite3
import json
import time
import glob
import re
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot_trading.db")

def get_db_connection():
    """Mengembalikan koneksi SQLite dengan mode WAL untuk multi-threading aman."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn

def init_db():
    """Inisialisasi tabel-tabel SQLite jika belum ada."""
    conn = get_db_connection()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS pairs_config (
        pair TEXT PRIMARY KEY,
        is_active INTEGER DEFAULT 1,
        budget_usd REAL DEFAULT 15.0,
        buy_amount REAL DEFAULT 2.1,
        max_layer INTEGER DEFAULT 7,
        drop_threshold REAL DEFAULT 0.01,
        max_loss_percent REAL DEFAULT -15.0,
        fee_rate REAL DEFAULT 0.001,
        take_profit_margin REAL DEFAULT 0.008,
        trailing_margin REAL DEFAULT 0.001,
        rsi_max_entry REAL DEFAULT 52.0,
        status INTEGER DEFAULT 1,
        dca_mode TEXT DEFAULT 'smart',
        force_sell INTEGER DEFAULT 0,
        updated_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS pair_states (
        pair TEXT PRIMARY KEY,
        budget_left REAL,
        peak_price REAL,
        lowest_price REAL,
        peak_time INTEGER,
        lowest_price_time INTEGER,
        last_buy_time INTEGER,
        idle_since INTEGER,
        pending_replacement TEXT,
        pending_config TEXT,
        revert_config_after_sell TEXT,
        config_json TEXT,
        updated_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS open_layers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair TEXT NOT NULL,
        layer_idx INTEGER NOT NULL,
        price REAL NOT NULL,
        qty REAL NOT NULL,
        cost_usdt REAL NOT NULL,
        buy_time INTEGER,
        UNIQUE(pair, layer_idx)
    );
    CREATE INDEX IF NOT EXISTS idx_open_layers_pair ON open_layers(pair);

    CREATE TABLE IF NOT EXISTS trade_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        trade_time TEXT NOT NULL,
        pair TEXT NOT NULL,
        action TEXT NOT NULL,
        price REAL NOT NULL,
        qty REAL NOT NULL,
        profit REAL NOT NULL,
        message TEXT,
        created_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_trade_history_pair ON trade_history(pair);
    CREATE INDEX IF NOT EXISTS idx_trade_history_date ON trade_history(trade_date);
    CREATE INDEX IF NOT EXISTS idx_trade_history_pair_time ON trade_history(pair, trade_date, trade_time);

    CREATE TABLE IF NOT EXISTS global_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair TEXT NOT NULL,
        time_str TEXT NOT NULL,
        price REAL NOT NULL,
        timestamp INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_price_history_pair ON price_history(pair);

    CREATE TABLE IF NOT EXISTS capital_tracker (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        injected_capital REAL DEFAULT 55.32,
        updated_at TEXT
    );
    """)
    conn.commit()
    conn.close()
    auto_migrate_from_files_if_needed()
    db_cleanup_duplicate_trades()

def db_cleanup_duplicate_trades():
    """
    Membersihkan dan merapikan trade_history di database SQLite:
    1. Memastikan aksi event/non-trade (COMPOUND, CONFIG_REVERTED, CONFIG_APPLIED, dsb) memiliki profit = 0.0
    2. Menghapus log SELL generik jika sudah ada log PARTIAL_TP / MANUAL_RECYCLE pada detik & harga yang sama
    3. Menghapus baris duplikat identik
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # 1. Reset profit untuk aksi internal non-realisasi
        c.execute("""
        UPDATE trade_history 
        SET profit = 0.0 
        WHERE action IN ('COMPOUND', 'CONFIG_REVERTED', 'CONFIG_APPLIED', 'AUTO_RESCUE', 
                         'AUTOPILOT_START', 'AUTOPILOT_TRIGGER', 'SWAP', 'PREBUY', 'AUTO_REVERT')
          AND profit != 0.0
        """)
        
        # 2. Hapus SELL duplikat dari API Binance jika PARTIAL_TP / MANUAL_RECYCLE sudah tercatat
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
        
        # 3. Hapus duplikasi baris identik (keep min id)
        c.execute("""
        DELETE FROM trade_history 
        WHERE id NOT IN (
            SELECT MIN(id) 
            FROM trade_history 
            GROUP BY trade_date, trade_time, pair, action, price, qty
        )
        """)
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error in db_cleanup_duplicate_trades: {e}")

def auto_migrate_from_files_if_needed():
    """
    Jika database baru dibuat / kosong atau belum memiliki BUY actions,
    otomatis mengimpor seluruh data dari active_pairs.json, global_settings.json,
    capital_config.json, bot_*.json, trade_log_*.txt, atau bot.tar.gz / legacy backup.
    """
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. Migrasi Global Settings jika belum ada
    c.execute("SELECT COUNT(*) FROM global_settings")
    if c.fetchone()[0] == 0:
        g_file = os.path.join(BASE_DIR, "global_settings.json")
        if os.path.exists(g_file):
            try:
                with open(g_file, "r", encoding="utf-8") as f:
                    g_data = json.load(f)
                    for k, v in g_data.items():
                        c.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
            except Exception:
                pass

    # 2. Migrasi Capital Config jika belum ada
    c.execute("SELECT COUNT(*) FROM capital_tracker")
    if c.fetchone()[0] == 0:
        cap_file = os.path.join(BASE_DIR, "capital_config.json")
        if os.path.exists(cap_file):
            try:
                with open(cap_file, "r", encoding="utf-8") as f:
                    cap_data = json.load(f)
                    inj = float(cap_data.get("injected_capital", 55.32))
                    upd = str(cap_data.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    c.execute("INSERT INTO capital_tracker (injected_capital, updated_at) VALUES (?, ?)", (inj, upd))
            except Exception:
                pass

    # 3. Migrasi Active Pairs jika belum ada
    c.execute("SELECT COUNT(*) FROM pairs_config")
    if c.fetchone()[0] == 0:
        act_file = os.path.join(BASE_DIR, "active_pairs.json")
        if os.path.exists(act_file):
            try:
                with open(act_file, "r", encoding="utf-8") as f:
                    act_data = json.load(f)
                    pairs_active = set(act_data.get("PAIRS", []))
                    pairs_cfg = act_data.get("PAIRS_CONFIG", {})
                    for p, cfg in pairs_cfg.items():
                        is_active = 1 if p in pairs_active else 0
                        c.execute("""
                        INSERT OR REPLACE INTO pairs_config 
                        (pair, is_active, budget_usd, buy_amount, max_layer, drop_threshold, max_loss_percent, fee_rate, take_profit_margin, trailing_margin, rsi_max_entry, status, dca_mode, force_sell, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            p, is_active,
                            float(cfg.get("BUDGET_USD", cfg.get("budget_usd", 15.0))),
                            float(cfg.get("BUY_AMOUNT", cfg.get("buy_amount", 2.1))),
                            int(cfg.get("MAX_LAYER", cfg.get("max_layer", 7))),
                            float(cfg.get("DROP_THRESHOLD", cfg.get("drop_threshold", 0.01))),
                            float(cfg.get("MAX_LOSS_PERCENT", cfg.get("max_loss_percent", -15.0))),
                            float(cfg.get("FEE_RATE", cfg.get("fee_rate", 0.001))),
                            float(cfg.get("TAKE_PROFIT_MARGIN", cfg.get("take_profit_margin", 0.008))),
                            float(cfg.get("TRAILING_MARGIN", cfg.get("trailing_margin", 0.001))),
                            float(cfg.get("RSI_MAX_ENTRY", cfg.get("rsi_max_entry", 48.0))),
                            int(cfg.get("STATUS", cfg.get("status", 1))),
                            str(cfg.get("DCA_MODE", cfg.get("dca_mode", "smart"))).lower(),
                            1 if cfg.get("FORCE_SELL", cfg.get("force_sell", False)) else 0,
                            int(time.time())
                        ))
            except Exception:
                pass

    # 4. Migrasi Bot Pair States & Open Layers jika kosong
    c.execute("SELECT COUNT(*) FROM pair_states")
    if c.fetchone()[0] == 0:
        bot_json_files = glob.glob(os.path.join(BASE_DIR, "bot_*.json"))
        for bfile in bot_json_files:
            if bfile.endswith(".bak"):
                continue
            p_name = os.path.basename(bfile).replace("bot_", "").replace(".json", "")
            if any(x in p_name.lower() for x in ['rescuepair', 'testusdt', 'compusdt', 'tstusdt']):
                continue
            try:
                with open(bfile, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
                    if not content:
                        continue
                    b_data = json.loads(content)
                    b_left = float(b_data.get("budget_left", 0.0))
                    p_peak = float(b_data.get("peak_price", 0.0))
                    p_low = float(b_data.get("lowest_price", 0.0))
                    p_time = int(b_data.get("peak_time", time.time()))
                    l_time = int(b_data.get("lowest_price_time", time.time()))
                    last_buy = int(b_data.get("last_buy_time", 0))
                    idle_s = int(b_data.get("idle_since", 0))
                    p_repl = json.dumps(b_data.get("pending_replacement"))
                    p_conf = json.dumps(b_data.get("pending_config"))
                    r_conf = json.dumps(b_data.get("revert_config_after_sell"))
                    c_json = json.dumps(b_data.get("config", {}))
                    
                    c.execute("""
                    INSERT OR REPLACE INTO pair_states 
                    (pair, budget_left, peak_price, lowest_price, peak_time, lowest_price_time, last_buy_time, idle_since, pending_replacement, pending_config, revert_config_after_sell, config_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (p_name, b_left, p_peak, p_low, p_time, l_time, last_buy, idle_s, p_repl, p_conf, r_conf, c_json, int(time.time())))
                    
                    buys = b_data.get("buys", [])
                    c.execute("DELETE FROM open_layers WHERE pair = ?", (p_name,))
                    for idx, b in enumerate(buys, start=1):
                        pr = float(b.get("price", 0.0))
                        qt = float(b.get("qty", 0.0))
                        bt = int(b.get("time", time.time()))
                        c.execute("""
                        INSERT INTO open_layers (pair, layer_idx, price, qty, cost_usdt, buy_time)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """, (p_name, idx, pr, qt, pr * qt, bt))
            except Exception:
                pass

    # 5. Migrasi Trade Logs (Termasuk BUY dan SELL lengkap)
    c.execute("SELECT COUNT(*) FROM trade_history WHERE action = 'BUY'")
    buy_count = c.fetchone()[0]
    
    if buy_count == 0:
        action_pattern = re.compile(
            r'\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\]\s+'
            r'([A-Za-z0-9_ ]+?)\s+'
            r'\|\s+Price:\s+([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+'
            r'\|\s+Qty:\s+([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+'
            r'\|\s+Profit:\s+([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)'
            r'(?:\s+\|\s*(.*))?'
        )

        def _import_log_lines(lines, p_from_file):
            cnt = 0
            for line in lines:
                m = action_pattern.search(line)
                if m:
                    d, t, act, pr, qt, prof = m.groups()[:6]
                    p_val = float(prof)
                    msg = (m.group(7) or '').strip() if m.lastindex >= 7 else ''
                    key = (d, t, p_from_file, act.strip(), round(float(pr), 8), round(float(qt), 8))
                    if key not in seen_tx:
                        seen_tx.add(key)
                        c.execute("""
                        INSERT INTO trade_history (trade_date, trade_time, pair, action, price, qty, profit, message, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (d, t, p_from_file, act.strip(), float(pr), float(qt), p_val, msg, int(time.time())))
                        cnt += 1
            return cnt

        seen_tx = set()
        c.execute("SELECT trade_date, trade_time, pair, action, price, qty FROM trade_history")
        for r in c.fetchall():
            seen_tx.add((r[0], r[1], r[2], str(r[3]).strip(), round(float(r[4]), 8), round(float(r[5]), 8)))

        # Cari file trade_log*.txt langsung
        log_files = [f for f in glob.glob(os.path.join(BASE_DIR, "trade_log_*.txt")) 
                     if not any(x in os.path.basename(f).lower() for x in ['backup', '(1)', '(2)', 'copy', 'rescuepair', 'recycle_test', 'testusdt', 'compusdt', 'tstusdt', 'testpair'])]
        for lfile in log_files:
            p_from_file = os.path.basename(lfile).replace("trade_log_", "").replace(".txt", "")
            if p_from_file == "trade_log" or not p_from_file:
                p_from_file = "DOGEUSDT"
            try:
                with open(lfile, "r", encoding="utf-8", errors="ignore") as f:
                    _import_log_lines(f.readlines(), p_from_file)
            except Exception:
                pass

        # Cari juga di dalam bot.tar.gz atau legacy_files_backup.tar.gz jika file txt sudah terhapus
        tar_candidates = [
            os.path.join(BASE_DIR, "legacy_files_backup.tar.gz"),
            os.path.join(BASE_DIR, "bot.tar.gz"),
            os.path.join(os.path.dirname(BASE_DIR), "bot.tar.gz")
        ]
        import tarfile
        for tpath in tar_candidates:
            if os.path.exists(tpath):
                try:
                    with tarfile.open(tpath, "r:*") as tar:
                        for m in tar.getmembers():
                            if "trade_log" in m.name and m.name.endswith(".txt"):
                                p_from_file = os.path.basename(m.name).replace("trade_log_", "").replace(".txt", "")
                                if p_from_file == "trade_log" or not p_from_file:
                                    p_from_file = "DOGEUSDT"
                                if any(x in p_from_file.lower() for x in ['backup', '(1)', '(2)', 'copy', 'rescuepair', 'recycle_test', 'testusdt', 'compusdt', 'tstusdt', 'testpair']):
                                    continue
                                f = tar.extractfile(m)
                                if f:
                                    lines = [l.decode('utf-8', 'ignore').strip() for l in f.readlines()]
                                    _import_log_lines(lines, p_from_file)
                except Exception:
                    pass

    conn.commit()
    conn.close()

# ============ DB CRUD HELPERS ============

def db_load_active_pairs():
    """Mengambil list PAIRS dan dictionary PAIRS_CONFIG dari SQLite."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM pairs_config")
    rows = c.fetchall()
    conn.close()
    
    pairs_list = []
    pairs_config = {}
    for r in rows:
        p = r["pair"]
        if r["is_active"] == 1:
            pairs_list.append(p)
        pairs_config[p] = {
            "BUDGET_USD": r["budget_usd"],
            "BUY_AMOUNT": r["buy_amount"],
            "MAX_LAYER": r["max_layer"],
            "DROP_THRESHOLD": r["drop_threshold"],
            "MAX_LOSS_PERCENT": r["max_loss_percent"],
            "FEE_RATE": r["fee_rate"],
            "TAKE_PROFIT_MARGIN": r["take_profit_margin"],
            "TRAILING_MARGIN": r["trailing_margin"],
            "RSI_MAX_ENTRY": r["rsi_max_entry"],
            "STATUS": r["status"],
            "DCA_MODE": r["dca_mode"],
            "FORCE_SELL": bool(r["force_sell"])
        }
    return pairs_list, pairs_config

def db_save_active_pairs(pairs_list, pairs_config):
    """Menyimpan list PAIRS dan PAIRS_CONFIG ke SQLite."""
    conn = get_db_connection()
    c = conn.cursor()
    for p, cfg in pairs_config.items():
        is_act = 1 if p in pairs_list else 0
        c.execute("""
        INSERT OR REPLACE INTO pairs_config 
        (pair, is_active, budget_usd, buy_amount, max_layer, drop_threshold, max_loss_percent, fee_rate, take_profit_margin, trailing_margin, rsi_max_entry, status, dca_mode, force_sell, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            p, is_act,
            float(cfg.get("BUDGET_USD", cfg.get("budget_usd", 15.0))),
            float(cfg.get("BUY_AMOUNT", cfg.get("buy_amount", 2.1))),
            int(cfg.get("MAX_LAYER", cfg.get("max_layer", 7))),
            float(cfg.get("DROP_THRESHOLD", cfg.get("drop_threshold", 0.01))),
            float(cfg.get("MAX_LOSS_PERCENT", cfg.get("max_loss_percent", -15.0))),
            float(cfg.get("FEE_RATE", cfg.get("fee_rate", 0.001))),
            float(cfg.get("TAKE_PROFIT_MARGIN", cfg.get("take_profit_margin", 0.008))),
            float(cfg.get("TRAILING_MARGIN", cfg.get("trailing_margin", 0.001))),
            float(cfg.get("RSI_MAX_ENTRY", cfg.get("rsi_max_entry", 48.0))),
            int(cfg.get("STATUS", cfg.get("status", 1))),
            str(cfg.get("DCA_MODE", cfg.get("dca_mode", "smart"))).lower(),
            1 if cfg.get("FORCE_SELL", cfg.get("force_sell", False)) else 0,
            int(time.time())
        ))
    conn.commit()
    conn.close()

def db_load_global_settings():
    """Mengambil global settings dari SQLite."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT key, value FROM global_settings")
    rows = c.fetchall()
    conn.close()
    
    settings = {
        "auto_pilot": True,
        "auto_pilot_idle_rotation": True,
        "auto_pilot_idle_hours": 3.0,
        "idle_cooldown_pairs": {},
        "auto_compound": False,
        "btc_guard": True,
        "auto_rescue": True,
        "auto_rescue_days": 4,
        "auto_rescue_tp": 0,
        "max_slots": 3,
        "locked_pairs": []
    }
    for r in rows:
        try:
            settings[r["key"]] = json.loads(r["value"])
        except Exception:
            settings[r["key"]] = r["value"]
    return settings

def db_save_global_settings(settings_dict):
    """Menyimpan global settings ke SQLite."""
    conn = get_db_connection()
    c = conn.cursor()
    for k, v in settings_dict.items():
        c.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
    conn.commit()
    conn.close()

def db_load_capital_config():
    """Mengambil capital tracker dari SQLite."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT injected_capital, updated_at FROM capital_tracker ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return {"injected_capital": float(row["injected_capital"]), "updated_at": str(row["updated_at"])}
    return {"injected_capital": 55.32, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

def db_save_capital_config(injected_capital):
    """Menyimpan capital tracker ke SQLite."""
    conn = get_db_connection()
    c = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO capital_tracker (injected_capital, updated_at) VALUES (?, ?)", (float(injected_capital), now_str))
    conn.commit()
    conn.close()
    return {"injected_capital": float(injected_capital), "updated_at": now_str}

def db_load_pair_state(pair, default_config=None):
    """Mengambil state lengkap (buys, peak, low, config) dari SQLite."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM pair_states WHERE pair = ?", (pair,))
    row = c.fetchone()
    
    c.execute("SELECT price, qty, buy_time FROM open_layers WHERE pair = ? ORDER BY layer_idx ASC", (pair,))
    layer_rows = c.fetchall()
    
    c.execute("SELECT time_str, price FROM price_history WHERE pair = ? ORDER BY id ASC LIMIT 48", (pair,))
    hist_rows = c.fetchall()
    conn.close()
    
    buys = [{"price": lr["price"], "qty": lr["qty"], "time": lr["buy_time"]} for lr in layer_rows]
    price_hist = [{"time": hr["time_str"], "price": hr["price"]} for hr in hist_rows]
    
    if not row:
        cfg = default_config or {
            "budget_usd": 15.0, "buy_amount": 2.1, "max_layer": 7,
            "drop_threshold": 0.01, "max_loss_percent": -15.0, "fee_rate": 0.001,
            "take_profit_margin": 0.008, "trailing_margin": 0.001, "status": 1,
            "dca_mode": "smart", "rsi_max_entry": 48.0, "force_sell": False
        }
        return {
            "buys": buys,
            "price_history": price_hist,
            "budget_left": float(cfg.get("budget_usd", 15.0)),
            "peak_price": 0.0,
            "lowest_price": 0.0,
            "peak_time": int(time.time()),
            "lowest_price_time": int(time.time()),
            "last_buy_time": 0,
            "idle_since": int(time.time()),
            "pending_replacement": None,
            "pending_config": None,
            "revert_config_after_sell": None,
            "config": cfg
        }
        
    def _parse_json(val, default):
        if not val: return default
        try: return json.loads(val)
        except Exception: return default

    config_dict = _parse_json(row["config_json"], default_config or {})
    return {
        "buys": buys,
        "price_history": price_hist,
        "budget_left": float(row["budget_left"] or 0.0),
        "peak_price": float(row["peak_price"] or 0.0),
        "lowest_price": float(row["lowest_price"] or 0.0),
        "peak_time": int(row["peak_time"] or time.time()),
        "lowest_price_time": int(row["lowest_price_time"] or time.time()),
        "last_buy_time": int(row["last_buy_time"] or 0),
        "idle_since": int(row["idle_since"] or 0),
        "pending_replacement": _parse_json(row["pending_replacement"], None),
        "pending_config": _parse_json(row["pending_config"], None),
        "revert_config_after_sell": _parse_json(row["revert_config_after_sell"], None),
        "config": config_dict
    }

def db_save_pair_state(pair, data):
    """Menyimpan state pair & open layers ke SQLite."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
    INSERT OR REPLACE INTO pair_states 
    (pair, budget_left, peak_price, lowest_price, peak_time, lowest_price_time, last_buy_time, idle_since, pending_replacement, pending_config, revert_config_after_sell, config_json, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        pair,
        float(data.get("budget_left", 0.0)),
        float(data.get("peak_price", 0.0)),
        float(data.get("lowest_price", 0.0)),
        int(data.get("peak_time", time.time())),
        int(data.get("lowest_price_time", time.time())),
        int(data.get("last_buy_time", 0)),
        int(data.get("idle_since", 0)),
        json.dumps(data.get("pending_replacement")),
        json.dumps(data.get("pending_config")),
        json.dumps(data.get("revert_config_after_sell")),
        json.dumps(data.get("config", {})),
        int(time.time())
    ))
    
    # Simpan buys
    buys = data.get("buys", [])
    c.execute("DELETE FROM open_layers WHERE pair = ?", (pair,))
    for idx, b in enumerate(buys, start=1):
        pr = float(b.get("price", 0.0))
        qt = float(b.get("qty", 0.0))
        bt = int(b.get("time", time.time()))
        c.execute("""
        INSERT INTO open_layers (pair, layer_idx, price, qty, cost_usdt, buy_time)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (pair, idx, pr, qt, pr * qt, bt))
        
    conn.commit()
    conn.close()

def db_log_trade_action(pair, action, price=0.0, qty=0.0, profit=0.0, message=""):
    """Menyimpan aksi trading (BUY, SELL, CUT_LOSS) ke SQLite trade_history."""
    if isinstance(profit, str) and not message:
        message = profit
        profit = 0.0
    try:
        profit_val = float(profit or 0.0)
    except (ValueError, TypeError):
        profit_val = 0.0
    now = datetime.now()
    d_str = now.strftime("%Y-%m-%d")
    t_str = now.strftime("%H:%M:%S")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
    INSERT INTO trade_history (trade_date, trade_time, pair, action, price, qty, profit, message, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (d_str, t_str, pair, action, float(price or 0.0), float(qty or 0.0), profit_val, str(message or ""), int(time.time())))
    conn.commit()
    conn.close()

def db_get_pair_logs_formatted(pair, limit=20):
    """Mengambil riwayat log teks formatted langsung dari SQLite trade_history."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
    SELECT trade_date, trade_time, action, price, qty, profit, message 
    FROM trade_history 
    WHERE pair = ? 
    ORDER BY trade_date DESC, trade_time DESC, id DESC 
    LIMIT ?
    """, (pair, limit))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Belum ada riwayat transaksi."
    lines = []
    for r in rows:
        msg = f" | {r['message']}" if r['message'] else ""
        lines.append(f"[{r['trade_date']} {r['trade_time']}] {r['action']} | Price: {r['price']} | Qty: {r['qty']} | Profit: {r['profit']}{msg}")
    return "\n".join(lines)

def db_log_price(pair, price):
    """Menyimpan history harga ke SQLite (auto-trim ke 48 entri terbaru per pair)."""
    now = datetime.now()
    t_str = now.strftime("%H:%M")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO price_history (pair, time_str, price, timestamp) VALUES (?, ?, ?, ?)",
              (pair, t_str, float(price), int(time.time())))
    # Trim to 48
    c.execute("""
    DELETE FROM price_history 
    WHERE id NOT IN (
        SELECT id FROM price_history WHERE pair = ? ORDER BY id DESC LIMIT 48
    ) AND pair = ?
    """, (pair, pair))
    conn.commit()
    conn.close()

def db_get_analytics_data(injected_capital, free_usdt, total_usdt):
    """Mengambil rekap analitik profit (All-Time, Daily, Monthly) secepat kilat via SQL query."""
    conn = get_db_connection()
    c = conn.cursor()
    
    REALIZED_FILTER = """
    action IN ('SELL', 'PARTIAL_TP', 'MANUAL_RECYCLE', 'FORCED_SELL_CUTLOSS', 'FORCE SELL', 'CUT LOSS', 'TAKE PROFIT')
    """
    
    # 1. Total Realized Profit & Trades
    c.execute(f"SELECT COUNT(*), COALESCE(SUM(profit), 0.0) FROM trade_history WHERE {REALIZED_FILTER}")
    row_tot = c.fetchone()
    total_trades = row_tot[0]
    total_realized_profit = round(row_tot[1], 4)
    
    # 2. Today Profit
    today_str = datetime.now().strftime("%Y-%m-%d")
    c.execute(f"SELECT COALESCE(SUM(profit), 0.0) FROM trade_history WHERE trade_date = ? AND ({REALIZED_FILTER})", (today_str,))
    today_profit = round(c.fetchone()[0], 4)
    
    # 3. This Month Profit
    month_str = datetime.now().strftime("%Y-%m")
    c.execute(f"SELECT COALESCE(SUM(profit), 0.0) FROM trade_history WHERE trade_date LIKE ? AND ({REALIZED_FILTER})", (f"{month_str}%",))
    month_profit = round(c.fetchone()[0], 4)
    
    # 4. Monthly Breakdown
    MONTH_NAMES_ID = {
        "01": "Januari", "02": "Februari", "03": "Maret", "04": "April",
        "05": "Mei", "06": "Juni", "07": "Juli", "08": "Agustus",
        "09": "September", "10": "Oktober", "11": "November", "12": "Desember"
    }
    c.execute(f"""
    SELECT substr(trade_date, 1, 7) as ym, COUNT(*), SUM(profit), GROUP_CONCAT(DISTINCT pair)
    FROM trade_history
    WHERE {REALIZED_FILTER}
    GROUP BY ym
    ORDER BY ym DESC
    """)
    m_rows = c.fetchall()
    monthly_list = []
    for r in m_rows:
        ym = r[0]
        cnt = r[1]
        prof = round(r[2], 4)
        pairs_set = [p.strip() for p in (r[3] or "").split(",") if p.strip()]
        parts = ym.split("-")
        lbl = f"{MONTH_NAMES_ID.get(parts[1], parts[1])} {parts[0]}" if len(parts) == 2 else ym
        monthly_list.append({
            "month": ym,
            "label": lbl,
            "profit": prof,
            "trades": cnt,
            "pairs": pairs_set,
            "avg_per_trade": round(prof / cnt, 4) if cnt > 0 else 0.0
        })
        
    # 5. Daily Breakdown (30 hari terakhir)
    c.execute(f"""
    SELECT trade_date, COUNT(*), SUM(profit), GROUP_CONCAT(DISTINCT pair)
    FROM trade_history
    WHERE {REALIZED_FILTER}
    GROUP BY trade_date
    ORDER BY trade_date DESC
    LIMIT 30
    """)
    d_rows = c.fetchall()
    daily_list = []
    for r in d_rows:
        dt = r[0]
        cnt = r[1]
        prof = round(r[2], 4)
        pairs_set = [p.strip() for p in (r[3] or "").split(",") if p.strip()]
        daily_list.append({
            "date": dt,
            "profit": prof,
            "trades": cnt,
            "pairs": pairs_set
        })
        
    # 6. Recent Trades (Semua transaksi termasuk BUY & SELL untuk log dan inspeksi)
    c.execute("""
    SELECT trade_date, trade_time, pair, action, price, qty, profit, message
    FROM trade_history
    ORDER BY trade_date DESC, trade_time DESC, id DESC
    LIMIT 50
    """)
    t_rows = c.fetchall()
    recent_trades = []
    for r in t_rows:
        recent_trades.append({
            "date": r["trade_date"],
            "time": r["trade_time"],
            "datetime": f"{r['trade_date']} {r['trade_time']}",
            "pair": r["pair"],
            "action": r["action"],
            "price": r["price"],
            "qty": r["qty"],
            "profit": round(r["profit"], 6),
            "message": r["message"] or ""
        })
        
    # 7. Recent Sells (Khusus transaksi realisasi sell)
    c.execute(f"""
    SELECT trade_date, trade_time, pair, action, price, qty, profit, message
    FROM trade_history
    WHERE {REALIZED_FILTER}
    ORDER BY trade_date DESC, trade_time DESC, id DESC
    LIMIT 50
    """)
    s_rows = c.fetchall()
    recent_sells = []
    for r in s_rows:
        recent_sells.append({
            "date": r["trade_date"],
            "time": r["trade_time"],
            "datetime": f"{r['trade_date']} {r['trade_time']}",
            "pair": r["pair"],
            "action": r["action"],
            "price": r["price"],
            "qty": r["qty"],
            "profit": round(r["profit"], 6),
            "message": r["message"] or ""
        })

    conn.close()
    
    # Capital tracker calculations
    net_growth = total_usdt - injected_capital
    net_growth_pct = (net_growth / injected_capital * 100.0) if injected_capital > 0 else 0.0
    
    return {
        "total_realized_profit": total_realized_profit,
        "total_trades": total_trades,
        "today_profit": today_profit,
        "month_profit": month_profit,
        "win_rate": 100.0 if total_trades > 0 else 0.0,
        "capital_tracker": {
            "injected_capital": round(injected_capital, 4),
            "current_equity": round(total_usdt, 4),
            "free_usdt": round(free_usdt, 4),
            "net_growth": round(net_growth, 4),
            "net_growth_pct": round(net_growth_pct, 2)
        },
        "daily_breakdown": daily_list,
        "monthly_breakdown": monthly_list,
        "recent_trades": recent_trades,
        "recent_sells": recent_sells
    }
