from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo


# ============================================================
# CONFIG
# ============================================================

HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 77

SPX_CONID = 416904
TRADING_CLASS = "SPXW"

ANCHOR_STRIKE = 7680.0
STRIKE_STEP = 5.0
RANGE_STEPS = 10

CONTRACT_REQ_START = 12000
MARKET_REQ_START = 13000

CONTRACT_TIMEOUT = 10

REFRESH_SECONDS = 1.0

# IBKR generic tick:
# 101 = Option Open Interest
OI_GENERIC_TICKS = "101"

# Synthetic SPX:
# Number of nearest valid call/put pairs used
SYNTHETIC_PAIRS = 3


# ============================================================
# HELPERS
# ============================================================

def now_ny():
    return datetime.now(
        ZoneInfo("America/New_York")
    )


def fmt(value, digits=2):
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def fmt_int(value):
    if value is None:
        return "-"
    return str(int(value))


def median(values):

    if not values:
        return None

    values = sorted(values)

    n = len(values)

    middle = n // 2

    if n % 2:
        return values[middle]

    return (values[middle - 1] + values[middle]) / 2.0


# ============================================================
# SYNTHETIC SPX
# ============================================================

def calculate_synthetic_spx(app, request_map):

    by_strike = {}

    # --------------------------------------------------------
    # Collect current quotes by strike/right
    # --------------------------------------------------------

    for req_id, c in request_map.items():

        with app.lock:

            q = dict(
                app.quotes.get(
                    req_id,
                    {}
                )
            )

        strike = float(c.strike)

        if strike not in by_strike:
            by_strike[strike] = {}

        by_strike[strike][c.right] = q

    # --------------------------------------------------------
    # Build valid put-call parity estimates
    #
    # F_K = K + CallMid - PutMid
    #
    # For today's 0DTE test this is the first-order
    # synthetic spot/reference calculation.
    # --------------------------------------------------------

    candidates = []

    for strike, row in by_strike.items():

        call = row.get("C", {})
        put = row.get("P", {})

        call_bid = call.get("bid")
        call_ask = call.get("ask")

        put_bid = put.get("bid")
        put_ask = put.get("ask")

        if (
            call_bid is None
            or call_ask is None
            or put_bid is None
            or put_ask is None
        ):
            continue

        if (
            call_bid <= 0
            or call_ask <= 0
            or put_bid <= 0
            or put_ask <= 0
        ):
            continue

        call_mid = (
            call_bid + call_ask
        ) / 2.0

        put_mid = (
            put_bid + put_ask
        ) / 2.0

        synthetic = (
            strike
            + call_mid
            - put_mid
        )

        candidates.append(
            {
                "strike": strike,
                "call_mid": call_mid,
                "put_mid": put_mid,
                "synthetic": synthetic,
                "distance": abs(
                    strike - ANCHOR_STRIKE
                ),
            }
        )

    # --------------------------------------------------------
    # Select nearest valid ATM pairs
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x: (
            x["distance"],
            x["strike"]
        )
    )

    selected = candidates[
        :SYNTHETIC_PAIRS
    ]

    if not selected:

        return {
            "value": None,
            "selected": [],
            "candidates": candidates,
        }

    values = [
        item["synthetic"]
        for item in selected
    ]

    synthetic_spx = median(values)

    return {
        "value": synthetic_spx,
        "selected": selected,
        "candidates": candidates,
    }


# ============================================================
# IB APPLICATION
# ============================================================

class App(EWrapper, EClient):

    def __init__(self):

        EClient.__init__(self, self)

        self.connected_event = threading.Event()

        self.contract_events = {}
        self.contract_results = {}

        self.quotes = {}
        self.market_types = {}

        self.lock = threading.Lock()

    # ========================================================
    # CONNECTION
    # ========================================================

    def nextValidId(self, orderId):

        print("CONNECTED: True")
        print(f"NEXT_VALID_ID: {orderId}")

        self.connected_event.set()

    def connectionClosed(self):

        print("\nCONNECTION CLOSED")

    # ========================================================
    # ERROR
    # ========================================================

    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):

        if errorCode in (2104, 2106, 2158):
            return

        if errorCode == 10090:
            return

        print(
            f"\nIB ERROR: "
            f"reqId={reqId} "
            f"code={errorCode} "
            f"{errorString}"
        )

    # ========================================================
    # CONTRACT DETAILS
    # ========================================================

    def contractDetails(
        self,
        reqId,
        contractDetails
    ):

        c = contractDetails.contract

        if reqId not in self.contract_results:

            self.contract_results[reqId] = []

        if (
            c.symbol == "SPX"
            and c.secType == "OPT"
            and c.tradingClass == TRADING_CLASS
        ):

            self.contract_results[reqId].append(c)

    def contractDetailsEnd(self, reqId):

        event = self.contract_events.get(reqId)

        if event:
            event.set()

    # ========================================================
    # MARKET DATA TYPE
    # ========================================================

    def marketDataType(
        self,
        reqId,
        marketDataType
    ):

        with self.lock:

            self.market_types[reqId] = marketDataType

    # ========================================================
    # PRICE
    # ========================================================

    def tickPrice(
        self,
        reqId,
        tickType,
        price,
        attrib
    ):

        with self.lock:

            if reqId not in self.quotes:
                return

            q = self.quotes[reqId]

            if tickType == 1:
                q["bid"] = price

            elif tickType == 2:
                q["ask"] = price

            elif tickType == 4:
                q["last"] = price

    # ========================================================
    # SIZE
    # ========================================================

    def tickSize(
        self,
        reqId,
        tickType,
        size
    ):

        with self.lock:

            if reqId not in self.quotes:
                return

            q = self.quotes[reqId]

            if tickType == 0:
                q["bid_size"] = size

            elif tickType == 3:
                q["ask_size"] = size

            elif tickType == 5:
                q["last_size"] = size

            elif tickType == 8:
                q["volume"] = size

            # ------------------------------------------------
            # OPTION OPEN INTEREST
            #
            # 27 = Call Open Interest
            # 28 = Put Open Interest
            # ------------------------------------------------

            elif tickType == 27:
                q["call_oi"] = size

            elif tickType == 28:
                q["put_oi"] = size


