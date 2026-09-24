import requests
import urllib3
urllib3.disable_warnings()

resp = requests.get('https://api.binance.com/api/v3/exchangeInfo', verify=False)
data = resp.json()

results = []
for s in data['symbols']:
    if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
        for f in s['filters']:
            if f['filterType'] == 'NOTIONAL' or f['filterType'] == 'MIN_NOTIONAL':
                min_n = float(f.get('minNotional', f.get('notional', 999)))
                if min_n <= 1.5:
                    results.append(f"{s['symbol']} ({min_n} USDT)")

print("\n".join(results[:50]))
