"""
Main trading coordinator for pump.fun tokens.
Volume-based trading strategy with clean terminal output.
"""

import asyncio
import json
import os
import logging
from datetime import datetime
from time import monotonic
from typing import Dict, Set, Callable, List, Optional, Tuple

import uvloop
import colorama
from colorama import Fore, Style
from solders.pubkey import Pubkey

from cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)
from core.client import SolanaClient
from core.curve import BondingCurveManager
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import PumpAddresses, SystemAddresses
from core.wallet import Wallet
from monitoring.block_listener import BlockListener
from monitoring.geyser_listener import GeyserListener
from monitoring.logs_listener import LogsListener
from trading.base import TokenInfo, TradeResult
from trading.buyer import TokenBuyer
from trading.seller import TokenSeller
from utils.logger import get_logger

# Configure detailed logging to file
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("pump_bot_debug.log"),
        logging.StreamHandler()
    ]
)

# Initialize colorama for cross-platform colored terminal output
colorama.init()

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

# Configure logging - HIDE HTTP REQUEST LOGS
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = get_logger(__name__)

# Improved PumpPortal listener that subscribes to token trade data
class PumpPortalListener:
    """Listens to PumpPortal WebSocket API for token creation and trade events."""

    def __init__(self, endpoint="wss://pumpportal.fun/api/data"):
        """Initialize PumpPortal listener.
        
        Args:
            endpoint: PumpPortal WebSocket API endpoint
        """
        self.endpoint = endpoint
        self.websocket = None
        self.callback = None
        self.match_string = None
        self.bro_address = None
        self.subscribed_tokens = set()
        self.token_volumes = {}
        self.running = False  # Start as not running
        
        # Diagnostic state
        self.messages_received = 0
        self.token_creation_events = 0
        self.trade_events = 0
        
        # Use simple logger for cleaner output
        self.logger = get_logger(__name__)
    
    async def listen_for_tokens(
        self,
        callback: Callable[[TokenInfo], None],
        match_string: str = None,
        bro_address: str = None,
    ) -> None:
        """Listen for new token events."""
        print(f"{Fore.YELLOW}🔌 Starting PumpPortal listener...{Style.RESET_ALL}")
        self.callback = callback
        self.match_string = match_string
        self.bro_address = bro_address
        self.running = True  # Set to running when method is called
        
        import websockets
        retry_count = 0
        max_retries = 10
        
        while retry_count < max_retries and self.running:
            try:
                print(f"{Fore.CYAN}⚡ Connecting to PumpPortal WebSocket...{Style.RESET_ALL}")
                
                async with websockets.connect(self.endpoint) as self.websocket:
                    print(f"{Fore.GREEN}✓ Connected to PumpPortal WebSocket{Style.RESET_ALL}")
                    
                    # Subscribe to token creation events
                    await self._subscribe_to_new_tokens()
                    print(f"{Fore.YELLOW}Listening for token events...{Style.RESET_ALL}")
                    
                    # Listen for incoming messages
                    last_message_time = monotonic()
                    
                    while self.running:
                        try:
                            # Set timeout to detect connection issues
                            message = await asyncio.wait_for(self.websocket.recv(), timeout=30)
                            last_message_time = monotonic()
                            self.messages_received += 1
                            
                            # Process message
                            data = json.loads(message)
                            
                            # Print diagnostic stats every 10 messages
                            if self.messages_received % 10 == 0:
                                print(f"{Fore.CYAN}📊 Stats: {self.messages_received} messages | {self.token_creation_events} tokens | {self.trade_events} trades{Style.RESET_ALL}")
                            
                            # Identify message type based on txType first
                            tx_type = data.get("txType")
                            
                            if tx_type == "create":
                                # Token creation event
                                self.token_creation_events += 1
                                token_mint = data.get("mint")
                                name = data.get("name", "")
                                symbol = data.get("symbol", "")
                                
                                print(f"{Fore.GREEN}💎 New token: {symbol} ({token_mint[:8]}...){Style.RESET_ALL}")
                                await self._handle_new_token(data)
                                
                            elif tx_type in ["buy", "sell"]:
                                # Handle trade message
                                self.trade_events += 1
                                await self._handle_token_trade(data)
                                
                            elif "message" in data:
                                # Service message
                                print(f"{Fore.YELLOW}Service message: {data.get('message', 'Unknown')}{Style.RESET_ALL}")
                                
                            elif "signature" in data and "mint" in data and self.messages_received <= 5:
                                # Just log a few signature messages for debugging
                                sig = data.get("signature", "")[:10]
                                print(f"{Fore.YELLOW}Transaction: {sig}... (Monitoring these for mint data){Style.RESET_ALL}")
                                
                        except asyncio.TimeoutError:
                            current_time = monotonic()
                            idle_time = current_time - last_message_time
                            print(f"{Fore.RED}⚠️ No messages for {idle_time:.1f} seconds, checking connection...{Style.RESET_ALL}")
                            
                            try:
                                pong_waiter = await self.websocket.ping()
                                await asyncio.wait_for(pong_waiter, timeout=5)
                                print(f"{Fore.GREEN}✓ WebSocket ping succeeded{Style.RESET_ALL}")
                            except:
                                print(f"{Fore.RED}✗ WebSocket ping failed, reconnecting...{Style.RESET_ALL}")
                                # Break inner loop to reconnect
                                break
                                
                        except json.JSONDecodeError:
                            print(f"{Fore.RED}✗ Invalid JSON received{Style.RESET_ALL}")
                            
                        except Exception as e:
                            print(f"{Fore.RED}✗ Error processing message: {e}{Style.RESET_ALL}")
                
                # If we're here, the connection was lost
                retry_count += 1
                print(f"{Fore.YELLOW}Connection lost. Retry {retry_count}/{max_retries}...{Style.RESET_ALL}")
                await asyncio.sleep(5)  # Wait before reconnecting
                
            except Exception as e:
                retry_count += 1
                print(f"{Fore.RED}✗ WebSocket error: {e!s}. Retry {retry_count}/{max_retries}...{Style.RESET_ALL}")
                await asyncio.sleep(5)  # Wait before reconnecting
        
        if not self.running:
            print(f"{Fore.YELLOW}Listener stopped by request{Style.RESET_ALL}")
        else:
            print(f"{Fore.RED}✗ Maximum retries reached.{Style.RESET_ALL}")
    
    async def _subscribe_to_new_tokens(self) -> None:
        """Subscribe to new token creation events."""
        if not self.websocket:
            return
            
        payload = {"method": "subscribeNewToken"}
        await self.websocket.send(json.dumps(payload))
        print(f"{Fore.GREEN}✓ Subscribed to new token events{Style.RESET_ALL}")
    
    async def _subscribe_to_token_trades(self, token_mint: str) -> None:
        """Subscribe to trade events for a specific token.
        
        Args:
            token_mint: Token mint address
        """
        if not self.websocket or token_mint in self.subscribed_tokens:
            return
            
        payload = {
            "method": "subscribeTokenTrade",
            "keys": [token_mint]
        }
        await self.websocket.send(json.dumps(payload))
        self.subscribed_tokens.add(token_mint)
        print(f"{Fore.CYAN}📊 Subscribed to trades for: {token_mint[:8]}...{Style.RESET_ALL}")
    
    async def _unsubscribe_from_token_trades(self, token_mint: str) -> None:
        """Unsubscribe from trade events for a specific token.
        
        Args:
            token_mint: Token mint address
        """
        if not self.websocket or token_mint not in self.subscribed_tokens:
            return
            
        payload = {
            "method": "unsubscribeTokenTrade",
            "keys": [token_mint]
        }
        await self.websocket.send(json.dumps(payload))
        self.subscribed_tokens.discard(token_mint)
        print(f"{Fore.YELLOW}🛑 Unsubscribed from trades for: {token_mint[:8]}...{Style.RESET_ALL}")
    
    async def _handle_new_token(self, data: dict) -> None:
        """Handle new token detection event.
        
        Args:
            data: Token data from PumpPortal
        """
        token_mint = data.get("mint")
        name = data.get("name", "")
        symbol = data.get("symbol", "")
        
        # Apply filters if specified
        if self.match_string and self.match_string.lower() not in (name + symbol).lower():
            return
            
        if self.bro_address and data.get("traderPublicKey", "") != self.bro_address:
            return
        
        try:
            # Create mint pubkey
            mint_pubkey = Pubkey.from_string(token_mint)
            
            # IMPORTANT CHANGE: Get bonding curve key directly from message
            bonding_curve_key = data.get("bondingCurveKey")
            if not bonding_curve_key:
                print(f"{Fore.RED}✗ No bondingCurveKey found in message for {symbol}{Style.RESET_ALL}")
                return
                
            bonding_curve = Pubkey.from_string(bonding_curve_key)
            
            # FIXED: Derive the associated token account for the bonding curve
            # instead of setting it to None
            associated_bonding_curve = PumpAddresses.get_associated_bonding_curve_address(
                bonding_curve,
                mint_pubkey
            )
            
            print(f"{Fore.GREEN}✓ Using bonding curve: {bonding_curve_key} for {symbol}{Style.RESET_ALL}")
            print(f"{Fore.GREEN}✓ Derived associated account: {associated_bonding_curve}{Style.RESET_ALL}")
            
            # Default URIs for pump.fun tokens
            uri = data.get("uri", f"https://pump.fun/token/{token_mint}")
            
            # Construct token information with all required fields
            token_info = TokenInfo(
                mint=mint_pubkey,
                name=name,
                symbol=symbol,
                uri=uri,
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                user=None,  # User will be set later when needed
            )
            
            # Subscribe to token trades
            await self._subscribe_to_token_trades(token_mint)
            
            # Initialize volume tracking
            self.token_volumes[token_mint] = 0.0
            
            # Invoke callback
            if self.callback:
                await self.callback(token_info)
                
        except Exception as e:
            print(f"{Fore.RED}✗ Error creating TokenInfo: {e}{Style.RESET_ALL}")
    
    async def _handle_token_trade(self, data: dict) -> None:
        """Handle token trade event.
        
        Args:
            data: Trade data from PumpPortal
        """
        try:
            # Check if this is a trade message (buy or sell)
            tx_type = data.get("txType")
            if tx_type not in ["buy", "sell"]:
                return
                
            token_mint = data.get("mint")
            
            if not token_mint or token_mint not in self.token_volumes:
                return
                
            # Get trade amount in SOL
            sol_amount = float(data.get("solAmount", 0))
            
            # Add to token's volume
            self.token_volumes[token_mint] += sol_amount
            
            # Debug output for trades
            token_mint_short = f"{token_mint[:6]}...{token_mint[-4:]}"
            print(f"{Fore.CYAN}💰 Trade: {token_mint_short} | +{sol_amount:.2f} SOL | Total: {self.token_volumes[token_mint]:.2f} SOL{Style.RESET_ALL}")
            
        except Exception as e:
            print(f"{Fore.RED}✗ Error processing trade data: {e!s}{Style.RESET_ALL}")
    
    def get_token_volume(self, token_mint: str) -> float:
        """Get current trading volume for a token in SOL.
        
        Args:
            token_mint: Token mint address
            
        Returns:
            Volume in SOL
        """
        return self.token_volumes.get(token_mint, 0.0)
    
    def stop(self) -> None:
        """Stop the listener."""
        print(f"{Fore.YELLOW}Stopping PumpPortal listener...{Style.RESET_ALL}")
        self.running = False


