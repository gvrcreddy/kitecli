# KiteCLI Workspace Agent Rules

These rules apply to all AI agents working on the `kitecli` repository. Follow these constraints to prevent regression bugs, ensure trade safety, and maintain TUI scroll performance.

## 1. Safety Guardrails & Testing
*   **No Live Trading in Tests**: Under no circumstances should test modules execute live network calls to Zerodha or place actual orders. Always use `unittest.mock` to patch `KCLIClient`, `KiteTicker`, and `KCLILiveSession.execute_exit`.
*   **Mandatory Test Validation**: After any modification to the CLI or the broker manager, you MUST run the test suite and verify that all tests pass:
    ```bash
    python3 run_tests.py
    ```

## 2. Command & Parameter Rules
*   **Limit Price Syntax**: Limit prices parsed from command inputs must be raw floats (e.g. `1.40`). Do not prefix prices with the `@` symbol (e.g. `@1.40` is invalid).
*   **Symbol Mapping for Exits**:
    *   The `exit all` command must map the `"all"` symbol to `None` when calling `execute_exit` or `exit_positions`.
    *   Position lookup is resolved via active positions table matching or index integers (1, 2, 3...) mapped in `session.position_id_map`.
*   **Order Quantity Splitting**:
    *   Order quantities larger than the exchange limit (1800) must route through `place_order` in `kite_manager.py` to be automatically sliced. Never bypass this code for large volume orders.

## 3. UI Scroll & State
*   **Dynamic WebSocket Connection Header**:
    *   The header uses `FormattedTextControl` with a list of text fragments to map a custom mouse click callback (`_header_click_handler`) on the WebSocket status label.
    *   Connection states are tracked via `on_connect`, `on_close`, and `on_error` callbacks inside `live_session.py`.
    *   Do not replace the header fragments with a plain string; keep the HTML/fragment structure intact for mouse click routing.
*   **Buffer Scroll Movement**:
    *   Use prompt-toolkit's native cursor movement (`cursor_up()` and `cursor_down()`) on the underlying pane buffers instead of modifying scroll values directly. This prevents scroll snapping and locks.

## 4. Multi-Broker Integration & TUI Quirks
*   **Kotak Neo Session Retries**:
    *   When intercepting `100008` (unauthorized) or `"unauthorized"` body responses to trigger auto-login retries, you MUST update both the request headers (`Sid`, `Auth`) and the URL query parameters (`sId`, `sid`) to keep them in sync. Mismatches will result in recurring 401s from the Kotak Neo gateway.
*   **Kotak Static IP Whitelisting**:
    *   Kotak Neo enforces strict static IP whitelisting ONLY on order placement APIs (place, modify, cancel). Limits and positions APIs are exempt. If limits/positions succeed but order placement returns `unauthorized`, verify if the proxy configured in `config.yaml` is whitelisted on the developer portal.
*   **TUI Ctrl+R History Search**:
    *   The `SearchToolbar` must be instantiated before the `TextArea` and passed directly into the `search_field` constructor argument. Dynamic assignment of `TextArea.search_field` is not supported by prompt-toolkit.
*   **Immediate UI Refresh**:
    *   Successful order placement (`execute_order`) and position exits (`execute_exit`) must explicitly trigger TUI refresh (`_trigger_immediate_refresh`) to ensure immediate rendering, as WebSocket order update ticks are not guaranteed or can be delayed on certain brokers (e.g. Kotak).
*   **Kotak Neo WebSocket Error Mapping**:
    *   When returning errors from `KotakTicker._on_error`, you MUST map auth-related errors to code `403` so that the live session's `on_error` handler knows to cleanly stop reconnection and avoid auth-rate-limiting, while letting ordinary connection drops trigger exponential backoff.
*   **Throttled TUI Logging for REST Refresh**:
    *   REST refresh failures caught in `_trigger_immediate_refresh` must be logged to the Status Logs pane using `_ws_should_log` with a 30s minimum interval to avoid spamming the user interface.
*   **Broker Proxy Routing**:
    *   Both Zerodha and Kotak Neo only enforce static IP whitelisting on order placement REST APIs (POST/PUT/DELETE /place, /modify, /cancel).
    *   To avoid proxy-related network latency, 502 Bad Gateway REST errors, and 1006 WebSocket connection handshake timeouts, all read-only REST APIs (positions, margins/limits, orderbook, profile) and all WebSocket connections (both Zerodha's `KiteTicker` / `probe_ws_auth` and Kotak's `KotakTicker` / WebSocket feed) must bypass proxies and connect directly.
*   **Handling Non-PyPI Dependencies**:
    *   Third-party packages not hosted on PyPI (such as Kotak Neo's `neo-api-client`) must **never** be listed in `pyproject.toml`'s dependencies or optional-dependencies list to prevent PyPI dependency resolution crashes.
    *   Instead, import them lazily at runtime and catch the `ImportError` to raise a clear instruction pointing to the direct Git repository installation command (e.g. `pip install "git+https://...#egg=neo_api_client"`). Always document the Git installation step in `README.md`.

