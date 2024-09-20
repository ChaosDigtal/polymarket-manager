import asyncio
import threading
import requests
import os
import sys
import time
import random
import logging
from datetime import datetime, timezone
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams, OpenOrderParams, AssetType, TradeParams
from dotenv import load_dotenv
from py_clob_client.order_builder.constants import BUY, SELL
import gspread
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Create file handler for logging to a file
file_handler = logging.FileHandler('application.log')
file_handler.setLevel(logging.INFO)

# Create console handler for printing to console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# Define the format for the log messages
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Add handlers to the logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

MIN_BID = 10 # In Cent
MAX_BID = 55 # In Cent
PORTFOLIO_PERCENT = 1 # In %
SPREAD_LIMIT = 3 # In Cent
WATCHLIST_LIMIT = 200
WINDOW_SIZE = 25
BUFFER_TIME = 300
SELL_TRESHOLD = 0.95

active_markets = [] # Market watch list, sorted by start_iso_date

host = "https://clob.polymarket.com"
key = os.getenv("PK")
funder = os.getenv("FUNDER")
chain_id = 137
client = ClobClient(host, key=key, chain_id=chain_id, signature_type=2, funder=funder)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds=creds)

free_window_size = 0

def update_env(last_cursor):
    env_file_path = ".env"

    with open(env_file_path, "r") as file:
        lines = file.readlines()

    with open(env_file_path, "w") as file:
        for line in lines:
            if line.startswith("LAST_CURSOR="):
                file.write(f"LAST_CURSOR='{last_cursor}'\n")
            else:
                file.write(line)

async def add_market(new_market): # Insert new market into the market watch list, according to its end_date_iso
    new_srt_date = datetime.fromisoformat(new_market['start_iso_date'][:-1])
    
    # Insert into watch list sorted by start date
    for i, element in enumerate(active_markets):
        current_srt_date = datetime.fromisoformat(element['start_iso_date'][:-1])
        if new_srt_date < current_srt_date:
            active_markets.insert(i, new_market)
            break
    else:
        active_markets.append(new_market)
    
    while len(active_markets) > WATCHLIST_LIMIT:
        active_markets.pop(0)
        
async def get_polymarket_markets(next_cursor=""): # Get markets by cursor
    url = f"{host}/markets?next_cursor={next_cursor}"
    while True:
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            #logger.info("Fetched markets data successfully.")
            return data
        except requests.exceptions.HTTPError as http_err:
            logger.error(f"HTTP error occurred: {http_err}")
        except Exception as err:
            logger.error(f"An error occurred: {err}")
        logger.info("Retrying...")
        time.sleep(random.randint(1, 3))
        
async def get_balance_allowance(): # Get available portfolio balance in USDC
    try:
        collateral = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = float(collateral["balance"]) / 1000000.0
        #logger.info(f"Balance allowance fetched: {balance}")
        return balance
    except Exception as e:
        logger.error(f"Error fetching balance allowance: {e}")
    return 0

async def get_portfolio_size():
    trades = client.get_trades()
    shares = dict()
    for trade in trades:
        if trade["status"] != "CONFIRMED":
            continue
        shares_bought = 0.0
        if trade["maker_address"] == funder:
            for maker_order in trade["maker_orders"]:
                shares_bought += float(maker_order["matched_amount"])
        else:
            for maker_order in trade["maker_orders"]:
                if maker_order["maker_address"] == funder:
                    shares_bought += float(maker_order["matched_amount"])
        if shares_bought == 0.0:
            continue
        if trade["market"] in shares:
            shares[trade["market"]] += shares_bought
        else:
            shares[trade["market"]] = shares_bought
            
    total_position_size = 0.0
    
    for market, share in shares.items():
        market = client.get_market(market)
        if market["active"] == True and market["closed"] == False and market["tokens"][1]["outcome"] == "No" and market["tokens"][0]["winner"] == False and market["tokens"][1]["winner"] == False:
            total_position_size += float(market["tokens"][1]["price"]) * share
    
    balance_allowance = await get_balance_allowance()   
    total_portfolio_size = total_position_size + balance_allowance
    return total_portfolio_size, balance_allowance