# ============================================================
# EXACT CONTRACT SEARCH
# ============================================================

def find_contract(
    app,
    req_id,
    expiry,
    strike,
    right
):

    contract = Contract()

    contract.symbol = "SPX"
    contract.secType = "OPT"
    contract.exchange = "SMART"
    contract.currency = "USD"

    contract.lastTradeDateOrContractMonth = expiry

    contract.strike = float(strike)
    contract.right = right

    contract.multiplier = "100"
    contract.tradingClass = TRADING_CLASS

    event = threading.Event()

    app.contract_events[req_id] = event
    app.contract_results[req_id] = []

    app.reqContractDetails(
        req_id,
        contract
    )

    if not event.wait(CONTRACT_TIMEOUT):

        return None

    results = app.contract_results.get(
        req_id,
        []
    )

    for c in results:

        if (
            c.symbol == "SPX"
            and c.secType == "OPT"
            and c.tradingClass == TRADING_CLASS
            and c.lastTradeDateOrContractMonth == expiry
            and c.right == right
            and abs(float(c.strike) - strike) < 0.001
            and c.multiplier == "100"
        ):

            return c

    return None


# ============================================================
# DISPLAY
# ============================================================

def display(
    app,
    contracts,
    request_map,
    expiry
):

    print("\033[2J\033[H", end="")

    print("=" * 125)
    print("SPXW 0DTE LIVE ENGINE")
    print("=" * 125)

    print(
        f"New York : "
        f"{now_ny().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    print(
        f"Expiry   : {expiry}"
    )

    print(
        f"TWS      : {HOST}:{PORT}"
    )

    print(
        f"Paper API: Client {CLIENT_ID}"
    )

    print()
    print("NO ORDERS. DATA ONLY.")
    print()

    # --------------------------------------------------------
    # Synthetic SPX
    # --------------------------------------------------------

    synthetic = calculate_synthetic_spx(
        app,
        request_map
    )

    synthetic_value = synthetic["value"]
    selected = synthetic["selected"]

    print(
        f"SYNTHETIC SPX : "
        f"{fmt(synthetic_value)}"
    )

    if selected:

        print(
            f"SYNTHETIC PAIRS: "
            f"{len(selected)}/{SYNTHETIC_PAIRS}"
        )

        print(
            "REFERENCE STRIKES: "
            + ", ".join(
                f"{x['strike']:.0f}"
                for x in selected
            )
        )

    else:

        print(
            "SYNTHETIC PAIRS: 0/3"
        )

    print()

    print(
        f"{'STRIKE':>8} "
        f"{'CALL BID':>10} "
        f"{'CALL ASK':>10} "
        f"{'CALL LAST':>10} "
        f"{'CALL VOL':>9} "
        f"{'CALL OI':>9} "
        f"| "
        f"{'PUT BID':>10} "
        f"{'PUT ASK':>10} "
        f"{'PUT LAST':>10} "
        f"{'PUT VOL':>9} "
        f"{'PUT OI':>9}"
    )

    print("-" * 125)

    by_strike = {}

    for req_id, c in request_map.items():

        with app.lock:

            q = dict(
                app.quotes.get(
                    req_id,
                    {}
                )
            )

        strike = float(c.strike)

        if strike not in by_strike:

            by_strike[strike] = {}

        by_strike[strike][c.right] = q

    for strike in sorted(by_strike):

        row = by_strike[strike]

        call = row.get("C", {})
        put = row.get("P", {})

        print(
            f"{strike:>8.2f} "

            f"{fmt(call.get('bid')):>10} "
            f"{fmt(call.get('ask')):>10} "
            f"{fmt(call.get('last')):>10} "
            f"{fmt_int(call.get('volume')):>9} "
            f"{fmt_int(call.get('call_oi')):>9} "

            f"| "

            f"{fmt(put.get('bid')):>10} "
            f"{fmt(put.get('ask')):>10} "
            f"{fmt(put.get('last')):>10} "
            f"{fmt_int(put.get('volume')):>9} "
            f"{fmt_int(put.get('put_oi')):>9}"
        )

    print("-" * 125)

    # ========================================================
    # STATUS
    # ========================================================

    live = 0
    oi_received = 0

    total = len(request_map)

    for req_id, c in request_map.items():

        with app.lock:

            q = app.quotes.get(
                req_id,
                {}
            )

            data_type = app.market_types.get(
                req_id,
                0
            )

            bid = q.get("bid")
            ask = q.get("ask")

            if c.right == "C":
                oi = q.get("call_oi")
            else:
                oi = q.get("put_oi")

        if (
            data_type == 1
            and (
                bid is not None
                or ask is not None
            )
        ):

            live += 1

        if oi is not None:

            oi_received += 1

    print()

    print(
        f"L1 LIVE     : {live}/{total}"
    )

    print(
        f"OI RECEIVED : {oi_received}/{total}"
    )

    print()

    print(
        "Press Ctrl+C to stop."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    expiry = now_ny().strftime("%Y%m%d")

    print("=" * 90)
    print("SPXW 0DTE LIVE ENGINE")
    print("=" * 90)

    print(f"Expiry       : {expiry}")
    print(f"TradingClass : {TRADING_CLASS}")
    print(f"Anchor       : {ANCHOR_STRIKE}")
    print(f"Range        : +/- {RANGE_STEPS}")
    print()

    print("NO ORDERS WILL BE SENT.")
    print("NO SETTINGS WILL BE CHANGED.")
    print()

    app = App()

    # ========================================================
    # CONNECT
    # ========================================================

    print("Connecting...")

    app.connect(
        HOST,
        PORT,
        CLIENT_ID
    )

    thread = threading.Thread(
        target=app.run,
        daemon=True
    )

    thread.start()

    if not app.connected_event.wait(10):

        print("CONNECTION FAILED")

        try:
            app.disconnect()
        except Exception:
            pass

        return

    # ========================================================
    # BUILD STRIKES
    # ========================================================

    strikes = []

    for n in range(
        -RANGE_STEPS,
        RANGE_STEPS + 1
    ):

        strikes.append(
            round(
                ANCHOR_STRIKE + n * STRIKE_STEP,
                2
            )
        )

    # ========================================================
    # DISCOVER CONTRACTS
    # ========================================================

    print()
    print(
        f"Discovering {len(strikes) * 2} SPXW contracts..."
    )

    contracts = []

    req_id = CONTRACT_REQ_START

    for strike in strikes:

        for right in ("C", "P"):

            c = find_contract(
                app,
                req_id,
                expiry,
                strike,
                right
            )

            if c:

                contracts.append(c)

            req_id += 1

            time.sleep(0.10)

    print(
        f"Contracts found: "
        f"{len(contracts)}/{len(strikes) * 2}"
    )

    if not contracts:

        print("NO CONTRACTS FOUND")

        app.disconnect()

        return

    # ========================================================
    # START LIVE DATA
    # ========================================================

    app.reqMarketDataType(1)

    request_map = {}

    req_id = MARKET_REQ_START

    for c in contracts:

        app.quotes[req_id] = {

            "contract": c,

            "bid": None,
            "ask": None,
            "last": None,

            "bid_size": None,
            "ask_size": None,
            "last_size": None,

            "volume": None,

            "call_oi": None,
            "put_oi": None,
        }

        request_map[req_id] = c

        app.reqMktData(
            req_id,
            c,
            OI_GENERIC_TICKS,
            False,
            False,
            []
        )

        req_id += 1

        time.sleep(0.08)

    print()
    print(
        f"L1 + OI subscriptions started: "
        f"{len(request_map)}"
    )

    # ========================================================
    # LIVE LOOP
    # ========================================================

    try:

        while app.isConnected():

            display(
                app,
                contracts,
                request_map,
                expiry
            )

            time.sleep(
                REFRESH_SECONDS
            )

    except KeyboardInterrupt:

        print()
        print()
        print("Stopping engine...")

    finally:

        # ====================================================
        # CANCEL DATA
        # ====================================================

        for request_id in request_map:

            try:

                app.cancelMktData(
                    request_id
                )

            except Exception:

                pass

        time.sleep(0.5)

        try:

            app.disconnect()

        except Exception:

            pass

        print()
        print("ENGINE STOPPED.")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
