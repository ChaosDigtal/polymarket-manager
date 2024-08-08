import asyncio
import requests
import os
import random
import logging
from datetime import datetime, timezone
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams, OpenOrderParams, AssetType, TradeParams
from dotenv import load_dotenv
from py_clob_client.order_builder.constants import BUY
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

active_markets = [] # Market watch list, sorted by end_date_iso

host = "https://clob.polymarket.com"
key = os.getenv("PK")
funder = os.getenv("FUNDER")
chain_id = 137
client = ClobClient(host, key=key, chain_id=chain_id, signature_type=2, funder=funder)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds=creds)

def add_market(new_market): # Insert new market into the market watch list, according to its end_date_iso
    new_end_date = datetime.fromisoformat(new_market['end_iso_date'][:-1])
    
    for i, element in enumerate(active_markets):
        current_end_date = datetime.fromisoformat(element['end_iso_date'][:-1])
        if new_end_date < current_end_date:
            active_markets.insert(i, new_market)
            break
    else:
        active_markets.append(new_market)

async def get_polymarket_markets(next_cursor=""): # Get markets by cursor
    url = f"{host}/markets?next_cursor={next_cursor}"
    while True:
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            logger.info("Fetched markets data successfully.")
            return data
        except requests.exceptions.HTTPError as http_err:
            logger.error(f"HTTP error occurred: {http_err}")
        except Exception as err:
            logger.error(f"An error occurred: {err}")
        logger.info("Retrying...")
        
async def get_balance_allowance(): # Get available portfolio balance in USDC
    try:
        collateral = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = float(collateral["balance"]) / 1000000.0
        logger.info(f"Balance allowance fetched: {balance}")
        return balance
    except Exception as e:
        logger.error(f"Error fetching balance allowance: {e}")
    return 0

async def create_order(price, size, token_id, isAsk=False): # Place limit order, isAsk: True=> Entry 2, False=> Entry 1, return True if order is placed, False otherwise
    try:
        order_args = OrderArgs(price=price,size=size,side=BUY,token_id=token_id)
        logger.info(f"Creating NO order: {order_args}")
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        logger.info(f"Order response: {resp}")
        if isAsk and resp["status"] != "matched" and resp["orderID"]: # If Entry 2 and failed to purchase immediately
            logger.info("Order Unmatched. Canceling...")
            resp_cancel = client.cancel(order_id=resp["orderID"]) # Cancel the order since it failed to purchase immediately
            if len(resp_cancel["canceled"]):
                logger.info("Order Successfully Canceled.")
                return False
            else:
                logger.info(f"Failed to Cancel Order due to {resp_cancel["not_canceled"]}")
                return True
        else:
            logger.info("Order Successfully Matched.")
            return True
    except Exception as e:
        logger.error(f"Error creating NO order: {e}")
        return False

async def cancel_orders_on_market(condition_id): # Cancel all open orders on market
    open_orders = client.get_orders(
        OpenOrderParams(
            market=condition_id,
        )
    )
    for open_order in open_orders:
        resp_cancel = client.cancel(order_id=open_order["id"])
        if len(resp_cancel["canceled"]):
            logger.info("Order Successfully Canceled.")
        else:
            logger.info(f"Failed to Cancel Order due to {resp_cancel["not_canceled"]}")
            return False
    return True

async def get_bet_on_market(condition_id): # Get total $ bet on market
    trades = client.get_trades(
        TradeParams(
            maker_address=funder,
            market=condition_id,
        ),
    )
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

def record_trade(isWin, question, avg_entry, no_shares_bought, total_bet, PnL): # Save record into spreadsheet
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

async def initialize_current_active_markets(next_cursor=""): # Search for all active markets and add to watch list
    logger.info("Initializing current active markets...")
    while True:
        data = await get_polymarket_markets(next_cursor)
        for i in range(0, data["count"]):
            market = data["data"][i]
            if market.get('question', '').startswith('[Single Market]') or market['end_date_iso'] == None:
                continue
            current_time = datetime.now(timezone.utc)
            end_time = datetime.fromisoformat(market['end_date_iso'][:-1]).replace(tzinfo=timezone.utc)
            if not market.get('closed', True) and market.get('active', True) and market["tokens"][0]["token_id"] and market["tokens"][1]["token_id"] and current_time < end_time:
                add_market({
                    "condition_id": market["condition_id"],
                    "no_asset_id": market["tokens"][1]["token_id"],
                    "min_tick_size": float(market["minimum_tick_size"]),
                    "end_iso_date": market["end_date_iso"]
                })
        if data["next_cursor"] == "LTE=":
            break
        next_cursor = data["next_cursor"]
    logger.info("Finished initialization.")
    return next_cursor