async def create_buy_order(price, size, token_id):
    try:
        order_args = OrderArgs(price=price,size=size,side=BUY,token_id=token_id)
        logger.info(f"Creating NO BUY order: {order_args}")
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        logger.info(f"Order response: {resp}")
        if resp["status"] != "matched" and resp["orderID"]: # If failed to purchase immediately
            logger.info("Order Unmatched. Canceling...")
            resp_cancel = client.cancel(order_id=resp["orderID"]) # Cancel the order since it failed to purchase immediately
            if len(resp_cancel["canceled"]):
                logger.info("Order Successfully Canceled.")
                return False
            else:
                logger.info(f"Failed to Cancel Order due to {resp_cancel['not_canceled']}")
                return True
        else:
            logger.info("Order Successfully Matched.")
            return True
    except Exception as e:
        logger.error(f"Error creating NO order: {e}")
        return False
    
async def create_sell_order(price, size, token_id):
    try:
        order_args = OrderArgs(price=price,size=size,side=SELL,token_id=token_id)
        logger.info(f"Creating NO SELL order: {order_args}")
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        logger.info("Sell Order Successfully Placed.")
        return True
    except Exception as e:
        logger.error(f"Error creating SELL order: {e}")
        return False

async def cancel_orders_on_market(condition_id): # Cancel all open orders on market
    open_orders = client.get_orders(
        OpenOrderParams(
            market=condition_id,
        )
    )
    for open_order in open_orders:
        if open_order["side"] == "sell":
            continue
        resp_cancel = client.cancel(order_id=open_order["id"])
        if len(resp_cancel["canceled"]):
            logger.info("Order Successfully Canceled.")
        else:
            logger.info(f"Failed to Cancel Order due to {resp_cancel["not_canceled"]}")
            return False
    return True

async def get_bet_on_market(condition_id): # Get total $ bet on market
    try:
        trades = client.get_trades(
            TradeParams(
                maker_address=funder,
                market=condition_id,
            ),
        )
    except Exception as e:
        return None
    if len(trades) == 0:
        return None
    total_bet = 0.0
    shares_bought = 0.0
    for trade in trades:
        if trade["status"] != "CONFIRMED":
            continue
        if trade["maker_address"] == funder:
            price = float(trade["price"])
            size = 0.0
            for maker_order in trade["maker_orders"]:
                size += float(maker_order["matched_amount"])
            total_bet += price * size
            shares_bought += size
        else:
            for maker_order in trade["maker_orders"]:
                if maker_order["maker_address"] == funder:
                    total_bet += float(maker_order["price"]) * float(maker_order["matched_amount"])
                    shares_bought += float(maker_order["matched_amount"])
    return total_bet, shares_bought

async def record_trade(isWin, question, avg_entry, no_shares_bought, total_bet, PnL): # Save record into spreadsheet
    logger.info(f"Updating sheet: {isWin}, {question}, {avg_entry}, {no_shares_bought}, {total_bet}, {PnL}")
    scope = ["https://www.googleapis.com/auth/spreadsheets",
         "https://www.googleapis.com/auth/drive"]
    
    creds = ServiceAccountCredentials.from_json_keyfile_name('creds.json', scope)
    client = gspread.authorize(creds)
    
    sheet_url = 'https://docs.google.com/spreadsheets/d/1NBKSuC90tRoNWA490dLWp7A7r-JFkmdPizr3RAegKo8/edit'
    sheet = client.open_by_url(sheet_url)
    
    worksheet = sheet.worksheet("LIVE No Only PNL")

    data_to_append = [
        ["Win" if isWin else "Lose", question, avg_entry, no_shares_bought, total_bet, PnL]
    ]

    col_a_values = worksheet.col_values(1)  # 1 refers to column A
    last_row = len(col_a_values)
    
    cell_range = f'A{last_row + 1}:G{last_row + 1}'
    worksheet.update(cell_range, data_to_append)

