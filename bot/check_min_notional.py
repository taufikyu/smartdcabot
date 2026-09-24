import requests
import urllib3
import json

# Nonaktifkan warning SSL jika ada masalah sertifikat di server/VPS
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_min_notional_list():
    print("Mengambil data terbaru dari Binance...")
    try:
        response = requests.get('https://api.binance.com/api/v3/exchangeInfo', verify=False)
        data = response.json()
        
        results = []
        
        # Loop semua koin
        for s in data['symbols']:
            # Hanya ambil koin yang berpasangan dengan USDT dan sedang aktif ditradingkan
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
                for f in s['filters']:
                    if f['filterType'] in ('NOTIONAL', 'MIN_NOTIONAL'):
                        # Ambil nilai batas minimal beli
                        min_n = float(f.get('minNotional', f.get('notional', 9999)))
                        results.append({
                            "symbol": s['symbol'],
                            "min_notional": min_n
                        })
                        
        # Urutkan dari yang paling kecil ke paling besar
        sorted_results = sorted(results, key=lambda x: x['min_notional'])
        
        # Simpan ke file teks agar mudah dibaca
        output_file = "min_notional_list.txt"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("DAFTAR KOIN BINANCE (USDT) BERDASARKAN BATAS MINIMAL BELI TERKECIL\n")
            f.write("="*65 + "\n\n")
            
            # Print di layar
            print(f"\nBerhasil menemukan {len(sorted_results)} koin USDT aktif.")
            print("Top 50 Koin dengan Min Notional Terkecil:\n")
            
            for i, item in enumerate(sorted_results):
                line = f"{i+1}. {item['symbol']:<15} : {item['min_notional']} USDT\n"
                f.write(line)
                
                # Tampilkan 50 urutan pertama saja di layar terminal
                if i < 50:
                    print(line.strip())
                    
        print(f"\n[SUKSES] Daftar lengkap seluruh koin sudah disimpan ke dalam file: {output_file}")
        
    except Exception as e:
        print(f"Terjadi kesalahan saat menghubungi Binance: {e}")

if __name__ == "__main__":
    get_min_notional_list()
