# Swing Scanner

This repository contains `scanner_ipervenduto.py`, a Python script for scanning US tickers with a mix of technical and fundamental filters and sending the result to Telegram.

## Requirements

Install the required dependencies:

```bash
pip install -r requirements.txt
```

## Environment configuration

The script loads secrets from a `.env` file at `/home/gianrico/.trading_env` by default.

Create that file and add the following values:

```env
ANTHROPIC_KEY=your_anthropic_api_key
NEWS_API_KEY=your_newsapi_api_key
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

### How to obtain API keys

- `ANTHROPIC_KEY`: sign up for Anthropic and generate an API key on the Anthropic dashboard.
- `NEWS_API_KEY`: sign up at [NewsAPI.org](https://newsapi.org/) and create an API key.
- `TELEGRAM_TOKEN`: create a Telegram bot with [BotFather](https://t.me/BotFather) and copy the bot token.
- `TELEGRAM_CHAT_ID`: send a message to the bot or use a Telegram chat ID discovery bot to get your chat ID.

## How to run

Run the scanner from the repository root:

```bash
python scanner_ipervenduto.py
```

The script will:
- download ticker data from Yahoo Finance and Nasdaq sources
- apply technical and fundamental filters
- build an AI report via Anthropic
- send the report to Telegram

## Configurable filters

Open `scanner_ipervenduto.py` and adjust the constants near the top.

### Fundamental filters

- `MIN_MARKET_CAP`: minimum market capitalization in USD
- `MAX_PE_RATIO`: maximum acceptable P/E ratio
- `MAX_PEG_RATIO`: maximum acceptable PEG ratio
- `MIN_PROFIT_MARGIN`: minimum net profit margin
- `MIN_FREE_CASH_FLOW`: minimum free cash flow threshold

Example:

```python
MIN_MARKET_CAP = 1_000_000_000   # $1B
MAX_PE_RATIO = 25
MAX_PEG_RATIO = 1.5
MIN_PROFIT_MARGIN = 0.05
MIN_FREE_CASH_FLOW = -10_000_000
```

### Volume and rate limiting

- `MIN_AVG_VOLUME`: minimum average volume for technical filtering
- `MIN_DOLLAR_VOLUME`: minimum dollar volume threshold
- `RL_DELAY`: tuple with random delay range between Yahoo Finance requests

Example:

```python
MIN_AVG_VOLUME = 10_000_000
MIN_DOLLAR_VOLUME = 5_000_000
RL_DELAY = (5, 10)
```

## Notes

- The script currently uses the global `/home/gianrico/.trading_env` path. Update `load_dotenv(...)` in `scanner_ipervenduto.py` if you want a different location.
- If you use SSH or proxies for network access, configure your environment separately.

## Troubleshooting

- If the script fails to fetch data, verify your internet connection and API key validity.
- Use `print` output from `scanner_ipervenduto.py` to inspect which filter stage failed.
- For Telegram issues, verify the bot token and chat ID.