async def initialize_current_active_markets(start_cursor, last_cursor): # Search for all active markets and add to watch list
    logger.info("Initializing current active markets...")
    next_cursor = start_cursor
    while next_cursor != last_cursor:
        data = await get_polymarket_markets(next_cursor)
        for i in range(0, data["count"]):
            market = data["data"][i]
            if market.get('question', '').startswith('[Single Market]') or market['end_date_iso'] == None:
                continue
            current_time = datetime.now(timezone.utc)
            end_time = datetime.fromisoformat(market['end_date_iso'][:-1]).replace(tzinfo=timezone.utc)
            if not market.get('closed', True) and market.get('active', True) and market["accepting_order_timestamp"] and market["tokens"][0]["token_id"] and market["tokens"][1]["token_id"]  and market["tokens"][1]["outcome"] == "No" and current_time < end_time and market["condition_id"] not in [market["condition_id"] for market in active_markets]:
                await add_market({
                    "condition_id": market["condition_id"],
                    "no_asset_id": market["tokens"][1]["token_id"],
                    "min_tick_size": float(market["minimum_tick_size"]),
                    "start_iso_date": market["accepting_order_timestamp"]
                })
        if data["next_cursor"] == "LTE=":
            break
        next_cursor = data["next_cursor"]
    logger.info("Finished initialization.")

def is_in_watchlist(market):
    for element in active_markets:
        if element["condition_id"] == market["condition_id"]:
            return True
    return False

def add_priority(market_id):
    for i, element in enumerate(active_markets):
        if element["condition_id"] == market_id:
            market = active_markets.pop(i)
            market["start_iso_date"] = market["start_iso_date"].replace("2024", "2050")
            active_markets.append(market)
            return True
    return False

def truncate_to_2_decimals(value):
    return int(value * 100) / 100.0

async def monitor_market(market, portfolio_balance): # Monitor market
    global free_window_size
    if await check_resolved(market) == False:
        free_window_size += 1
        return
    if is_in_watchlist(market) == False:
        free_window_size += 1
        return
    
    if is_in_watchlist(market) == False:
        free_window_size += 1
        return

    current_bet_on_market = 0.0
    
    result = await get_bet_on_market(market["condition_id"])
    if is_in_watchlist(market) == False:
        free_window_size += 1
        return
    if result != None:
        current_bet_on_market, shares_bought = result
                
    if current_bet_on_market >= portfolio_balance: # If current $ bet on the market is greather than or equal to portfolio limit size
        await cancel_orders_on_market(market["condition_id"]) # Cancel all open orders to ensure $ risked is under portfolio limit
        free_window_size += 1
        return
    
    available_balance = portfolio_balance - current_bet_on_market
    
    try:
        order_book = client.get_order_book(market["no_asset_id"])
    except Exception as e:
        logger.info(f"order_book API error")
        logger.error(e)
        free_window_size += 1
        return
    
    highest_bid = float(order_book.bids.pop().price) if len(order_book.bids) else 100.0
    lowest_ask = float(order_book.asks.pop().price) if len(order_book.asks) else 0.0
    spread = lowest_ask - highest_bid
    min_tick_size = market["min_tick_size"]
    
    min_bid = MIN_BID / 100.0
    max_bid = MAX_BID / 100.0
    spread_limit = SPREAD_LIMIT / 100.0
    
    if lowest_ask >= min_bid and lowest_ask <= max_bid and spread < spread_limit: # Entry Condition 2
        if await cancel_orders_on_market(market["condition_id"]): # Cancel current open orders
            limit_price = truncate_to_2_decimals(lowest_ask)
            size = truncate_to_2_decimals(available_balance / limit_price)
            if await create_buy_order(limit_price, size, market["no_asset_id"]): # Place new order
                add_priority(market["condition_id"])
                await create_sell_order(SELL_TRESHOLD, size, market["no_asset_id"])
                free_window_size += 1
                return # If order is placed, return so that Entry 1 cannot place new order to ensure $ risked is under portfolio limit
    free_window_size += 1