class TokenMonitor:
    """Monitors token metrics like volume and price."""
    
    def __init__(self, token_info: TokenInfo, monitor_duration: int = 30):
        self.token_info = token_info
        self.monitor_start_time = monotonic()
        self.monitor_duration = monitor_duration
        self.volume_sol = 0.0
        self.price_sol = 0.0
        self.last_check_time = 0
        self.monitoring_active = True
        self.volume_target_reached = False
        
    @property
    def elapsed_time(self) -> float:
        """Get elapsed monitoring time in seconds."""
        return monotonic() - self.monitor_start_time
        
    @property
    def remaining_time(self) -> float:
        """Get remaining monitoring time in seconds."""
        return max(0, self.monitor_duration - self.elapsed_time)
    
    @property
    def is_expired(self) -> bool:
        """Check if monitoring period has expired."""
        return self.elapsed_time >= self.monitor_duration
    
    def update_metrics(self, volume_sol: float, price_sol: float) -> None:
        """Update token metrics."""
        self.volume_sol = volume_sol
        self.price_sol = price_sol
        self.last_check_time = monotonic()


class PumpTrader:
    """Coordinates trading operations for pump.fun tokens with volume-based strategy."""
    def __init__(
        self,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float,
        sell_slippage: float,
        listener_type: str = "logs",
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,

        extreme_fast_mode: bool = False,
        extreme_fast_token_amount: int = 30,
        
        # Priority fee configuration
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 200_000,
        extra_priority_fee: float = 0.0,
        hard_cap_prior_fee: int = 200_000,
        
        # Retry and timeout settings
        max_retries: int = 3,
        wait_time_after_creation: int = 15, # here and further - seconds
        wait_time_after_buy: int = 15,
        wait_time_before_new_token: int = 15,
        max_token_age: int | float = 60.0,
        token_wait_timeout: int = 30,
        
        # Cleanup settings
        cleanup_mode: str = "disabled",
        cleanup_force_close_with_burn: bool = False,
        cleanup_with_priority_fee: bool = False,
        
        # Trading filters
        match_string: str | None = None,
        bro_address: str | None = None,
        marry_mode: bool = False,
        yolo_mode: bool = False,
        
        # Volume strategy settings
        volume_threshold_sol: float = 20.0,
        monitor_duration: int = 30,
    ):
        """Initialize the pump trader."""
        self.solana_client = SolanaClient(rpc_endpoint)
        self.wallet = Wallet(private_key)
        self.curve_manager = BondingCurveManager(self.solana_client)
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
        )
        self.buyer = TokenBuyer(
            self.solana_client,
            self.wallet,
            self.curve_manager,
            self.priority_fee_manager,
            buy_amount,
            buy_slippage,
            max_retries,
            extreme_fast_token_amount,
            extreme_fast_mode
        )
        self.seller = TokenSeller(
            self.solana_client,
            self.wallet,
            self.curve_manager,
            self.priority_fee_manager,
            sell_slippage,
            max_retries,
        )
        
        # Use our improved PumpPortal listener
        self.token_listener = PumpPortalListener()
        
        # Trading parameters
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount
        
        # Timing parameters
        self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy
        self.wait_time_before_new_token = wait_time_before_new_token
        self.max_token_age = max_token_age
        self.token_wait_timeout = token_wait_timeout
        
        # Cleanup parameters
        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close_with_burn = cleanup_force_close_with_burn
        self.cleanup_with_priority_fee = cleanup_with_priority_fee

        # Trading filters/modes
        self.match_string = match_string
        self.bro_address = bro_address
        self.marry_mode = marry_mode
        self.yolo_mode = yolo_mode
        
        # Volume strategy settings
        self.volume_threshold_sol = volume_threshold_sol
        self.monitor_duration = monitor_duration
        
        # State tracking
        self.traded_mints: Set[Pubkey] = set()
        self.processed_tokens: Set[str] = set()
        self.token_timestamps: Dict[str, float] = {}
        
        # Active monitoring state
        self.active_monitors: Dict[str, TokenMonitor] = {}
        self.monitoring_task = None
        
        # Print welcome message with colored output
        print(f"{Fore.CYAN}{'='*80}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}💰 PUMP.FUN VOLUME SNIPER BOT{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'='*80}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Volume threshold: {self.volume_threshold_sol} SOL{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Monitoring duration: {self.monitor_duration} seconds{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Buy amount: {self.buy_amount} SOL{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'='*80}{Style.RESET_ALL}\n")
        
    async def start(self) -> None:
        """Start the trading bot and listen for new tokens."""
        try:
            health_resp = await self.solana_client.get_health()
            print(f"{Fore.GREEN}✓ RPC connection established{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.RED}✗ RPC connection failed: {e!s}{Style.RESET_ALL}")
            return

        try:
            # Start the monitoring loop as a background task
            self.monitoring_task = asyncio.create_task(self._monitor_tokens_loop())
            
            # Start listening for new tokens
            print(f"{Fore.YELLOW}⚡ Starting token listener...{Style.RESET_ALL}\n")
            await self.token_listener.listen_for_tokens(
                lambda token: self._on_token_detected(token),
                self.match_string,
                self.bro_address,
            )
        
        except Exception as e:
            print(f"{Fore.RED}✗ Error: {e!s}{Style.RESET_ALL}")
        
        finally:
            if self.monitoring_task:
                self.monitoring_task.cancel()
                try:
                    await self.monitoring_task
                except asyncio.CancelledError:
                    pass
            
            await self._cleanup_resources()

    async def _cleanup_resources(self) -> None:
        """Perform cleanup operations before shutting down."""
        if self.traded_mints:
            try:
                print(f"{Fore.YELLOW}🧹 Cleaning up {len(self.traded_mints)} traded token(s)...{Style.RESET_ALL}")
                await handle_cleanup_post_session(
                    self.solana_client, 
                    self.wallet, 
                    list(self.traded_mints), 
                    self.priority_fee_manager,
                    self.cleanup_mode,
                    self.cleanup_with_priority_fee,
                    self.cleanup_force_close_with_burn
                )
            except Exception as e:
                print(f"{Fore.RED}✗ Error during cleanup: {e!s}{Style.RESET_ALL}")
                
        await self.solana_client.close()
        print(f"{Fore.YELLOW}👋 Bot shutdown complete{Style.RESET_ALL}")

    async def _on_token_detected(self, token_info: TokenInfo) -> None:
        """Handle newly detected token."""
        token_key = str(token_info.mint)
        
        if token_key in self.processed_tokens:
            return
            
        self.processed_tokens.add(token_key)
        self.token_timestamps[token_key] = monotonic()
        
        # Show minimal token info
        token_address_short = f"{token_key[:6]}...{token_key[-4:]}"
        print(f"{Fore.YELLOW}🔎 Token detected: {token_info.symbol} ({token_address_short}){Style.RESET_ALL}")
        
        # Create a new token monitor and add to active monitors
        self.active_monitors[token_key] = TokenMonitor(token_info, self.monitor_duration)

    async def _monitor_tokens_loop(self) -> None:
        """Background task that continuously monitors active tokens."""
        status_update_interval = 4  # How often to print status updates (seconds)
        last_status_times = {}  # Track last status update time for each token
        
        while True:
            try:
                # Process all active monitors
                expired_tokens = []
                
                current_time = monotonic()
                
                for token_key, monitor in list(self.active_monitors.items()):
                    if not monitor.monitoring_active:
                        continue
                        
                    token_info = monitor.token_info
                    token_address_short = f"{token_key[:6]}...{token_key[-4:]}"
                    
                    # Check if monitoring period has expired
                    if monitor.is_expired:
                        if not monitor.volume_target_reached:
                            print(f"{Fore.RED}⏱️ Timeout: {token_info.symbol} ({token_address_short}) - Volume: {monitor.volume_sol:.2f} SOL{Style.RESET_ALL}")
                            expired_tokens.append(token_key)
                            # Unsubscribe from token trades when monitoring expires
                            await self.token_listener._unsubscribe_from_token_trades(token_key)
                        continue
                    
                    # Update token metrics - get volume from PumpPortal
                    volume_sol = self.token_listener.get_token_volume(token_key)
                    price_sol = 0  # We don't need price for volume-based strategy
                    monitor.update_metrics(volume_sol, price_sol)
                    
                    # Check if volume threshold reached
                    if monitor.volume_sol >= self.volume_threshold_sol and not monitor.volume_target_reached:
                        monitor.volume_target_reached = True
                        
                        print(f"{Fore.GREEN}🚀 VOLUME TARGET REACHED: {token_info.symbol} ({token_address_short}){Style.RESET_ALL}")
                        print(f"{Fore.GREEN}   Volume: {monitor.volume_sol:.2f} SOL | Time: {monitor.elapsed_time:.1f}s{Style.RESET_ALL}")
                        print(f"{Fore.YELLOW}⚡ Buying token...{Style.RESET_ALL}")
                        
                        # Buy token in a separate task to not block monitoring loop
                        asyncio.create_task(self._buy_token(token_info))
                    
                    # If it's time for a status update
                    last_status_time = last_status_times.get(token_key, 0)
                    if (current_time - last_status_time) >= status_update_interval and not monitor.volume_target_reached:
                        progress = int((monitor.elapsed_time / monitor.monitor_duration) * 20)
                        progress_bar = f"[{'#' * progress}{' ' * (20-progress)}]"
                        
                        print(f"{Fore.CYAN}📊 {token_info.symbol} ({token_address_short}): Vol: {monitor.volume_sol:.2f} SOL | {progress_bar} {int(monitor.remaining_time)}s{Style.RESET_ALL}")
                        
                        # Update last status time
                        last_status_times[token_key] = current_time
                
                # Remove expired monitors
                for token_key in expired_tokens:
                    if token_key in self.active_monitors:
                        del self.active_monitors[token_key]
                        if token_key in last_status_times:
                            del last_status_times[token_key]
                
                # Small delay to avoid excessive CPU usage
                await asyncio.sleep(0.5)
                
            except asyncio.CancelledError:
                # Handle cancellation
                break
            except Exception as e:
                print(f"{Fore.RED}✗ Error in monitoring loop: {e!s}{Style.RESET_ALL}")
                await asyncio.sleep(1)

    async def _buy_token(self, token_info: TokenInfo) -> None:
        """Buy a token that has met the volume threshold."""
        token_key = str(token_info.mint)
        token_address_short = f"{token_key[:6]}...{token_key[-4:]}"
        
        try:
            # Update the user field before buying
            token_info.user = self.wallet.pubkey
            
            # Ensure bonding curve is initialized before buying
            for attempt in range(8):  # Increased attempts
                bonding_curve_account = await self.solana_client.get_account_info(token_info.bonding_curve)
                if bonding_curve_account is not None and bonding_curve_account.owner == PumpAddresses.PROGRAM:
                    print(f"{Fore.GREEN}✓ Bonding curve verified for {token_info.symbol}{Style.RESET_ALL}")
                    break
                
                wait_time = 2 + attempt  # Increasing delays
                print(f"{Fore.YELLOW}⏳ Waiting for bonding curve initialization... (attempt {attempt+1}/8) {wait_time}s{Style.RESET_ALL}")
                await asyncio.sleep(wait_time)
                
                # If last attempt, log details
                if attempt == 7:
                    print(f"{Fore.RED}🔍 Bonding curve debug: {token_info.bonding_curve}{Style.RESET_ALL}")
            
            # Execute buy operation
            buy_result: TradeResult = await self.buyer.execute(token_info)

            if buy_result.success:
                print(f"{Fore.GREEN}✅ BOUGHT {token_info.symbol} ({token_address_short}){Style.RESET_ALL}")
                print(f"{Fore.GREEN}   Amount: {buy_result.amount:.6f} tokens | Price: {buy_result.price:.8f} SOL{Style.RESET_ALL}")
                print(f"{Fore.GREEN}   TX: {buy_result.tx_signature}{Style.RESET_ALL}")
                await asyncio.sleep(self.wait_time_after_buy + 3)
                
                self.traded_mints.add(token_info.mint)
                self._log_trade("buy", token_info, buy_result.price, buy_result.amount, buy_result.tx_signature)
                
                # Sell if not in marry mode
                if not self.marry_mode:
                    print(f"{Fore.YELLOW}⏳ Waiting {self.wait_time_after_buy}s before selling...{Style.RESET_ALL}")
                    await asyncio.sleep(self.wait_time_after_buy)
                    await asyncio.sleep(1.5)
                    print(f"{Fore.YELLOW}💰 Selling {token_info.symbol}...{Style.RESET_ALL}")
                    
                    sell_result: TradeResult = await self.seller.execute(token_info)
                    
                    if sell_result.success:
                        print(f"{Fore.GREEN}✅ SOLD {token_info.symbol} ({token_address_short}){Style.RESET_ALL}")
                        print(f"{Fore.GREEN}   Amount: {sell_result.amount:.6f} tokens | Price: {sell_result.price:.8f} SOL{Style.RESET_ALL}")
                        print(f"{Fore.GREEN}   TX: {sell_result.tx_signature}{Style.RESET_ALL}")
                        
                        self._log_trade("sell", token_info, sell_result.price, sell_result.amount, sell_result.tx_signature)
                        
                        # Cleanup
                        await handle_cleanup_after_sell(
                            self.solana_client, 
                            self.wallet, 
                            token_info.mint, 
                            self.priority_fee_manager,
                            self.cleanup_mode,
                            self.cleanup_with_priority_fee,
                            self.cleanup_force_close_with_burn
                        )
                    else:
                        print(f"{Fore.RED}✗ Failed to sell {token_info.symbol}: {sell_result.error_message}{Style.RESET_ALL}")
            else:
                print(f"{Fore.RED}✗ Failed to buy {token_info.symbol}: {buy_result.error_message}{Style.RESET_ALL}")
                
        except Exception as e:
            print(f"{Fore.RED}✗ Error buying {token_info.symbol}: {e!s}{Style.RESET_ALL}")
        
        finally:
            # Remove from active monitors
            if token_key in self.active_monitors:
                self.active_monitors[token_key].monitoring_active = False

    def _log_trade(
        self,
        action: str,
        token_info: TokenInfo,
        price: float,
        amount: float,
        tx_hash: str | None,
    ) -> None:
        """Log trade information."""
        try:
            os.makedirs("trades", exist_ok=True)

            log_entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "action": action,
                "token_address": str(token_info.mint),
                "symbol": token_info.symbol,
                "price": price,
                "amount": amount,
                "tx_hash": str(tx_hash) if tx_hash else None,
            }

            with open("trades/trades.log", "a") as log_file:
                log_file.write(json.dumps(log_entry) + "\n")
        except Exception as e:
            print(f"{Fore.RED}✗ Failed to log trade information: {e!s}{Style.RESET_ALL}")
