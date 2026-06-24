import yfinance as yf
import pandas as pd
import pandas_ta as ta
from anthropic import Anthropic
from newsapi import NewsApiClient
from newsapi.newsapi_exception import NewsAPIException
from curl_cffi import requests as curl_requests
import requests
import time
import io
import json
import os
import random
from datetime import datetime
from dotenv import load_dotenv

# Carica le variabili dal file .env nel sistema
load_dotenv("/home/gianrico/.trading_env")

# --- CONFIGURAZIONI ---
ANTHROPIC_KEY = os.getenv("ANTHROPIC_KEY")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RAW_DATA_CACHE = "market_data_raw.pkl"
MIN_AVG_VOLUME = 10_000_000  # Almeno 10 milioni di azioni scambiate al giorno
MIN_DOLLAR_VOLUME = 5_000_000 # Almeno 5 milioni di dollari scambiati al giorno

# --- FILTRI FONDAMENTALI (VALUE) ---
MIN_MARKET_CAP = 1_000_000_000   # $1B 
MAX_PE_RATIO = 25                # P/E massimo per value
MAX_PEG_RATIO = 1.5              # PEG < 1.5 = sottovalutato
MIN_PROFIT_MARGIN = 0.05         # Margine netto > 5%
MIN_FREE_CASH_FLOW = -10_000_000           # FCF negativo ma non troppo

# --- AVOID RATE LIMITING  ---
RL_DELAY = (5, 10)       # Delay random tra chiamate yf

# Configurazione Sessione per bypassare i blocchi
session = curl_requests.Session(impersonate="chrome")

client_claude = Anthropic(api_key=ANTHROPIC_KEY)
news_api = NewsApiClient(api_key=NEWS_API_KEY)

def get_ftse_mib_tickers():
    # URL di una tabella aggiornata (es. da it.investing.com o simili)
    # Per semplicità e precisione, ecco i principali 40 già formattati:
    TICKERS_IT = [
    {"symbol": "STMPA",  "exchange": "BVME", "currency": "EUR"},
    {"symbol": "STLAP",  "exchange": "BVME", "currency": "EUR"},
    {"symbol": "LDO", "exchange": "BVME", "currency": "EUR"},
    {"symbol": "FBK", "exchange": "BVME", "currency": "EUR"},
    {"symbol": "UCG", "exchange": "BVME", "currency": "EUR"},
]
    return TICKERS_IT

def get_us_tickers():
    print("📥 Recupero ticker dalle fonti ufficiali...")
    all_tickers = set()

    # 1. S&P 500 tramite iShares (BlackRock) - Molto stabile
    try:
        url_sp500 = "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund"
        s = requests.get(url_sp500).content
        df_sp = pd.read_csv(io.StringIO(s.decode('utf-8')), skiprows=9)
        tickers_sp = df_sp['Ticker'].dropna().tolist()
        all_tickers.update(tickers_sp)
    except Exception as e:
        print(f"⚠️ Errore S&P 500: {e}")

    # 2. NASDAQ & NYSE tramite NasdaqTrader (File TXT ufficiale)
    try:
        url_nasdaq = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
        df_nasdaq = pd.read_csv(url_nasdaq, sep="|")
        tickers_nasdaq = df_nasdaq['Symbol'].dropna().tolist()
        all_tickers.update(tickers_nasdaq)
    except Exception as e:
        print(f"⚠️ Errore Nasdaq: {e}")

    # Pulizia: rimuoviamo ticker non validi (es. 'CASH_USD' o quelli con descrizioni)
    # yfinance vuole il trattino '-' al posto del punto '.' (es. BRK-B)
    clean_list = [str(t).replace('.', '-') for t in all_tickers if len(str(t)) <= 5 and str(t).isalpha()]
    
    return list(set(clean_list))