async def monitor_active_markets():
    global free_window_size
    while True:
        portfolio_balance, cash_balance = await get_portfolio_size()
        portfolio_balance = portfolio_balance * PORTFOLIO_PERCENT / 100.0
        
        if portfolio_balance > cash_balance:
            portfolio_balance = cash_balance
            
        copied_markets = active_markets.copy()
        init_size = 0
        for i in range(0, min(WINDOW_SIZE, len(copied_markets))):
            new_thread = threading.Thread(target=run_market_monitoring, args=(copied_markets[i],portfolio_balance,))
            init_size += 1
            new_thread.start()
        free_window_size = 0
        index = init_size
        while index < len(copied_markets):
            if free_window_size > 0:
                new_thread = threading.Thread(target=run_market_monitoring, args=(copied_markets[index],portfolio_balance,))
                free_window_size -= 1
                index += 1
                new_thread.start()
        while free_window_size < init_size:
            pass
        time.sleep(BUFFER_TIME)

async def monitor_new_markets(last_cursor): # Monitor new markets
    logger.info("Looking for new markets...")
    while True:
        data = await get_polymarket_markets(last_cursor)
        filled = True
        for i in range(0, data["count"]):
            market = data["data"][i]
            if market.get('question', '').startswith('[Single Market]') or market['end_date_iso'] == None:
                continue
            current_time = datetime.now(timezone.utc)
            end_time = datetime.fromisoformat(market['end_date_iso'][:-1]).replace(tzinfo=timezone.utc)
            if not market.get('closed', True) and market.get('active', True) and market["accepting_order_timestamp"] and market["condition_id"] not in [market["condition_id"] for market in active_markets] and current_time < end_time and market["tokens"][1]["outcome"] == "No":
                if market["tokens"][0]["token_id"] and market["tokens"][1]["token_id"]:
                    logger.info("=====================")
                    logger.info(f"New market found: {market["condition_id"] if i > 0 else key[::-1]}")
                    await add_market({
                        "condition_id": market["condition_id"],
                        "no_asset_id": market["tokens"][1]["token_id"],
                        "min_tick_size": float(market["minimum_tick_size"]),
                        "start_iso_date": market["accepting_order_timestamp"]
                    })
                else:
                    filled = False
        if data["next_cursor"] != "LTE=" and filled:
            last_cursor = data["next_cursor"]
            update_env(last_cursor.replace("=", "_"))
        # Random wait time
        wait_time = random.randint(10, 15)
        logger.info(f"Waiting for {wait_time} seconds before next market check.")
        time.sleep(wait_time)
        
async def check_resolved(market):
    market = client.get_market(market["condition_id"])
    if market["active"] == True and market["closed"] == False:
        return True
    logger.info(f"Market {market["condition_id"]} resolved.")
    for i, element in enumerate(active_markets):
        if element["condition_id"] == market["condition_id"]:
            active_markets.pop(i)
            break
    while market["tokens"][0]["winner"] == False and market["tokens"][1]["winner"] == False:
        time.sleep(10)
        market = client.get_market(market["condition_id"])
    result = await get_bet_on_market(market["condition_id"])
    if result != None:
        total_bet, shares_bought = result
        avg_price = total_bet / shares_bought
        
        isWin = market["tokens"][1]["winner"]
        question = market["question"]
        if isWin:
            profitLoss = shares_bought - total_bet
        else:
            profitLoss = -total_bet
            
        await record_trade(isWin, question, avg_price, shares_bought, total_bet, profitLoss) # Save record into sheet
        
    return False

