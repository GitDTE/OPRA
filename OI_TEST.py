from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 78

TRADING_CLASS = "SPXW"
EXPIRY = datetime.now(
    ZoneInfo("America/New_York")
).strftime("%Y%m%d")

ANCHOR_STRIKE = 7680.0
STRIKE_STEP = 5.0
RANGE_STEPS = 10

CONTRACT_REQ_START = 12000
MARKET_REQ_START = 13000

TIMEOUT = 10


class App(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)

        self.connected_event = threading.Event()

        self.contract_events = {}
        self.contract_results = {}

        self.data = {}
        self.lock = threading.Lock()

    # --------------------------------------------------------
    # CONNECTION
    # --------------------------------------------------------

    def nextValidId(self, orderId):
        print("CONNECTED")
        print(f"NEXT VALID ID: {orderId}")
        self.connected_event.set()

    def connectionClosed(self):
        print("\nCONNECTION CLOSED")

    # --------------------------------------------------------
    # ERRORS
    # --------------------------------------------------------

    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        if errorCode in (2104, 2106, 2158, 10090):
            return

        print(
            f"\nIB ERROR: "
            f"reqId={reqId} "
            f"code={errorCode} "
            f"{errorString}"
        )

    # --------------------------------------------------------
    # CONTRACT DETAILS
    # --------------------------------------------------------

    def contractDetails(self, reqId, contractDetails):

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

    # --------------------------------------------------------
    # TICK SIZE
    # --------------------------------------------------------

    def tickSize(self, reqId, tickType, size):

        with self.lock:

            if reqId not in self.data:
                return

            # 27 = Option Call Open Interest
            if tickType == 27:
                self.data[reqId]["call_oi"] = size

            # 28 = Option Put Open Interest
            elif tickType == 28:
                self.data[reqId]["put_oi"] = size


# ============================================================
# FIND CONTRACT
# ============================================================

def find_contract(app, req_id, strike, right):

    contract = Contract()

    contract.symbol = "SPX"
    contract.secType = "OPT"
    contract.exchange = "SMART"
    contract.currency = "USD"

    contract.lastTradeDateOrContractMonth = EXPIRY
    contract.strike = float(strike)
    contract.right = right

    contract.multiplier = "100"
    contract.tradingClass = TRADING_CLASS

    event = threading.Event()

    app.contract_events[req_id] = event
    app.contract_results[req_id] = []

    app.reqContractDetails(req_id, contract)

    if not event.wait(TIMEOUT):
        return None

    results = app.contract_results.get(req_id, [])

    for c in results:

        if (
            c.symbol == "SPX"
            and c.secType == "OPT"
            and c.tradingClass == TRADING_CLASS
            and c.lastTradeDateOrContractMonth == EXPIRY
            and c.right == right
            and abs(float(c.strike) - strike) < 0.001
            and c.multiplier == "100"
        ):
            return c

    return None


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 80)
    print("SPXW 0DTE OPEN INTEREST TEST")
    print("=" * 80)

    print(f"Expiry       : {EXPIRY}")
    print(f"TradingClass : {TRADING_CLASS}")
    print(f"Anchor       : {ANCHOR_STRIKE}")
    print("Generic Tick : 101")
    print()
    print("NO ORDERS")
    print("NO SETTINGS CHANGED")
    print()

    app = App()

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
        return

    # --------------------------------------------------------
    # STRIKES
    # --------------------------------------------------------

    strikes = [
        round(
            ANCHOR_STRIKE + n * STRIKE_STEP,
            2
        )
        for n in range(
            -RANGE_STEPS,
            RANGE_STEPS + 1
        )
    ]

    # --------------------------------------------------------
    # FIND CONTRACTS
    # --------------------------------------------------------

    print(
        f"Finding {len(strikes) * 2} contracts..."
    )

    contracts = []

    req_id = CONTRACT_REQ_START

    for strike in strikes:

        for right in ("C", "P"):

            c = find_contract(
                app,
                req_id,
                strike,
                right
            )

            if c:
                contracts.append(c)

            req_id += 1

    print(
        f"Contracts found: "
        f"{len(contracts)}/{len(strikes) * 2}"
    )

    if not contracts:
        print("NO CONTRACTS FOUND")
        app.disconnect()
        return

    # --------------------------------------------------------
    # MARKET DATA
    # --------------------------------------------------------

    app.reqMarketDataType(1)

    request_map = {}

    req_id = MARKET_REQ_START

    for c in contracts:

        app.data[req_id] = {
            "contract": c,
            "call_oi": None,
            "put_oi": None,
        }

        request_map[req_id] = c

        # 101 = Option Open Interest
        app.reqMktData(
            req_id,
            c,
            "101",
            False,
            False,
            []
        )

        req_id += 1

        time.sleep(0.08)

    print(
        f"OI subscriptions started: "
        f"{len(request_map)}"
    )

    print()
    print("Waiting for OI...")
    print()

    # Give TWS time to return data.
    time.sleep(5)

    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    print("=" * 80)
    print("OPEN INTEREST RESULT")
    print("=" * 80)

    print(
        f"{'STRIKE':>8} "
        f"{'RIGHT':>6} "
        f"{'CONID':>10} "
        f"{'OI':>12}"
    )

    print("-" * 80)

    received = 0

    for req_id, c in sorted(
        request_map.items(),
        key=lambda x: (
            float(x[1].strike),
            x[1].right
        )
    ):

        with app.lock:
            d = dict(
                app.data.get(
                    req_id,
                    {}
                )
            )

        if c.right == "C":
            oi = d.get("call_oi")
        else:
            oi = d.get("put_oi")

        if oi is not None:
            received += 1

        print(
            f"{float(c.strike):>8.2f} "
            f"{c.right:>6} "
            f"{c.conId:>10} "
            f"{str(oi if oi is not None else '-'):>12}"
        )

    print("-" * 80)

    print()
    print(
        f"OI RECEIVED: "
        f"{received}/{len(request_map)}"
    )

    if received == len(request_map):

        print()
        print("RESULT: OI AVAILABLE FOR ALL CONTRACTS.")

    elif received > 0:

        print()
        print("RESULT: OI AVAILABLE, BUT NOT FOR ALL CONTRACTS.")

    else:

        print()
        print("RESULT: NO OI RECEIVED.")

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    for req_id in request_map:

        try:
            app.cancelMktData(req_id)
        except Exception:
            pass

    time.sleep(0.5)

    app.disconnect()

    print()
    print("TEST FINISHED.")


if __name__ == "__main__":
    main()