def get_market_data(ticker_list, batch_size=100):
    """Scarica i dati in blocchi con pause per evitare il rate limiting"""
    
    # 1. Controllo Cache (come richiesto)
    today = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(RAW_DATA_CACHE):
        file_time = datetime.fromtimestamp(os.path.getmtime(RAW_DATA_CACHE)).strftime("%Y-%m-%d")
        if file_time == today:
            print("📦 Cache valida trovata. Caricamento in corso...")
            return pd.read_pickle(RAW_DATA_CACHE)

    print(f"🚀 Inizio download di {len(ticker_list)} titoli in batch da {batch_size}...")
    all_data = []

    # 2. Suddivisione in Batch
    for i in range(0, len(ticker_list), batch_size):
        batch = ticker_list[i:i + batch_size]
        print(f"📥 Scaricando blocco {i//batch_size + 1}... ({len(batch)} ticker)")
        
        try:
            # Download del batch corrente
            batch_data = yf.download(
                batch, 
                period="1y", 
                interval="1d", 
                group_by='ticker', 
                session=session, 
                threads=True, 
                progress=True
            )
            all_data.append(batch_data)
            
            # 3. Pausa strategica tra i batch (non l'ultimo)
            if i + batch_size < len(ticker_list):
                wait_time = random.uniform(*RL_DELAY)
                print(f"😴 Pausa di {wait_time:.2f} secondi per evitare blocchi...")
                time.sleep(wait_time)
                
        except Exception as e:
            print(f"⚠️ Errore durante il download del batch: {e}")
            continue

    # 4. Unione di tutti i DataFrame
    if not all_data:
        return pd.DataFrame()

    final_df = pd.concat(all_data, axis=1)
    
    # Salvataggio in cache
    final_df.to_pickle(RAW_DATA_CACHE)
    print(f"✅ Download completato e salvato in {RAW_DATA_CACHE}")
    return final_df

