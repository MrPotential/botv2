# src/monitoring/helius_websocket.py

import asyncio
import json
import websockets
from solders.pubkey import Pubkey
from typing import Dict, Callable, Coroutine, Any

from utils.logger import get_logger

logger = get_logger(__name__)

class HeliusWebSocketClient:
    """WebSocket client for Helius API to monitor accounts and transactions."""
    
    def __init__(self, helius_url: str):
        """
        Initialize the Helius WebSocket client.
        
        Args:
            helius_url: Helius WebSocket URL with API key
        """
        self.url = helius_url
        self.active = False
        self.websocket = None
        self.subscription_ids = {}  # Map pubkeys to subscription IDs
        self.callbacks = {}  # Map subscription IDs to callbacks
        self.connection_task = None
    
    async def connect(self):
        """Establish connection to Helius WebSocket API."""
        if self.connection_task and not self.connection_task.done():
            return
        
        self.active = True
        self.connection_task = asyncio.create_task(self._maintain_connection())
    
    async def _maintain_connection(self):
        """Maintain the WebSocket connection with auto-reconnect."""
        while self.active:
            try:
                async with websockets.connect(self.url) as websocket:
                    self.websocket = websocket
                    logger.info(f"Connected to Helius WebSocket at {self.url}")
                    
                    # Resubscribe to active subscriptions after reconnect
                    await self._resubscribe()
                    
                    # Listen for messages
                    while self.active:
                        try:
                            message = await websocket.recv()
                            await self._process_message(message)
                        except websockets.exceptions.ConnectionClosed:
                            logger.warning("Helius WebSocket connection closed, reconnecting...")
                            break
                        except Exception as e:
                            logger.error(f"Error receiving Helius message: {e}")
                            continue
                
                self.websocket = None
            except Exception as e:
                logger.error(f"Helius connection error: {e}")
            
            if self.active:
                logger.info("Reconnecting to Helius WebSocket in 5 seconds...")
                await asyncio.sleep(5)
    
    async def _process_message(self, message: str):
        """Process incoming WebSocket messages."""
        try:
            data = json.loads(message)
            
            # Handle subscription confirmation
            if "id" in data and "result" in data:
                sub_id = data["result"]
                req_id = data["id"]
                logger.debug(f"Subscription confirmed with ID: {sub_id}")
                # We might want to store this mapping
                return
            
            # Handle subscription update
            if "method" in data and data["method"] == "accountNotification":
                sub_id = data["params"]["subscription"]
                account_data = data["params"]["result"]["value"]
                
                # Call the registered callback for this subscription
                if sub_id in self.callbacks:
                    callback = self.callbacks[sub_id]
                    await callback(account_data)
        except json.JSONDecodeError:
            logger.error("Received invalid JSON on Helius WebSocket")
        except Exception as e:
            logger.error(f"Error processing Helius message: {e}", exc_info=True)
    
    async def _resubscribe(self):
        """Resubscribe to all active subscriptions after reconnect."""
        # Clone dict to avoid modification during iteration
        subscriptions = dict(self.subscription_ids)
        self.subscription_ids = {}
        
        for pubkey, sub_id in subscriptions.items():
            # Find the callback for this subscription
            callback = None
            for sid, cb in self.callbacks.items():
                if sid == sub_id:
                    callback = cb
                    break
            
            if callback:
                # Resubscribe with the same callback
                await self.accountSubscribe(pubkey, callback)
    
    async def accountSubscribe(self, account: Pubkey, callback: Callable[[dict], Coroutine[Any, Any, None]]):
        """
        Subscribe to an account for updates.
        
        Args:
            account: Pubkey of the account to monitor
            callback: Async function to call with account data when updates occur
        """
        if not self.websocket:
            logger.error("Cannot subscribe: WebSocket not connected")
            return None
        
        request_id = hash(str(account))
        account_str = str(account)
        
        try:
            # Create subscription request
            request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "accountSubscribe",
                "params": [
                    account_str,
                    {"encoding": "base64", "commitment": "confirmed"}
                ]
            }
            
            # Send subscription request
            await self.websocket.send(json.dumps(request))
            
            # Wait for subscription confirmation
            response = await asyncio.wait_for(self.websocket.recv(), timeout=10)
            data = json.loads(response)
            
            if "result" in data:
                sub_id = data["result"]
                self.subscription_ids[account_str] = sub_id
                self.callbacks[sub_id] = callback
                logger.info(f"Subscribed to account {account_str[:8]}...{account_str[-4:]} with ID {sub_id}")
                return sub_id
            else:
                logger.error(f"Failed to subscribe to account {account_str}: {data}")
                return None
        
        except Exception as e:
            logger.error(f"Error subscribing to account {account_str}: {e}")
            return None
    
    async def accountUnsubscribe(self, account: Pubkey):
        """
        Unsubscribe from an account.
        
        Args:
            account: Pubkey of the account to unsubscribe from
        """
        account_str = str(account)
        if account_str not in self.subscription_ids:
            return False
        
        sub_id = self.subscription_ids[account_str]
        
        try:
            request = {
                "jsonrpc": "2.0",
                "id": hash(f"unsub_{account_str}"),
                "method": "accountUnsubscribe",
                "params": [sub_id]
            }
            
            await self.websocket.send(json.dumps(request))
            
            # Remove from tracking
            self.subscription_ids.pop(account_str, None)
            self.callbacks.pop(sub_id, None)
            
            logger.info(f"Unsubscribed from account {account_str[:8]}...{account_str[-4:]}")
            return True
        
        except Exception as e:
            logger.error(f"Error unsubscribing from account {account_str}: {e}")
            return False
    
    def stop(self):
        """Stop the client and close connection."""
        self.active = False
        self.subscription_ids.clear()
        self.callbacks.clear()
        logger.info("Helius WebSocket client stopping...")
