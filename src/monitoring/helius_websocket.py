import asyncio
import json
import base64
import websockets
from solders.pubkey import Pubkey
from typing import Dict, Callable, Coroutine, Any, Optional, List
import time

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
        self.subscription_lock = asyncio.Lock()
        self.message_queue = asyncio.Queue()  # Queue for incoming messages
        self.pending_responses = {}  # Track pending request IDs waiting for responses
        self.processor_task = None

    async def connect(self):
        """
        Establish connection to Helius WebSocket API.

        Returns:
            bool: True if connection task was started successfully, False otherwise
        """
        if self.connection_task and not self.connection_task.done():
            return True

        try:
            self.active = True
            self.connection_task = asyncio.create_task(self._maintain_connection())
            if not self.processor_task or self.processor_task.done():
                self.processor_task = asyncio.create_task(self._process_message_queue())

            logger.info("Created Helius WebSocket connection task")
            return True
        except Exception as e:
            logger.error(f"Failed to start Helius WebSocket connection task: {e}")
            return False

    async def _maintain_connection(self):
        """Maintain the WebSocket connection with auto-reconnect."""
        while self.active:
            try:
                async with websockets.connect(self.url) as websocket:
                    self.websocket = websocket
                    logger.info(f"Connected to Helius WebSocket at {self.url}")

                    # Resubscribe to active subscriptions after reconnect
                    await self._resubscribe()

                    # Listen for messages and push to queue
                    while self.active:
                        try:
                            message = await websocket.recv()
                            await self.message_queue.put(message)
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

    async def _process_message_queue(self):
        """Process messages from the queue."""
        while self.active:
            try:
                # Get message from queue with timeout
                try:
                    message = await asyncio.wait_for(self.message_queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    continue

                try:
                    data = json.loads(message)

                    # Handle subscription confirmation or RPC response
                    if "id" in data and "result" in data:
                        req_id = data["id"]
                        result = data["result"]

                        # Check if this is a response to a pending request
                        if req_id in self.pending_responses:
                            future = self.pending_responses.pop(req_id)
                            if not future.done():
                                future.set_result(result)

                    # Handle subscription update
                    elif "method" in data and data["method"] == "accountNotification":
                        sub_id = data["params"]["subscription"]
                        account_data = data["params"]["result"]["value"]

                        # FIXED: Process the data before passing to callback
                        # Convert data from base64 to raw bytes if needed for bonding curve processing
                        if account_data and "data" in account_data:
                            if isinstance(account_data["data"], list) and len(account_data["data"]) >= 2:
                                # Check if the first element is "base64" encoding specifier
                                if account_data["data"][0] == "base64":
                                    try:
                                        # Convert base64 to bytes for BondingCurveState
                                        raw_bytes = base64.b64decode(account_data["data"][1])
                                        account_data["data"] = raw_bytes
                                    except Exception as e:
                                        logger.error(f"Error decoding base64 data: {e}")

                        # Call the registered callback for this subscription
                        if sub_id in self.callbacks:
                            callback = self.callbacks[sub_id]
                            asyncio.create_task(callback(account_data))
                except json.JSONDecodeError:
                    logger.error("Received invalid JSON on Helius WebSocket")
                except Exception as e:
                    logger.error(f"Error processing Helius message: {e}")

                # Mark task as done
                self.message_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in message processor: {e}")
                await asyncio.sleep(0.5)

    async def _resubscribe(self):
        """Resubscribe to all active subscriptions after reconnect."""
        # Clone dict to avoid modification during iteration
        subscriptions = dict(self.subscription_ids)
        self.subscription_ids = {}

        logger.info(f"Resubscribing to {len(subscriptions)} accounts")

        for pubkey, sub_id in subscriptions.items():
            # Find the callback for this subscription
            callback = None
            for sid, cb in self.callbacks.items():
                if sid == sub_id:
                    callback = cb
                    break

            if callback:
                # Resubscribe with the same callback
                try:
                    await self.accountSubscribe(Pubkey.from_string(pubkey), callback)
                    await asyncio.sleep(0.1)  # Small delay between resubscribes
                except Exception as e:
                    logger.error(f"Error resubscribing to {pubkey}: {e}")

    async def _send_request(self, request_data):
        """
        Send a request and wait for the response.

        Args:
            request_data: The JSON-RPC request data

        Returns:
            The result from the response, or None if there was an error
        """
        if not self.websocket:
            logger.error("Cannot send request: WebSocket not connected")
            return None

        req_id = request_data["id"]

        # Create a future to receive the response
        response_future = asyncio.Future()
        self.pending_responses[req_id] = response_future

        # Send the request
        try:
            await self.websocket.send(json.dumps(request_data))

            # Wait for response with timeout
            try:
                result = await asyncio.wait_for(response_future, timeout=5)  # Reduced timeout to 5 seconds
                return result
            except asyncio.TimeoutError:
                logger.error(f"Timeout waiting for response to request {req_id}")
                return None
        except Exception as e:
            logger.error(f"Error sending WebSocket request: {e}")
            return None
        finally:
            # Clean up
            self.pending_responses.pop(req_id, None)

    async def accountSubscribe(self, account: Pubkey, callback: Callable[[dict], Coroutine[Any, Any, None]]) -> \
    Optional[str]:
        """
        Subscribe to an account for updates.

        Args:
            account: Pubkey of the account to monitor
            callback: Async function to call with account data when updates occur

        Returns:
            Subscription ID if successful, None otherwise
        """
        if not self.websocket:
            logger.error("Cannot subscribe: WebSocket not connected")
            return None

        request_id = abs(hash(str(account)) % 1000000000000000000)  # More reliable request ID generation
        account_str = str(account)

        # Check if already subscribed
        if account_str in self.subscription_ids:
            logger.info(f"Already subscribed to {account_str[:8]}...{account_str[-4:]}")
            return self.subscription_ids[account_str]

        try:
            async with self.subscription_lock:
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

                # Send request and wait for response
                sub_id = await self._send_request(request)

                if sub_id:
                    self.subscription_ids[account_str] = sub_id
                    self.callbacks[sub_id] = callback
                    logger.info(f"Subscribed to account {account_str[:8]}...{account_str[-4:]} with ID {sub_id}")
                    return sub_id
                else:
                    logger.error(f"Failed to subscribe to account {account_str}")
                    return None

        except Exception as e:
            logger.error(f"Error subscribing to account {account_str}: {e}")
            return None

    async def accountUnsubscribe(self, account: Pubkey) -> bool:
        """
        Unsubscribe from an account.

        Args:
            account: Pubkey of the account to unsubscribe from
        """
        account_str = str(account)
        if account_str not in self.subscription_ids or not self.websocket:
            return False

        sub_id = self.subscription_ids[account_str]

        try:
            async with self.subscription_lock:
                request = {
                    "jsonrpc": "2.0",
                    "id": abs(hash(f"unsub_{account_str}") % 1000000000000000000),
                    "method": "accountUnsubscribe",
                    "params": [sub_id]
                }

                result = await self._send_request(request)

                # Remove from tracking regardless of result
                self.subscription_ids.pop(account_str, None)
                self.callbacks.pop(sub_id, None)

                if result:
                    logger.info(f"Unsubscribed from account {account_str[:8]}...{account_str[-4:]}")
                    return True
                else:
                    logger.warning(f"Failed to unsubscribe from account {account_str[:8]}...{account_str[-4:]}")
                    return False

        except Exception as e:
            logger.error(f"Error unsubscribing from account {account_str}: {e}")
            return False

    def stop(self):
        """Stop the client and close connection."""
        self.active = False

        # Cancel tasks
        if self.processor_task:
            self.processor_task.cancel()

        if self.connection_task:
            self.connection_task.cancel()

        # Clear data structures
        self.subscription_ids.clear()
        self.callbacks.clear()
        self.pending_responses.clear()

        logger.info("Helius WebSocket client stopping...")