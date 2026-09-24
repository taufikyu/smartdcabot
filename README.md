# 🤖 Smart DCA Bot (Multi-Coin Crypto Trading & Dashboard)

Smart DCA Bot adalah sistem trading cryptocurrency otomatis berbasis **Dollar-Cost Averaging (DCA)** dan **Smart Piramida** yang terintegrasi dengan **Binance Spot API** serta dilengkapi antarmuka web dashboard interaktif real-time.

---

## 🚀 Fitur Unggulan (Core Features)

1. **Smart Piramida & Dynamic Layer Allocation**:
   - Pembagian bobot modal bertingkat otomatis dengan pemadatan layer cerdas (*Adaptive Layer Reduction*) yang menjamin setiap order memenuhi syarat `MIN_NOTIONAL` Binance ($\ge \$1.10$).
2. **Full-Spectrum BTC Crash Guard**:
   - *Circuit Breaker* otomatis yang memantau pergerakan harga Bitcoin (1-Hour drop). Jika BTC dump $> 3.5\%$, bot otomatis membekukan seluruh pembelian (*First Buy* maupun *Layer DCA*) untuk menyelamatkan modal.
3. **Rebound Confirmation Guard**:
   - Mencegah serok pisau jatuh (*falling knife*) saat pasar crash dengan memastikan harga telah memantul minimal $+0.3\%$ dari titik terendah lokal (`lowest_price`), dilengkapi batas toleransi *timeout* 15 menit jika pasar sideways di dasar.
4. **Autonomous Auto-Rescue Engine**:
   - Penyelamat mandiri koin yang mengendap/tersangkut $\ge 4$ hari saat modal habis. Bot mengalokasikan kas darurat kecil ($\sim \$2.15$) dari dompet Binance, menyerok di titik dasar, dan otomatis mengembalikan settingan ke ukuran normal via *Auto-Revert*.
5. **Layer Recycling (Partial Scalping / Auto-Exit)**:
   - Merealisasikan profit cepat pada layer terbawah saat koin memegang $\ge 6$ layer dan pasar memantul $\ge +3.5\%$, memulihkan modal serok ke kas aktif tanpa harus menunggu tembus AVG keseluruhan.
6. **Auto-Pilot Coin Rotator & Auto-Compound**:
   - Rotasi koin potensial otomatis berdasarkan evaluasi simulasi 24 jam dan reinvestasi profit otomatis ke alokasi modal koin.
7. **Real-time Web Dashboard & Multi-Coin Management**:
   - Monitoring profit, layer aktif, visual chart, floating PnL, server clock, dan kontrol manual (Force Buy, Manual Recycle, Force Sell, Status Toggle) via Web Browser.

---

## 📂 Struktur Repositori

```
smartdcabot/
├── README.md               # Dokumentasi proyek
├── .gitignore              # Pengabaian file runtime, database, log & data dinamis
└── bot/                    # Source code trading bot & dashboard
    ├── multibot.py         # Engine utama multi-coin trading & Flask server
    ├── templates/
    │   ├── index.html      # Web dashboard UI interaktif
    │   └── login.html      # Halaman autentikasi login
    ├── requirements.txt    # Dependensi Python
    ├── .env.example        # Template konfigurasi environment (bebas API key)
    ├── active_pairs.example.json   # Template konfigurasi pair koin
    ├── capital_config.example.json # Template alokasi modal
    └── global_settings.example.json# Template pengaturan global (Auto-Pilot, BTC Guard, Auto-Rescue)
```

---

## 🛠️ Instalasi & Menjalankan Bot

### 1. Prasyarat
- Python 3.9 s/d 3.14
- Akun Binance dengan Spot Trading API Key & Secret aktif

### 2. Kloning Repositori
```bash
git clone https://github.com/taufikyu/smartdcabot.git
cd smartdcabot/bot
```

### 3. Pasang Dependensi
```bash
pip install -r requirements.txt
```

### 4. Konfigurasi Environment (`.env`)
Salin file `.env.example` menjadi `.env`, kemudian isi dengan API Key Binance Anda:
```bash
cp .env.example .env
```
Isi file `.env`:
```env
BINANCE_API_KEY=your_binance_api_key_here
BINANCE_API_SECRET=your_binance_api_secret_here

AUTH_USERNAME=admin
AUTH_PASSWORD=your_secure_password_here
```

### 5. Jalankan Bot & Dashboard
```bash
python multibot.py
```
Buka browser dan akses dashboard di: **`http://localhost:5000`**

---

## ⚠️ Disclaimer
Trading cryptocurrency memiliki risiko volatilitas pasar yang tinggi. Gunakan manajemen risiko dan alokasi modal yang bijak sesuai profil risiko Anda.