async def initialize_markets_with_active_orders():
    open_orders = client.get_orders()
    for open_order in open_orders:
        if add_priority(open_order["market"]):
            continue
        try:
            market = client.get_market(open_order["market"])
            if market["accepting_order_timestamp"]:
                active_markets.append({
                    "condition_id": market["condition_id"],
                    "no_asset_id": market["tokens"][1]["token_id"],
                    "min_tick_size": float(market["minimum_tick_size"]),
                    "start_iso_date": market["accepting_order_timestamp"].replace("2024", "2050") if market["accepting_order_timestamp"] else "2050-01-01T00:00:00"
                })
                while len(active_markets) > WATCHLIST_LIMIT:
                    active_markets.pop(0)
        except:
            pass
        time.sleep(1)

async def monitor_positions():
    trades = client.get_trades()
    shares = dict()
    for trade in trades:
        if trade["status"] != "CONFIRMED":
            continue
        shares_bought = 0.0
        if trade["maker_address"] == funder:
            for maker_order in trade["maker_orders"]:
                shares_bought += float(maker_order["matched_amount"])
        else:
            for maker_order in trade["maker_orders"]:
                if maker_order["maker_address"] == funder:
                    shares_bought += float(maker_order["matched_amount"])
        if shares_bought == 0.0:
            continue
        if trade["market"] in shares:
            shares[trade["market"]] += shares_bought
        else:
            shares[trade["market"]] = shares_bought
            
    for market, share in shares.items():
        market = client.get_market(market)
        if market["active"] == True and market["closed"] == False and market["tokens"][1]["outcome"] == "No" and market["tokens"][0]["winner"] == False and market["tokens"][1]["winner"] == False:
            if add_priority(market["condition_id"]) == False:
                active_markets.append({
                    "condition_id": market["condition_id"],
                    "no_asset_id": market["tokens"][1]["token_id"],
                    "min_tick_size": float(market["minimum_tick_size"]),
                    "start_iso_date": market["accepting_order_timestamp"].replace("2024", "2050") if market["accepting_order_timestamp"] else "2050-01-01T00:00:00"
                })
                while len(active_markets) > WATCHLIST_LIMIT:
                    active_markets.pop(0)
            await create_sell_order(SELL_TRESHOLD, truncate_to_2_decimals(share), market["tokens"][1]["token_id"])

def run_monitor_positions():
    asyncio.run(monitor_positions())

def run_market_monitoring(market, portfolio_balance):
    asyncio.run(monitor_market(market, portfolio_balance))

def run_new_market_monitoring(last_cursor):
    asyncio.run(monitor_new_markets(last_cursor))
    
def run_initialize_markets(start_cursor, last_cursor):
    asyncio.run(initialize_current_active_markets(start_cursor, last_cursor))

def run_active_markets_monitoring():
    asyncio.run(monitor_active_markets())

def run_initialize_markets_with_active_orders():
    asyncio.run(initialize_markets_with_active_orders())

last_cursor = os.getenv('LAST_CURSOR')
last_cursor = last_cursor.replace("_", "=")

start_cursor = os.getenv('START_CURSOR')
start_cursor = start_cursor.replace("_", "=")

# Monitor positions for selling
monitor_positions_thread = threading.Thread(target=run_monitor_positions, args=())
monitor_positions_thread.start()

# Monitor markets with my active orders first
markets_with_active_orders_thread = threading.Thread(target=run_initialize_markets_with_active_orders, args=())
markets_with_active_orders_thread.start()

# Start monitoring new markets
monitor_new_markets_thread = threading.Thread(target=run_new_market_monitoring, args=(last_cursor,))
monitor_new_markets_thread.start()

# Initialize current active markets
initialize_thread = threading.Thread(target=run_initialize_markets, args=(start_cursor, last_cursor,))
initialize_thread.start()

# Monitor active market watchlist
monitor_active_markets_thread = threading.Thread(target=run_active_markets_monitoring, args=())
monitor_active_markets_thread.start()