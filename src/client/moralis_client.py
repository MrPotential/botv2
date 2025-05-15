# src/client/moralis_client.py

import aiohttp
import asyncio
import json
import time
from typing import Dict, List, Optional, Any
from utils.logger import get_logger
import os

logger = get_logger(__name__)

class MoralisClient:
    """Client for interacting with Moralis API for Solana token data."""
    
    def __init__(self, api_key: str = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6ImZkYmEzZDg5LWRjNTUtNGFkMS05MTU5LTY1ZTkzN2MyMThkYyIsIm9yZ0lkIjoiNDQ2NjI1IiwidXNlcklkIjoiNDU5NTIwIiwidHlwZUlkIjoiZWIwMzlmOTctMzkxNC00NWVkLTk3OWEtMDJhNDhmMzAzNTAyIiwidHlwZSI6IlBST0pFQ1QiLCJpYXQiOjE3NDcwNDU1MDEsImV4cCI6NDkwMjgwNTUwMX0.Lnv4DIOoNLWCM5sQ4XpucXcRf6dgdkMEUvzdZIHUDkg"):
        """Initialize with hardcoded API key"""
        self.api_key = api_key
        self.base_url = "https://solana-gateway.moralis.io"
        self.network = "mainnet"
        self.session = None
        self.request_count = 0
        self.last_request_time = 0
        self.rate_limit = 1.0  # seconds between requests
        self.enabled = bool(self.api_key)
        
        # Cache for trade data to reduce API calls
        self._trade_cache = {}
        self._cache_ttl = 10  # seconds
        
        if not self.enabled:
            logger.warning("Moralis client initialized without API key. Analysis features will be disabled.")
        else:
            logger.info(f"Moralis client initialized with API key. Analysis features enabled.")
    
    async def ensure_session(self):
        """Ensure we have an active aiohttp session."""
        if not self.enabled:
            return False
            
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"X-API-Key": self.api_key, "accept": "application/json"}
            )
        return True
    
    async def close(self):
        """Close the aiohttp session."""
        if self.session and not self.session.closed:
            await self.session.close()
            
    async def _make_request(self, endpoint: str, params: Dict = None) -> Dict:
        """Make a rate-limited request to Moralis API with error handling."""
        if not self.enabled or not await self.ensure_session():
            return {"error": "Client disabled or session initialization failed"}
        
        # Rate limiting
        now = time.time()
        if self.last_request_time > 0:
            elapsed = now - self.last_request_time
            if elapsed < self.rate_limit:
                await asyncio.sleep(self.rate_limit - elapsed)
        
        url = f"{self.base_url}/{endpoint}"
        logger.debug(f"Making Moralis API request to: {url}")
        
        try:
            self.last_request_time = time.time()
            self.request_count += 1
            
            async with self.session.get(url, params=params, timeout=5.0) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.debug(f"Moralis API response: {json.dumps(data)[:500]}...")
                    return data
                else:
                    error_text = await response.text()
                    logger.error(f"Moralis API error ({response.status}): {error_text}")
                    return {"error": error_text, "status": response.status}
                    
        except asyncio.TimeoutError:
            logger.error(f"Moralis API timeout for {endpoint}")
            return {"error": "Request timed out"}
        except Exception as e:
            logger.error(f"Error making Moralis request to {endpoint}: {e}")
            return {"error": str(e)}
    
    async def get_token_metadata(self, mint_address: str) -> Dict:
        """Get token metadata using the format from documentation."""
        try:
            # Use the correct endpoint format: token/{network}/{address}/metadata
            endpoint = f"token/{self.network}/{mint_address}/metadata"
            return await self._make_request(endpoint)
        except Exception as e:
            logger.error(f"Error getting token metadata: {e}")
            return {"error": str(e)}
    
    async def get_token_price(self, mint_address: str) -> Dict:
        """Get token price using the format from documentation."""
        try:
            # Use the correct endpoint format: token/{network}/{address}/price
            endpoint = f"token/{self.network}/{mint_address}/price"
            return await self._make_request(endpoint)
        except Exception as e:
            logger.error(f"Error getting token price: {e}")
            return {"error": str(e)}
    
    async def get_token_transfers(self, mint_address: str, limit: int = 50) -> Dict:
        """Get token transfers using the format from documentation."""
        try:
            # Use the correct endpoint format: account/{network}/{address}/transfers
            endpoint = f"account/{self.network}/{mint_address}/transfers"
            params = {"limit": limit}
            return await self._make_request(endpoint, params)
        except Exception as e:
            logger.error(f"Error getting token transfers: {e}")
            return {"error": str(e)}
    
    async def get_token_spl_transfers(self, mint_address: str, limit: int = 50) -> Dict:
        """Get SPL token transfers."""
        try:
            # Use the correct endpoint format: account/{network}/{address}/spl-transfers
            endpoint = f"account/{self.network}/{mint_address}/spl-transfers"
            params = {"limit": limit}
            return await self._make_request(endpoint, params)
        except Exception as e:
            logger.error(f"Error getting SPL transfers: {e}")
            return {"error": str(e)}
    
    async def get_wallet_spl_tokens(self, wallet_address: str, token_address: str = None) -> Dict:
        """Get SPL token balances for a wallet."""
        try:
            # Use the correct endpoint format: account/{network}/{address}/spl
            endpoint = f"account/{self.network}/{wallet_address}/spl"
            params = {}
            if token_address:
                params["token_addresses"] = token_address
            return await self._make_request(endpoint, params)
        except Exception as e:
            logger.error(f"Error getting wallet SPL tokens: {e}")
            return {"error": str(e)}
            
    async def get_new_pumpfun_tokens(self, limit: int = 100, cursor: str = None) -> Dict:
        """Get newly created Pump.fun tokens."""
        try:
            # Use the Pump.fun endpoint from the documentation
            endpoint = f"token/{self.network}/exchange/pumpfun/new"
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            return await self._make_request(endpoint, params)
        except Exception as e:
            logger.error(f"Error getting new Pump.fun tokens: {e}")
            return {"error": str(e)}
    
    async def get_token_volume_data(self, mint_address: str) -> Dict[str, Any]:
        """
        Get accurate volume data for a token using a combination of Moralis endpoints.
        This aggregates data from price and transfer endpoints.
        """
        try:
            # Get price data which includes exchange and liquidity info
            price_data = await self.get_token_price(mint_address)
            
            # Get transfer data to analyze volume and activity
            transfers_data = await self.get_token_transfers(mint_address, 100)
            
            # Default response
            result = {
                "verified": False,
                "volume_sol": 0.0,
                "trade_count": 0,
                "unique_wallets": 0,
            }
            
            # Check if we have price data
            if price_data and not ("error" in price_data):
                result["price_usd"] = price_data.get("usdPrice", 0)
                result["exchange"] = price_data.get("exchangeName", "Unknown")
                result["verified"] = True
            
            # Process transfer data if available
            if transfers_data and "result" in transfers_data:
                transfers = transfers_data.get("result", [])
                
                # Calculate volume and unique wallets
                volume_sol = 0.0
                unique_wallets = set()
                
                for transfer in transfers:
                    # Try to extract SOL amount if available
                    amount = float(transfer.get("amount", 0))
                    value_usd = float(transfer.get("value", 0))
                    
                    # If we have price data and transfer amount, compute SOL value
                    if amount > 0 and "price_usd" in result and result["price_usd"] > 0:
                        # Estimate SOL value from USD value and SOL price (~$100)
                        sol_value = value_usd / 100  # Approximate SOL price
                        volume_sol += sol_value
                    
                    # Track unique wallets
                    from_addr = transfer.get("from_address")
                    to_addr = transfer.get("to_address")
                    if from_addr:
                        unique_wallets.add(from_addr)
                    if to_addr:
                        unique_wallets.add(to_addr)
                
                result["volume_sol"] = volume_sol
                result["trade_count"] = len(transfers)
                result["unique_wallets"] = len(unique_wallets)
                result["verified"] = True
            
            return result
                
        except Exception as e:
            logger.error(f"Error getting volume data: {e}")
            return {
                "verified": False,
                "volume_sol": 0.0,
                "trade_count": 0,
                "error": str(e)
            }
    
    async def analyze_token(self, mint_address: str) -> Dict[str, Any]:
        """
        Analyze a token with the updated Moralis endpoints.
        Returns metrics or error information.
        """
        # Exit early if client disabled
        if not self.enabled:
            return {"score": 0, "error": "Moralis client disabled", "enabled": False}
            
        try:
            # Gather data from various endpoints
            tasks = [
                self.get_token_metadata(mint_address),
                self.get_token_price(mint_address),
                self.get_token_transfers(mint_address, 100)
            ]
            
            # Execute requests in parallel
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            metadata = results[0] if not isinstance(results[0], Exception) else None
            price_info = results[1] if not isinstance(results[1], Exception) else None
            transfers = results[2] if not isinstance(results[2], Exception) else None
            
            # Extract volume metrics from transfers data
            volume_sol = 0.0
            swap_count = 0
            unique_wallet_count = 0
            
            if transfers and "result" in transfers:
                transfer_list = transfers["result"]
                swap_count = len(transfer_list)
                
                # Calculate unique wallets
                wallets = set()
                for tx in transfer_list:
                    from_addr = tx.get("from_address")
                    to_addr = tx.get("to_address")
                    if from_addr:
                        wallets.add(from_addr)
                    if to_addr:
                        wallets.add(to_addr)
                unique_wallet_count = len(wallets)
                
                # Estimate volume if price data is available
                if price_info and "usdPrice" in price_info:
                    usd_price = float(price_info["usdPrice"])
                    # Calculate estimated SOL price (assume SOL = $100 for simplicity)
                    sol_price = usd_price / 100
                    
                    # Estimate volume based on transfers and price
                    for tx in transfer_list:
                        amount = float(tx.get("amount", 0))
                        if amount > 0:
                            volume_sol += amount * sol_price
            
            # Get price data
            usd_price = 0
            exchange = "Unknown"
            if price_info and "usdPrice" in price_info:
                usd_price = float(price_info["usdPrice"])
                exchange = price_info.get("exchangeName", "Unknown")
            
            # Calculate scores (0-100)
            volume_score = min(100, int(volume_sol * 5))  # 20 SOL = 100 score
            activity_score = min(100, swap_count * 2)  # 50 swaps = 100 score
            unique_users_score = min(100, unique_wallet_count * 10)  # 10 unique wallets = 100 score
            
            # Combined score - weighted average
            final_score = int(
                volume_score * 0.5 +        # Volume is most important
                activity_score * 0.3 +      # Activity is important
                unique_users_score * 0.2    # Unique users
            )
            
            # Identify red flags
            red_flags = []
            
            if volume_sol < 3.0:
                red_flags.append(f"Low volume ({volume_sol:.2f} SOL)")
                
            if swap_count < 3:
                red_flags.append(f"Few transfers ({swap_count})")
                
            if unique_wallet_count < 3:
                red_flags.append(f"Few unique wallets ({unique_wallet_count})")
            
            return {
                "score": final_score,
                "verified_volume_sol": volume_sol,
                "verified_swap_count": swap_count,
                "unique_wallets": unique_wallet_count,
                "price_usd": usd_price,
                "exchange": exchange,
                "metadata": metadata,
                "price_info": price_info,
                "enabled": True,
                "verified": True if (price_info or (transfers and "result" in transfers)) else False,
                "red_flags": red_flags
            }
            
        except Exception as e:
            logger.error(f"Error analyzing token {mint_address}: {e}")
            return {"score": 0, "error": str(e), "enabled": True, "verified": False}
