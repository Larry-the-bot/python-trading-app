# python-trading-app

Weekly options **max pain** as a trading condition.

Max pain is the strike that minimizes the total intrinsic value of open
call and put interest if the underlying expired at that strike.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```python
from max_pain import get_weekly_max_pain

result = get_weekly_max_pain("SPY")
print(result.max_pain, result.expiration, result.spot, result.above_max_pain)
```

`get_weekly_max_pain` loads the nearest weekly option chain via
[yfinance](https://github.com/ranaroussi/yfinance), computes max pain from
open interest, and returns a `MaxPainResult`.

Pure helpers (`compute_max_pain`, `nearest_weekly_expiration`) take in-memory
data and do not hit the network.

## Tests

```bash
pytest
```