async def monitor_active_markets(): # Monitor active market watch list
    logger.info("Monitoring active markets...")
    while True:
        logger.info(f"Current active markets: {len(active_markets)}")
        for market in active_markets:
            logger.info(f"{market["condition_id"]}")
            portfolio_balance = await get_balance_allowance()
            portfolio_balance = portfolio_balance * PORTFOLIO_PERCENT / 100.0

            current_bet_on_market = 0.0
            
            result = await get_bet_on_market(market["condition_id"])
            if result != None:
                current_bet_on_market, shares_bought = result
                        
            if current_bet_on_market >= portfolio_balance: # If current $ bet on the market is greather than or equal to portfolio limit size
                await cancel_orders_on_market(market["condition_id"]) # Cancel all open orders to ensure $ risked is under portfolio limit
                continue
            
            available_balance = portfolio_balance - current_bet_on_market
            
            order_book = client.get_order_book(market["no_asset_id"])
            
            highest_bid = float(order_book.bids.pop().price) if len(order_book.bids) else 100.0
            lowest_ask = float(order_book.asks.pop().price) if len(order_book.asks) else 0.0
            spread = lowest_ask - highest_bid
            min_tick_size = market["min_tick_size"]
            
            min_bid = MIN_BID / 100.0
            max_bid = MAX_BID / 100.0
            spread_limit = SPREAD_LIMIT / 100.0
            
            if lowest_ask >= min_bid and lowest_ask <= max_bid and spread < spread_limit: # Entry Condition 2
                if await cancel_orders_on_market(market["condition_id"]): # Cancel current open orders
                    limit_price = lowest_ask
                    if await create_order(limit_price, available_balance / limit_price, market["no_asset_id"], True): # Place new order
                        return # If order is placed, return so that Entry 1 cannot place new order to ensure $ risked is under portfolio limit
            
            if highest_bid >= min_bid and highest_bid < max_bid: # Entry Conditoin 1
                if await cancel_orders_on_market(market["condition_id"]): # Cancel current open orders
                    limit_price = highest_bid + min_tick_size
                    await create_order(limit_price, available_balance / limit_price, market["no_asset_id"]) # Place new order
    
async def monitor_new_markets(next_cursor): # Monitor new markets
    logger.info("Looking for new markets...")
    while True:
        data = await get_polymarket_markets(next_cursor)
        filled = True
        for i in range(0, data["count"]):
            market = data["data"][i]
            if market.get('question', '').startswith('[Single Market]') or market['end_date_iso'] == None:
                continue
            current_time = datetime.now(timezone.utc)
            end_time = datetime.fromisoformat(market['end_date_iso'][:-1]).replace(tzinfo=timezone.utc)
            if not market.get('closed', True) and market.get('active', True) and market["condition_id"] not in [market["condition_id"] for market in active_markets] and current_time < end_time:
                if market["tokens"][0]["token_id"] and market["tokens"][1]["token_id"]:
                    logger.info("=====================")
                    logger.info("New market found")
                    add_market({
                        "condition_id": market["condition_id"],
                        "no_asset_id": market["tokens"][1]["token_id"],
                        "min_tick_size": float(market["minimum_tick_size"]),
                        "end_iso_date": market["end_date_iso"]
                    })
                else:
                    filled = False
        if data["next_cursor"] != "LTE=" and filled:
            next_cursor = data["next_cursor"]
            
        # Random wait time
        wait_time = random.randint(10, 15)
        logger.info(f"Waiting for {wait_time} seconds before next market check.")
        await asyncio.sleep(wait_time)
        
async def monitor_resolved_markets():
    while True:
        if len(active_markets):
            current_time = datetime.now(timezone.utc)
            end_time = datetime.fromisoformat(active_markets[0]['end_iso_date'][:-1]).replace(tzinfo=timezone.utc) # Since watch list is sorted by end_date_iso, the first market will be resolved first
            if current_time >= end_time: # If the first active market resolved
                resolved_market = active_markets.pop(0) # Remove from the watch list
                logger.info(f"Market {resolved_market["condition_id"]} resolved.")
                resolved_market = client.get_market(resolved_market["condition_id"])
                result = await get_bet_on_market(resolved_market["condition_id"])
                if result != None:
                    total_bet, shares_bought = result
                    avg_price = total_bet / shares_bought
                    
                    isWin = resolved_market["tokens"][1]["winner"]
                    question = resolved_market["question"]
                    if isWin:
                        profitLoss = shares_bought - total_bet
                    else:
                        profitLoss = -total_bet
                        
                    record_trade(isWin, question, avg_price, shares_bought, total_bet, profitLoss) # Save record into sheet
                            
        await asyncio.sleep(10)

async def main():
    # Initialize current active markets
    next_cursor = await initialize_current_active_markets()
    
    # Monitor active markets
    asyncio.create_task(monitor_active_markets())
    
    # Monitor resolved markets
    asyncio.create_task(monitor_resolved_markets())
    
    # Start monitoring new markets
    await monitor_new_markets(next_cursor)

if __name__ == "__main__":
    asyncio.run(main())