def analyze_and_filter(data, ticker_list, rsi_limit=25):
    """
    Analisi in 2 fasi:
    - Fase 1: Filtro tecnico veloce (usa solo dati OHLCV)
    - Fase 2: Filtro fondamentale con rate limiting (usa yf.Ticker().info)
    """
    
    # ===== FASE 1: FILTRO TECNICO (veloce, usa solo 'data') =====
    print("📊 Fase 1: Analisi tecnica in corso...")
    technical_candidates = []
    
    for ticker in ticker_list:
        try:
            df = data[ticker].dropna()
            if len(df) < 200: continue
            
            # --- DATI TECNICI ---
            last_price = df['Close'].iloc[-1]
            last_open = df['Open'].iloc[-1]
            
            # RSI e SMA 20
            rsi_series = ta.rsi(df['Close'], length=14)
            sma20 = ta.sma(df['Close'], length=20)
            
            # ADX per forza del trend
            adx_df = ta.adx(df['High'], df['Low'], df['Close'], length=14)
            
            last_rsi = rsi_series.iloc[-1]
            last_adx = adx_df['ADX_14'].iloc[-1]
            last_sma20 = sma20.iloc[-1]
            
            # --- ANALISI VOLUMI ---
            current_volume = df['Volume'].iloc[-1]
            avg_volume_20d = df['Volume'].tail(20).mean()
            
            # Salta se dati volume mancanti o non validi
            if pd.isna(avg_volume_20d) or avg_volume_20d == 0:
                continue
            if pd.isna(current_volume):
                continue
                
            rvol = current_volume / avg_volume_20d # Relative Volume
            
            # --- FILTRI TECNICI DI SCREENING ---
            if pd.isna(last_rsi) or last_rsi > rsi_limit: continue
            if avg_volume_20d < MIN_AVG_VOLUME: continue # Liquidez minima
            if pd.isna(last_adx) or last_adx > 50: continue # Evita crolli verticali incontrollati
            
            # --- SCORE TECNICO ---
            score_tecnico = 0
            if last_price > last_open: score_tecnico += 2    # Candela verde
            if rvol > 1.5: score_tecnico += 2               # Volume spike
            if last_sma20 and last_price < (last_sma20 * 0.90): score_tecnico += 1 # Estensione > 10% da SMA20
            
            technical_candidates.append({
                "ticker": ticker,
                "price": round(float(last_price), 2),
                "rsi": round(float(last_rsi), 2),
                "adx": round(float(last_adx), 2),
                "rvol": round(float(rvol), 2),
                "score_tecnico": score_tecnico,
                "trend_5d": [round(float(x), 2) for x in df['Close'].tail(5).tolist()]
            })
            
        except Exception as e:
            continue
    
    print(f"✅ Fase 1 completata: {len(technical_candidates)} candidati tecnici trovati")
    
    # ===== FASE 2: FILTRO FONDAMENTALE (con rate limiting) =====
    print("💰 Fase 2: Analisi fondamentale in corso...")
    final_candidates = []
    
    for i, candidate in enumerate(technical_candidates):
        ticker = candidate['ticker']
        
        try:
            # Delay random per evitare rate limiting (non sul primo)
            if i > 0:
                delay = random.uniform(*RL_DELAY)
                time.sleep(delay)
            
            ticker_info = yf.Ticker(ticker).info
            
            # --- FILTRI FONDAMENTALI ---
            
            # Market Cap
            market_cap = ticker_info.get('marketCap')
            if not market_cap or market_cap < MIN_MARKET_CAP:
                print(f"   ❌ {ticker}: Market Cap insufficiente ({market_cap})")
                continue
            
            # P/E Ratio
            pe_ratio = ticker_info.get('trailingPE') or ticker_info.get('forwardPE')
            if not pe_ratio or pe_ratio <= 0 or pe_ratio > MAX_PE_RATIO:
                print(f"   ❌ {ticker}: P/E non valido o troppo alto ({pe_ratio})")
                continue
            
            # PEG Ratio (opzionale ma preferito)
            peg_ratio = ticker_info.get('pegRatio')
            if peg_ratio and peg_ratio > MAX_PEG_RATIO:
                print(f"   ❌ {ticker}: PEG troppo alto ({peg_ratio})")
                continue
            
            # Profit Margin
            profit_margin = ticker_info.get('profitMargins')
            if not profit_margin or profit_margin < MIN_PROFIT_MARGIN:
                print(f"   ❌ {ticker}: Margine netto insufficiente ({profit_margin})")
                continue
            
            # Free Cash Flow
            fcf = ticker_info.get('freeCashflow')
            if fcf is not None and fcf < MIN_FREE_CASH_FLOW:
                print(f"   ❌ {ticker}: FCF negativo ({fcf})")
                continue
            
            # --- SCORING COMBINATO (tecnico + fondamentale) ---
            score = candidate['score_tecnico']
            if pe_ratio < 15: score += 1           # P/E molto attraente
            if peg_ratio and peg_ratio < 1: score += 2  # Fortemente sottovalutato
            if profit_margin > 0.15: score += 1    # Margine eccellente
            
            company_name = ticker_info.get('longName', ticker)
            sector = ticker_info.get('sector', 'N/A')
            
            final_candidates.append({
                "ticker": ticker,
                "company_name": company_name,
                "sector": sector,
                "price": candidate['price'],
                "rsi": candidate['rsi'],
                "adx": candidate['adx'],
                "rvol": candidate['rvol'],
                "trend_5d": candidate['trend_5d'],
                "market_cap_B": round(market_cap / 1e9, 2),
                "pe_ratio": round(pe_ratio, 2),
                "peg_ratio": round(peg_ratio, 2) if peg_ratio else None,
                "profit_margin_pct": round(profit_margin * 100, 2),
                "fcf_M": round(fcf / 1e6, 2) if fcf else None,
                "score": score
            })
            
            print(f"   ✅ {ticker}: MC=${market_cap/1e9:.1f}B | P/E={pe_ratio:.1f} | PEG={peg_ratio} | Score={score}")
            
        except Exception as e:
            print(f"   ⚠️ Errore fondamentali {ticker}: {e}")
            continue
    
    print(f"🎯 Fase 2 completata: {len(final_candidates)} candidati finali")
    
    # Ordiniamo per score decrescente, poi RSI crescente
    return sorted(final_candidates, key=lambda x: (-x['score'], x['rsi']))[:10]

