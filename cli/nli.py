import asyncio
import json
import logging
import socket
import requests
import urllib3.util.connection as connection
from typing import Any, Dict, List

# Force IPv4 resolution globally to prevent macOS IPv6 lookup timeouts/delays
def allowed_gai_family():
    return socket.AF_INET

connection.allowed_gai_family = allowed_gai_family

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = """
You are the Natural Language Interface (NLI) parsing engine for kcli, a multi-account Zerodha Kite option trading terminal.
Your task is to translate user natural language requests into exact kcli CLI commands.

Available Command Syntax:
1. Switch account context:
   - Syntax: `account <name>` (e.g. `account ZK8719`, `account SS1009`)
2. Exit/square off specific position:
   - Syntax: `exit <symbol>` (e.g. `exit NIFTY26JUN22200PE`)
3. Exit all positions:
   - Syntax: `exit all` (exits all open positions across targeted accounts)
4. Place orders:
   - Syntax: `buy <symbol> <quantity> [price] [product]` or `sell <symbol> <quantity> [price] [product]`
   - Quantity can be in lots using the 'L' suffix (e.g. `27L` for 27 lots, `2L` for 2 lots).
   - Product can be specified at the end: `MIS` or `NRML`. Default is MIS.
   - Price can be optionally specified as a raw numeric decimal/float (e.g., `1.4` or `125.5`).
     - **CRITICAL**: Do NOT prefix the price with the `@` symbol. (For example, `buy NIFTY26JUN22300PE 50 @1.4` is WRONG; use `buy NIFTY26JUN22300PE 50 1.4` instead).
   - Example 1 (Market Order): `sell NIFTY26JUN24000CE 27L`
   - Example 2 (Limit Order): `buy NIFTY26JUN22300PE 1L 1.4`
5. Show Option Chain:
   - Syntax: `oc <UNDERLYING> [week <N>]` (e.g. `oc NIFTY`, `oc NIFTY week 1`)
6. Sync and Utilities:
   - Syntax: `refresh` (refresh data)
   - Syntax: `clear` (clear logs)

Command Chaining:
Multiple commands can be chained together using ` && `.
- Example: `account ZK8719 && exit all`
- Example: `account VJR419 && sell NIFTY26JUN24000CE 27L && sell NIFTY26JUN22000PE 27L`

Resolving Exits and Replication Orders via Open Positions:
- You must always inspect the provided `open_positions` list.
- If the user asks to "exit", "close", "replicate", or "sell" weekly options:
  1. First, search the provided `open_positions` list across all accounts. If any account already holds active positions for the matching expiry or category, extract their exact symbols (`tradingsymbol`) and use those same symbols to formulate the commands. This ensures you replicate the exact same strikes and symbols across accounts.
  2. If no accounts have matching positions open, look up the target option symbols in the provided `available_options` list matching the target expiry and strikes.
  - Do NOT invent or estimate option symbols. Always use either `open_positions` or the fallback `available_options` list.

Context Provided:
We will pass you the user's input, the selected account context, available account names, the nearest weekly expiries, the list of available option contracts near the spot price, and the list of active open positions across all accounts. Use this context to resolve abbreviations (e.g. "zk" -> "ZK8719").

OUTPUT FORMAT:
You MUST return a JSON object with the following fields:
- `command`: The exact matched kcli CLI command string. If the request cannot be translated, return an empty string.
- `confidence`: Confidence score float between 0.0 and 1.0.
- `explanation`: A short description explaining what the command will do.

Strict Rules:
- Do NOT wrap your output in markdown code blocks like ```json ... ```. Output raw JSON only.
- Generate only valid kcli commands as defined above. Do not invent any new commands.
"""

async def parse_natural_language(
    api_key: str,
    user_input: str,
    selected_account: str | None,
    accounts_list: List[str],
    open_positions: List[Dict[str, Any]],
    nifty_spot: float | None,
    nearest_expiries: Dict[str, str] | None = None,
    available_options: List[Dict[str, Any]] | None = None,
    model: str = "gemini-2.5-flash"
) -> Dict[str, Any]:
    """
    Send natural language request to Gemini API and get structured JSON response.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    
    # Formulate context payload for the prompt using local state and option database fallback
    context = {
        "user_input": user_input,
        "current_context": {
            "selected_account": selected_account,
            "nifty_spot": nifty_spot,
            "available_accounts": accounts_list,
            "nearest_expiry_by_underlying": nearest_expiries,
            "available_options": available_options,
            "open_positions": [
                {
                    "account": pos.get("account_name"),
                    "symbol": pos.get("tradingsymbol"),
                    "quantity": pos.get("quantity"),
                    "pnl": pos.get("pnl")
                }
                for pos in open_positions
            ]
        }
    }
    
    prompt = json.dumps(context, indent=2)
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    try:
        resp = await asyncio.to_thread(
            requests.post,
            url,
            headers=headers,
            json=payload,
            proxies={"http": None, "https": None},
            timeout=15
        )
        
        if resp.status_code == 200:
            result = resp.json()
            try:
                text = result["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(text.strip())
                return parsed
            except (KeyError, IndexError, json.JSONDecodeError) as exc:
                logger.error("Failed to parse Gemini JSON output: %s. Raw text: %s", exc, text if 'text' in locals() else "None")
                return {"command": "", "confidence": 0.0, "explanation": f"API parsing error: {exc}"}
        else:
            logger.error("Gemini API error (HTTP %s): %s", resp.status_code, resp.text)
            return {"command": "", "confidence": 0.0, "explanation": f"Gemini API returned status {resp.status_code}"}
    except Exception as exc:
        logger.error("Gemini API request failed: %s", exc)
        return {"command": "", "confidence": 0.0, "explanation": f"Request failed: {exc}"}
