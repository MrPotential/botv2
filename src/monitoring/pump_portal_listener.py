"""
Optimized listener for PumpPortal real-time token creation events with high-velocity detection.
"""

import asyncio
import json
import random
import time
from collections import deque
import websockets
from solders.pubkey import Pubkey
from colorama import Fore, Style
from solana.rpc.api import Client

from monitoring.base_listener import BaseTokenListener
from trading.base import TokenInfo
from utils.logger import get_logger
from utils.robust_websocket import RobustWebSocket  # Import the RobustWebSocket class

# Constants
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# Set up logger
logger = get_logger(__name__)

class PumpPortalListener(BaseTokenListener):
    """WebSocket listener for pump.fun token creation events using PumpPortal API."""

    def __init__(self, volume_threshold: float = 20.0, token_wait_timeout: float = 60.0):
        """
        Initialize the PumpPortal listener.

        Args:
            volume_threshold: Volume threshold in SOL to trigger buy.
            token_wait_timeout: Time window (seconds) to wait for volume threshold.
        """
        # Connection settings
        self.url = "wss://pumpportal.fun/api/data"
        self.active = False
        self.websocket = None

        # Trading parameters
        self.volume_threshold = volume_threshold
        self.token_wait_timeout = token_wait_timeout

        # Use faster RPC endpoint for better performance
        self.client = Client("https://api.mainnet-beta.solana.com")

        # State tracking (use sets/dicts for O(1) lookups)
        self.subscribed_tokens = set()
        self.token_volumes = {}
        self.pending_tokens = {}
        self.threshold_tasks = {}

        # High-velocity detection (optimized)
        self.trade_timestamps = {}
        self.token_trade_counts = {}
        self.velocity_threshold = 4.0  # SOL per second
        self.min_velocity_volume = 10.0  # Minimum volume before velocity can trigger
        self.last_price_updates = {}

        # Performance monitoring
        self.message_count = 0
        self.processing_times = deque(maxlen=1000)  # Store recent processing times
        self.last_stats_time = time.monotonic()

        # Account validation settings
        self.mint_check_retries = {}
        self.max_mint_retries = 3
        self.mint_retry_delay = 1.0  # Reduced delay for faster validation

        # Cache for recent tokens to prevent duplicate processing
        self.recent_tokens_cache = set()
        self.cache_cleanup_time = time.monotonic()

        # For callback storage
        self.token_callback = None
        self.match_string = None
        self.creator_address = None

        logger.info(f"PumpPortal listener initialized (threshold={volume_threshold} SOL, window={token_wait_timeout}s)")
        logger.info(f"GREEDY MODE: High-velocity detection enabled (threshold={self.velocity_threshold} SOL/sec)")

    async def listen_for_tokens(self, token_callback, match_string=None, creator_address=None):
        """
        Listen for new token creation events and trigger callback when conditions are met.

        Args:
            token_callback: Coroutine function to call with TokenInfo when buy condition is met.
            match_string: Optional substring to filter token name/symbol.
            creator_address: Optional creator address to filter tokens.
        """
        self.active = True
        self.token_callback = token_callback
        self.match_string = match_string
        self.creator_address = creator_address

        # Start periodic cleanup task
        cleanup_task = asyncio.create_task(self._periodic_cleanup())
        # Start stats reporting task
        stats_task = asyncio.create_task(self._periodic_stats())

        logger.info(f"{Fore.YELLOW}🔌 Starting PumpPortal listener with robust WebSocket...{Style.RESET_ALL}")

        try:
            # Create robust WebSocket client
            self.websocket = RobustWebSocket(
                url=self.url,
                on_message=self._process_message,  # Use existing message processor
                on_connect=self._on_websocket_connect,
                on_disconnect=self._on_websocket_disconnect,
                ping_interval=30,
                reconnect_interval_min=1,
                reconnect_interval_max=30,
                max_reconnect_attempts=0,  # Unlimited reconnection attempts
                message_timeout=30,
            )

            # Start the WebSocket client
            await self.websocket.start()

            # Keep running until stopped
            while self.active:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Error in WebSocket listener: {e}")

        finally:
            # Cancel background tasks
            for task in [cleanup_task, stats_task]:
                if task and not task.cancelled():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            # Close WebSocket connection
            if hasattr(self.websocket, 'stop') and callable(self.websocket.stop):
                await self.websocket.stop()

            logger.info("PumpPortal listener stopped.")

    async def _on_websocket_connect(self):
        """Called when WebSocket connects successfully."""
        logger.info(f"{Fore.GREEN}✓ Connected to PumpPortal WebSocket{Style.RESET_ALL}")

        # Subscribe to new token events
        await self._subscribe_to_new_tokens()

        # Resubscribe to all tracked tokens
        for token_mint in list(self.subscribed_tokens):
            symbol = self.pending_tokens[token_mint].symbol if token_mint in self.pending_tokens else "Unknown"
            await self._subscribe_to_token_trades(token_mint, symbol)

    async def _on_websocket_disconnect(self):
        """Called when WebSocket disconnects."""
        logger.info(f"{Fore.YELLOW}WebSocket disconnected{Style.RESET_ALL}")

    async def _process_message(self, data):
        """Process a single message from the websocket."""
        if not isinstance(data, dict):
            return

        # Track message processing performance
        start_time = time.monotonic()
        self.message_count += 1

        try:
            # Handle token creation events
            if (data.get("txType") == "create" or
                (data.get("type") == "token_create" and "data" in data)) and "mint" in data:

                # Extract token data
                token_data = data if data.get("txType") == "create" else data["data"]
                mint_str = token_data.get("mint")

                # Skip if we've seen this token recently (prevents duplicate processing)
                if mint_str in self.recent_tokens_cache:
                    return

                # Add to cache
                self.recent_tokens_cache.add(mint_str)

                # Clean cache if needed
                current_time = time.monotonic()
                if current_time - self.cache_cleanup_time > 300:  # Every 5 minutes
                    self.recent_tokens_cache = set()
                    self.cache_cleanup_time = current_time

                name = token_data.get("name", "") or ""
                symbol = token_data.get("symbol", "") or ""
                creator = token_data.get("creator") or token_data.get("traderPublicKey", "")

                # Log new token with clearer format
                logger.info(f"💎 NEW TOKEN: {Fore.CYAN}{name} ({symbol}){Style.RESET_ALL} | Mint: {mint_str[:10]}...")

                # Apply filters
                if self.match_string and self.match_string.lower() not in (name + symbol).lower():
                    logger.info(f"Token {symbol} doesn't match filter '{self.match_string}'. Skipping.")
                    return

                if self.creator_address and creator != self.creator_address:
                    logger.info(f"Token {symbol} creator {creator[:8]}... doesn't match filter. Skipping.")
                    return

                # Start validation as a separate task for better responsiveness
                asyncio.create_task(self._validate_and_process_token(mint_str, name, symbol, creator, token_data))

            # Handle trade events
            elif data.get("txType") in ["buy", "sell"]:
                token_mint = data.get("mint")
                if not token_mint or token_mint not in self.subscribed_tokens:
                    return

                # Process trade data
                current_time = time.monotonic()
                sol_amount = float(data.get("solAmount", 0))

                # Update volume and trade count
                self.token_volumes[token_mint] = self.token_volumes.get(token_mint, 0.0) + sol_amount
                self.token_trade_counts[token_mint] = self.token_trade_counts.get(token_mint, 0) + 1
                trade_count = self.token_trade_counts[token_mint]
                current_volume = self.token_volumes[token_mint]

                # Track trade for velocity calculation
                if token_mint not in self.trade_timestamps:
                    self.trade_timestamps[token_mint] = deque(maxlen=50)

                self.trade_timestamps[token_mint].append((current_time, sol_amount))
                self.last_price_updates[token_mint] = current_time

                # Calculate time elapsed and display stats
                first_trade_time = min(t for t, _ in self.trade_timestamps[token_mint]) if self.trade_timestamps[token_mint] else current_time
                time_elapsed = current_time - first_trade_time
                time_remaining = max(0, self.token_wait_timeout - time_elapsed)

                # Log trade info more efficiently
                should_log = (
                    trade_count <= 3 or  # Always log first 3 trades
                    trade_count % 3 == 0 or  # Then every 3rd trade
                    sol_amount >= 0.5 or  # Or significant amount
                    current_volume >= self.volume_threshold * 0.8  # Or approaching threshold
                )

                if should_log and token_mint in self.pending_tokens:
                    symbol = self.pending_tokens[token_mint].symbol
                    progress = min(100, int(100 * current_volume / self.volume_threshold))
                    # Improved progress bar
                    bar_length = 20
                    filled = int(bar_length * progress / 100)
                    bar = '█' * filled + '░' * (bar_length - filled)

                    logger.info(f"📊 {Fore.YELLOW}{symbol}{Style.RESET_ALL}: {Fore.GREEN}{current_volume:.2f} SOL{Style.RESET_ALL} | "
                            f"[{Fore.CYAN}{bar}{Style.RESET_ALL}] {time_remaining:.1f}s | {trade_count} trades")

                # Calculate velocity for high-momentum detection
                velocity = self._calculate_velocity(token_mint, current_time)
                high_velocity = velocity >= self.velocity_threshold

                if high_velocity and token_mint in self.pending_tokens:
                    logger.info(f"🚀 {Fore.RED}HIGH VELOCITY{Style.RESET_ALL} for {self.pending_tokens[token_mint].symbol}: "
                            f"{Fore.MAGENTA}{velocity:.1f} SOL/sec{Style.RESET_ALL}")

                # Check buy conditions
                await self._check_buy_conditions(token_mint, current_volume, velocity, high_velocity, time_elapsed)

        except Exception as e:
            logger.error(f"Error processing message: {e}")

        finally:
            # Track processing time for performance monitoring
            process_time = time.monotonic() - start_time
            self.processing_times.append(process_time)

    async def _subscribe_to_new_tokens(self):
        """Subscribe to token creation events."""
        if not hasattr(self.websocket, 'send') or not callable(self.websocket.send):
            logger.warning("Cannot subscribe to new tokens: WebSocket not initialized")
            return

        try:
            payload = {"method": "subscribeNewToken"}
            await self.websocket.send(payload)
            logger.info(f"{Fore.GREEN}✓ Subscribed to new token events{Style.RESET_ALL}")
        except Exception as e:
            logger.error(f"Failed to subscribe to new tokens: {e}")

    async def _subscribe_to_token_trades(self, mint_str, symbol):
        """Subscribe to trades for a token."""
        if mint_str in self.subscribed_tokens or not hasattr(self.websocket, 'send'):
            return

        sub_msg = {"method": "subscribeTokenTrade", "keys": [mint_str]}
        try:
            await self.websocket.send(sub_msg)
            self.subscribed_tokens.add(mint_str)
            logger.info(f"👂 Subscribed to trades for {symbol} ({mint_str[:8]}...)")
        except Exception as e:
            logger.error(f"Failed to subscribe to trades for {symbol}: {str(e)}")

    def _calculate_velocity(self, token_mint, current_time):
        """Calculate trading velocity (SOL/sec)."""
        velocity = 0.0

        if (token_mint in self.pending_tokens and
            len(self.trade_timestamps[token_mint]) >= 3 and
            self.token_volumes[token_mint] >= self.min_velocity_volume):

            # Get trades from the last 10 seconds
            recent_trades = [
                (t, amt) for t, amt in self.trade_timestamps[token_mint]
                if current_time - t <= 10
            ]

            if len(recent_trades) >= 2:
                recent_time = min(t for t, _ in recent_trades)
                time_span = max(current_time - recent_time, 1.0)
                recent_volume = sum(amt for _, amt in recent_trades)
                velocity = recent_volume / time_span

        return velocity

    async def _check_buy_conditions(self, token_mint, volume, velocity, high_velocity, time_elapsed):
        """Check if conditions are met to buy a token."""
        if token_mint not in self.pending_tokens:
            return

        trigger_buy = False
        trigger_reason = None

        # Check standard volume threshold
        if volume >= self.volume_threshold:
            trigger_buy = True
            trigger_reason = f"Volume threshold reached: {volume:.2f} SOL"

        # Check high velocity condition
        elif high_velocity and volume >= (self.volume_threshold * 0.5):
            trigger_buy = True
            trigger_reason = f"HIGH VELOCITY ({velocity:.1f} SOL/sec)"

        if trigger_buy:
            token_info = self.pending_tokens[token_mint]
            symbol = token_info.symbol

            # Cancel timeout task
            if token_mint in self.threshold_tasks:
                self.threshold_tasks.pop(token_mint).cancel()

            # Log the trigger with enhanced formatting
            trade_count = self.token_trade_counts[token_mint]
            logger.info(f"{Fore.GREEN}🚀 BUY TRIGGERED: {symbol} ({token_mint[:6]}...){Style.RESET_ALL}")
            logger.info(f"   Reason: {Fore.YELLOW}{trigger_reason}{Style.RESET_ALL}")
            logger.info(f"   Volume: {volume:.2f} SOL | Time: {time_elapsed:.1f}s | Trades: {trade_count}")

            # Execute callback - the actual quality checks will happen in the trader class
            try:
                await self.token_callback(token_info)
            except Exception as e:
                logger.error(f"Error executing callback for {symbol}: {str(e)}")

            # Clean up token data
            await self.unsubscribe_token(token_mint)

    async def _volume_timeout(self, token_mint: str):
        """Handle timeout for volume threshold."""
        try:
            await asyncio.sleep(self.token_wait_timeout)
        except asyncio.CancelledError:
            return

        if token_mint in self.pending_tokens:
            token_info = self.pending_tokens.pop(token_mint)
            symbol = token_info.symbol
            volume = self.token_volumes.get(token_mint, 0.0)
            logger.info(f"⏱️ {Fore.YELLOW}Timeout{Style.RESET_ALL}: {symbol} - Volume: {volume:.2f} SOL (below threshold)")
            await self.unsubscribe_token(token_mint)

    async def _periodic_cleanup(self):
        """Periodically clean up stale data."""
        while self.active:
            try:
                await asyncio.sleep(30)
                await self.cleanup_stale_data()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {str(e)}")

    async def _periodic_stats(self):
        """Periodically log performance statistics."""
        while self.active:
            try:
                await asyncio.sleep(60)  # Log stats every minute

                current_time = time.monotonic()
                active_tokens = len(self.pending_tokens)
                subscribed_tokens = len(self.subscribed_tokens)

                if self.processing_times:
                    avg_time = sum(self.processing_times) / len(self.processing_times) * 1000
                    max_time = max(self.processing_times) * 1000 if self.processing_times else 0
                else:
                    avg_time = 0
                    max_time = 0

                logger.info(f"📊 Performance: {self.message_count} messages | " 
                          f"Active tokens: {active_tokens} | "
                          f"Avg process time: {avg_time:.1f}ms")

                # Reset for next period
                self.processing_times.clear()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic stats: {str(e)}")

    async def cleanup_stale_data(self):
        """Clean up data for tokens that haven't seen updates."""
        current_time = time.monotonic()
        tokens_to_cleanup = []

        for mint, last_update in list(self.last_price_updates.items()):
            if current_time - last_update > self.token_wait_timeout * 1.5:
                tokens_to_cleanup.append(mint)

        for mint in tokens_to_cleanup:
            await self.unsubscribe_token(mint)

        if tokens_to_cleanup and len(tokens_to_cleanup) > 3:
            logger.debug(f"Cleaned up {len(tokens_to_cleanup)} stale tokens")

    async def unsubscribe_token(self, token_mint: str):
        """Unsubscribe from trade events and clean up token data."""
        if token_mint not in self.subscribed_tokens:
            return

        # Safely send unsubscribe message if connection exists
        if self.websocket and hasattr(self.websocket, 'send'):
            try:
                await self.websocket.send({
                    "method": "unsubscribeTokenTrade",
                    "keys": [token_mint]
                })
            except Exception:
                pass  # Ignore errors during unsubscribe

        # Clean up all data for this token
        self.subscribed_tokens.discard(token_mint)
        self.token_volumes.pop(token_mint, None)
        self.pending_tokens.pop(token_mint, None)
        self.trade_timestamps.pop(token_mint, None)
        self.token_trade_counts.pop(token_mint, None)
        self.last_price_updates.pop(token_mint, None)

        if token_mint in self.threshold_tasks:
            task = self.threshold_tasks.pop(token_mint)
            if not task.done():
                task.cancel()

    async def close_connection(self):
        """Close the WebSocket connection gracefully."""
        if not self.websocket or self.websocket.closed:
            return

        try:
            await asyncio.wait_for(self.websocket.close(), timeout=2.0)
            logger.info(f"{Fore.YELLOW}WebSocket connection closed gracefully{Style.RESET_ALL}")
        except Exception:
            pass
        finally:
            self.websocket = None
            self.active = False

    async def stop(self):
        """Stop the listener."""
        logger.info("Stopping PumpPortal listener...")
        self.active = False

        # Cancel threshold tasks
        for token_mint, task in list(self.threshold_tasks.items()):
            if not task.done():
                task.cancel()

        # Close WebSocket connection gracefully
        if self.websocket:
            if hasattr(self.websocket, 'stop') and callable(self.websocket.stop):
                await self.websocket.stop()

        logger.info(f"{Fore.YELLOW}PumpPortal listener stopped.{Style.RESET_ALL}")