def get_market_breadth(data, tickers):
    """Calcola quanti titoli sono sopra la loro media mobile a 50 giorni"""
    above_sma50 = 0
    total = 0
    
    for t in tickers:
        try:
            df = data[t].dropna()
            sma50 = ta.sma(df['Close'], length=50).iloc[-1]
            if df['Close'].iloc[-1] > sma50:
                above_sma50 += 1
            total += 1
        except: continue
    
    percentage = (above_sma50 / total) * 100 if total > 0 else 50
    return round(percentage, 2)

def get_macro_sentiment():
    """Recupera le notizie macroeconomiche per definire il contesto di mercato"""
    # Cerchiamo notizie su indici principali e termini macro
    query = "(S&P 500 OR Nasdaq OR Federal Reserve OR Inflation OR Recession)"
    
    try:
        articles = news_api.get_everything(
            q=query,
            language='en',
            sort_by='relevancy',
            page_size=5,
            domains="reuters.com,bloomberg.com,wsj.com"
        )
        
        macro_context = "--- CONTESTO MACRO ATTUALE ---\n"
        for art in articles['articles']:
            macro_context += f"• {art['title']}\n"
        
        return macro_context
    except Exception:
        return "Contesto macro non disponibile."

def get_financial_news(ticker, company_name, sector=None):
    """Recupera news con query semplificata e fallback su settore"""
    
    # Domini finanziari espansi
    fin_domains = "bloomberg.com,reuters.com,cnbc.com,wsj.com,investing.com,seekingalpha.com,benzinga.com,finance.yahoo.com,marketwatch.com,barrons.com,ft.com"
    
    # FASE 1: Query semplificata (solo ticker + nome azienda)
    query = f'{ticker} OR "{company_name}"'
    
    try:
        articles = news_api.get_everything(
            q=query,
            domains=fin_domains,
            language='en',
            sort_by='relevancy',
            page_size=5
        )
        
        # Se trovate news, ritorna
        if articles['articles']:
            news_summary = ""
            for art in articles['articles']:
                news_summary += f"- {art['title']} (Fonte: {art['source']['name']})\n"
            print(news_summary)
            return news_summary
        
        # FASE 2: Fallback su settore (se fornito e nessuna news trovata)
        if sector and sector != 'N/A':
            query_sector = f'"{sector}" stock market'
            
            articles_sector = news_api.get_everything(
                q=query_sector,
                domains=fin_domains,
                language='en',
                sort_by='relevancy',
                page_size=3
            )
            
            if articles_sector['articles']:
                news_summary = f"[News di settore - {sector}]\n"
                for art in articles_sector['articles']:
                    news_summary += f"- {art['title']} (Fonte: {art['source']['name']})\n"
                print(news_summary)
                return news_summary
        
        return "Nessuna notizia finanziaria rilevante trovata."
        
    except NewsAPIException as e:
        return f"Errore nel recupero news: {e}"

