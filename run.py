#from web3 import Web3
import requests
import os
import asyncio
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, BalanceAllowanceParams, AssetType
from dotenv import load_dotenv
from py_clob_client.constants import AMOY
from datetime import datetime

from py_clob_client.order_builder.constants import BUY

load_dotenv()

percent2invest = 1 # Percentage of how many of your available balance to invest
order_1cent_min_amount = 1 # Minimum order price for minimum_tick_size = 1 cent
order_1cent_max_amount = 5 # Maximum order price for minimum_tick_size = 1 cent
order_decicent_min_amount = 0.1 # Minimum order price for minimum_tick_size = 1/10 cent
order_decicent_max_amount = 0.5 # Maximum order price for minimum_tick_size = 1/10 cent

host = "https://clob.polymarket.com"
key = os.getenv("PK")
chain_id = 137
client = ClobClient(host, key=key, chain_id=chain_id, signature_type=2, funder="0xAd4ECa3bE320353ef0c36EdA7820Ef29431dec67")
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds=creds)

def create_api_key():
    host = "https://clob.polymarket.com"
    key = os.getenv("PK")
    chain_id = 137
    #client = ClobClient(host, key=key, chain_id=chain_id)
    client = ClobClient(host, key=key, chain_id=chain_id, signature_type=2, funder="0xAd4ECa3bE320353ef0c36EdA7820Ef29431dec67")

    print(client.create_api_key())
    
async def get_balance_allowance():
    collateral = client.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    print(collateral)
    return float(collateral["balance"]) / 1000000.0
    
async def get_polymarket_markets(next_cursor=""):
    url = f"{host}/markets?next_cursor={next_cursor}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        return data
    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error occurred: {http_err}")
    except Exception as err:
        print(f"An error occurred: {err}")

def create_order(price, size, token_id):
    order_args = OrderArgs(
        price=price,
        size=size,
        side=BUY,
        token_id=token_id,
    )
    print(order_args)
    signed_order = client.create_order(order_args)

    resp = client.post_order(signed_order, OrderType.GTC)
    print(resp)
   
async def parse_market(market):
    market_id = market.get('condition_id', 'N/A')
    tokens = market.get('tokens', [])
    token_yes = tokens[0].get('token_id', 'N/A') if len(tokens) > 0 else 'N/A'
    token_no = tokens[1].get('token_id', 'N/A') if len(tokens) > 1 else 'N/A'
    
    min_tick_size = market.get('minimum_tick_size', 'N/A')
    if min_tick_size != 'N/A':
        min_tick_size = float(min_tick_size) * 100.0
    else:
        return
    
    print(market)
    
    balance_allowance = await get_balance_allowance()
    balance_allowance = balance_allowance * percent2invest
    
    if min_tick_size >= 1.0:
        low_cent = order_1cent_min_amount
        high_cent = order_1cent_max_amount
        step = 1
    else:
        low_cent = order_decicent_min_amount
        high_cent = order_decicent_max_amount
        step = 0.1
    
    order_count = int((high_cent - low_cent) / step) + 1
    
    cent = low_cent
    while cent <= high_cent:
        create_order(cent / 100.0, balance_allowance / order_count / cent, token_yes)
        create_order(cent / 100.0, balance_allowance / order_count / cent, token_no)
        cent += step

async def main():
    next_cursor = "MTA1MDA="
    data = None
    while True:
        data = await get_polymarket_markets(next_cursor)
        if data["next_cursor"] == "LTE=":
            break
        next_cursor = data["next_cursor"]
    current_count = data["count"]
    print("Finding new markets...")
    while True:
        data = await get_polymarket_markets(next_cursor)
        if data["count"] > current_count:
            for i in range(current_count, data["count"]):
                market = data["data"][i]
                if market.get('question', '').startswith('[Single Market]'):
                    continue
                if not market.get('closed', True) and market.get('active', True):
                    print("=====================")
                    print("New market found")
                    print(datetime.now().isoformat())
                    print(f"accepting_order_timestamp: {market["accepting_order_timestamp"]}")
                    await parse_market(market)
            current_count = data["count"]
        if data["next_cursor"] != "LTE=":
            current_count = 0
            next_cursor = data["next_cursor"]
    
asyncio.run(main())