def get_report(candidates, breadth):
    """Analisi singola massiva per risparmiare token e tempo"""
    if not candidates: return "Nessun titolo che soddisfa i tuoi criteri trovato oggi."
    
    # Prepariamo il blocco dati per Claude
    full_report_input = ""
    for c in candidates:
        news = get_financial_news(c['ticker'], c['company_name'], c.get('sector'))
        
        # Gestione valori None per PEG e FCF
        peg_display = c['peg_ratio'] if c['peg_ratio'] else "N/A"
        fcf_display = f"${c['fcf_M']}M" if c['fcf_M'] else "N/A"
        
        full_report_input += f"""
        🏢 {c['company_name']} ({c['ticker']})
        📂 Settore: {c['sector']}
        💰 Market Cap: ${c['market_cap_B']}B
        📊 Prezzo: ${c['price']} | RSI: {c['rsi']} | ADX: {c['adx']}
        📈 P/E: {c['pe_ratio']} | PEG: {peg_display} | Margine Netto: {c['profit_margin_pct']}%
        💵 Free Cash Flow: {fcf_display}
        🔊 Vol. Relativo (RVOL): {c['rvol']} | Score Qualità: {c['score']}/8
        📉 Trend 5gg: {c['trend_5d']}
        NOTIZIE RECENTI:
        {news}
        -----------------------------------
        """

    macro_news = get_macro_sentiment()

    prompt = f"""
Sei un analista finanziario esperto. Analizza questi candidati per uno Swing Trade con un orizzonte futuro di circa 2 mesi.

ANALISI TECNICA - Per ogni titolo considera:
1. RSI: se è sotto 25 è un ipervenduto estremo.
2. RVOL (Relative Volume): se è sopra 1.5, indica che sta succedendo qualcosa di importante.
3. PRICE ACTION: se la candela è verde con alto volume, è un segnale di "Buy" potente.
4. ADX: valori tra 20-25 indicano trend emergente, sopra 25 trend forte.

ANALISI FONDAMENTALE (VALUE INVESTING) - Per ogni titolo considera:
5. P/E Ratio: sotto 15 è molto attraente per value investing, sotto 20 è accettabile.
6. PEG Ratio: sotto 1 indica forte sottovalutazione rispetto alla crescita, sotto 1.5 è buono.
7. Profit Margin: margini netti sopra 10% indicano business solido, sopra 15% eccellente.
8. Free Cash Flow: FCF positivo e crescente indica solidità finanziaria e capacità di reinvestimento.
9. Market Cap: considera la dimensione dell'azienda nel contesto del settore.

CONTESTO DI MERCATO:
10. Se l'ampiezza è < 30% e le news macro sono negative, siamo in un regime di 'Panic Selling'. 
    Sii estremamente selettivo. Non consigliare BUY a meno che un titolo non mostri 
    una forza relativa eccezionale, fondamentali solidi, o una 'Capitulation' evidente dai volumi.
11. Considera tutte le notizie recenti per ogni titolo.

NOTIZIE MACROECONOMICHE:
{macro_news}
AMPIEZZA DI MERCATO: {breadth}% dei titoli è sopra la SMA50.
    
DATI TITOLI:
{full_report_input}

Fornisci un report per Telegram usando le Emoji: 🟢 (BUY), 🟡 (WAIT), 🔴 (AVOID).
Includi sempre: ticker, nome azienda, settore, prezzo attuale, P/E, PEG, Target Price e Stop Loss basato sulla volatilità.
Motiva brevemente la raccomandazione citando sia fattori tecnici che fondamentali.
Non superare i 4096 caratteri UTF-8 totali nel report finale.
"""

    try:
        response = client_claude.messages.create(
            model="claude-opus-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        error_msg = f"Errore nella chiamata Claude API: {e}"
        print(error_msg)
        return error_msg

def send_telegram_report(title, text="No report generated."):
    """Invia il report finale"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    full_message = title + "\n\n" + text
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": full_message, "parse_mode": "Markdown"})

def main():
    tickers = get_us_tickers() #+ get_ftse_mib_tickers()
    print(f"✅ Ticker totali caricati: {len(tickers)}")
    raw_data = get_market_data(tickers)
    top_candidates = analyze_and_filter(raw_data, tickers)
    # top_candidates = run_advanced_scanner(raw_data, tickers)
    print(f"TOP CANDIDATES\n{top_candidates}")

    if top_candidates:
        breadth = get_market_breadth(raw_data, tickers)
        report_ai = get_report(top_candidates, breadth)
        print(report_ai)
        send_telegram_report("🚀 *AI SWING SCANNER REPORT*", report_ai)
        print("Report inviato con successo!")
    else:
         send_telegram_report("Nessun titolo trovato che soddisfa i tuoi criteri")


if __name__ == "__main__":
    start_time = time.time()
    
    try:
        # Tutto il workflow: get_data -> analyze -> ai -> telegram
        main()
    except Exception as e:
        # Invia un messaggio di emergenza al tuo bot telegram
        send_telegram_report(f"⚠️ ERRORE CRITICO NELLO SCRIPT: {str(e)}")   
        
    print(f"Tempo totale: {round(time.time() - start_time, 2)} secondi.")
