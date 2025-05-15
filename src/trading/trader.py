"""
Main trading coordinator for pump.fun tokens.
Volume-based trading strategy with clean terminal output.
"""

import asyncio
import json
import os
import logging
import time
from datetime import datetime
from time import monotonic
from typing import Dict, Set, Callable, List, Optional, Tuple
from core.pubkeys import PumpAddresses
from client.moralis_client import MoralisClient
from trading.token_quality_analyzer import TokenQualityAnalyzer
from core.curve import BondingCurveState





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
from core.wallet import Wallet
from monitoring.block_listener import BlockListener
from monitoring.geyser_listener import GeyserListener
from monitoring.logs_listener import LogsListener
from monitoring.helius_websocket import HeliusWebSocketClient  # New import
from trading.base import TokenInfo, TradeResult
from trading.buyer import TokenBuyer
from trading.seller import TokenSeller
from trading.position_manager import PositionManager, Position
from analytics.analytics import TradingAnalytics
from utils.logger import get_logger
from trading.helius_token_monitor import WalletBalanceMonitor
from client.token_account_manager import TokenAccountManager
from logging_config import setup_logging

logger = setup_logging(console_level=logging.INFO)

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

# Silence the specific curve state errors to reduce spam
curve_logger = logging.getLogger("core.curve")
curve_logger.addFilter(lambda record: "Failed to get curve state:" not in record.getMessage())

# Constants for position management
TAKE_PROFIT_PCT = 0.20  # 20% take profit
STOP_LOSS_PCT = 0.10  # 10% stop loss
TRAILING_STOP_PCT = 0.10  # 10% trailing stop
MAX_OPEN_POSITIONS = 3  # Maximum concurrent positions


# Improved PumpPortal listener that subscribes to token trade data
from utils.robust_websocket import RobustWebSocket  # Import the new class


class PumpPortalListener:
    """Listens to PumpPortal WebSocket API for token creation and trade events."""

    def __init__(self, endpoint="wss://pumpportal.fun/api/data"):
        """Initialize PumpPortal listener."""
        self.endpoint = endpoint
        self.websocket = None  # This will be a RobustWebSocket instance
        self.callback = None
        self.match_string = None
        self.bro_address = None
        self.subscribed_tokens = set()
        self.token_volumes = {}
        self.running = False

        # Diagnostic state
        self.messages_received = 0
        self.token_creation_events = 0
        self.trade_events = 0

        self.logger = get_logger(__name__)

    async def listen_for_tokens(
            self,
            callback: Callable[[TokenInfo], None],
            match_string: str = None,
            bro_address: str = None,
    ) -> None:
        """Listen for new token events using robust WebSocket client."""
        print(f"{Fore.YELLOW}🔌 Starting PumpPortal listener...{Style.RESET_ALL}")
        self.callback = callback
        self.match_string = match_string
        self.bro_address = bro_address
        self.running = True

        # Create and start the robust WebSocket client
        self.websocket = RobustWebSocket(
            url=self.endpoint,
            on_message=self._process_message,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            ping_interval=30,
            reconnect_interval_min=1,
            reconnect_interval_max=30,
            max_reconnect_attempts=0,  # Unlimited reconnection attempts
            message_timeout=60,
        )

        await self.websocket.start()

        # Keep the listener running until stopped
        while self.running:
            await asyncio.sleep(1)

        # Cleanup when stopped
        await self.stop()

    async def stop(self) -> None:
        """Stop the listener."""
        self.running = False
        if self.websocket:
            await self.websocket.stop()
            self.websocket = None

    async def _on_connect(self) -> None:
        """Called when the WebSocket connects."""
        # Subscribe to token creation events
        await self._subscribe_to_new_tokens()
        print(f"{Fore.YELLOW}Listening for token events...{Style.RESET_ALL}")

    async def _on_disconnect(self) -> None:
        """Called when the WebSocket disconnects."""
        print(f"{Fore.YELLOW}Disconnected from PumpPortal WebSocket{Style.RESET_ALL}")

    async def _process_message(self, data: dict) -> None:
        """Process incoming WebSocket messages."""
        try:
            self.messages_received += 1

            # Print diagnostic stats less frequently to reduce spam
            if self.messages_received % 100 == 0:
                print(
                    f"{Fore.CYAN}📊 Stats: {self.messages_received} messages | {self.token_creation_events} tokens | {self.trade_events} trades{Style.RESET_ALL}")

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
                # Handle trade message silently - only update counters to reduce spam
                self.trade_events += 1
                await self._handle_token_trade(data)

            elif "message" in data:
                # Service message
                self.logger.debug(f"Service message: {data.get('message', 'Unknown')}")

        except Exception as e:
            print(f"{Fore.RED}✗ Error processing message: {str(e)}{Style.RESET_ALL}")

    async def _subscribe_to_new_tokens(self) -> None:
        """Subscribe to new token creation events."""
        if not self.websocket:
            return

        payload = {"method": "subscribeNewToken"}
        await self.websocket.send(payload)
        self.logger.debug(f"✓ Subscribed to new token events")

    async def _subscribe_to_token_trades(self, token_mint: str) -> None:
        """Subscribe to trade events for a specific token."""
        if not self.websocket or token_mint in self.subscribed_tokens:
            return

        payload = {
            "method": "subscribeTokenTrade",
            "keys": [token_mint]
        }
        await self.websocket.send(payload)
        self.subscribed_tokens.add(token_mint)
        self.logger.debug(f"📊 Subscribed to trades for: {token_mint[:8]}...")

    async def _unsubscribe_from_token_trades(self, token_mint: str) -> None:
        """Unsubscribe from trade events for a specific token."""
        if not self.websocket or token_mint not in self.subscribed_tokens:
            return

        payload = {
            "method": "unsubscribeTokenTrade",
            "keys": [token_mint]
        }
        await self.websocket.send(json.dumps(payload))
        self.subscribed_tokens.discard(token_mint)
        self.logger.debug(f"🛑 Unsubscribed from trades for: {token_mint[:8]}...")

    async def _handle_new_token(self, data: dict) -> None:
        """Handle new token detection event."""
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

            # Get bonding curve key directly from message
            bonding_curve_key = data.get("bondingCurveKey")
            if not bonding_curve_key:
                print(f"{Fore.RED}✗ No bondingCurveKey found in message for {symbol}{Style.RESET_ALL}")
                return

            bonding_curve = Pubkey.from_string(bonding_curve_key)

            # Derive the associated token account for the bonding curve
            associated_bonding_curve = PumpAddresses.get_associated_bonding_curve_address(
                bonding_curve,
                mint_pubkey
            )

            self.logger.debug(f"✓ Using bonding curve: {bonding_curve_key} for {symbol}")
            self.logger.debug(f"✓ Derived associated account: {associated_bonding_curve}")

            # Default URIs for pump.fun tokens
            uri = data.get("uri", f"https://pump.fun/token/{token_mint}")

            # Get creator from message data
            creator_address = data.get("creator") or data.get("traderPublicKey")
            if not creator_address:
                print(f"{Fore.RED}✗ No creator address found for {symbol}{Style.RESET_ALL}")
                return

            # Convert creator to Pubkey
            creator_pubkey = Pubkey.from_string(creator_address)

            # Derive creator vault using the proper seed format
            creator_vault, _ = Pubkey.find_program_address(
                [b"creator-vault", bytes(creator_pubkey)],  # Note: use hyphen, not underscore
                PumpAddresses.PROGRAM  # Assuming this constant is available
            )

            print(f"{Fore.CYAN}✓ Derived creator vault: {str(creator_vault)[:8]}...{Style.RESET_ALL}")

            # Construct token information with all required fields
            token_info = TokenInfo(
                mint=mint_pubkey,
                name=name,
                symbol=symbol,
                uri=uri,
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                user=creator_pubkey,  # Set user to creator
                creator=creator_pubkey,  # Add creator
                creator_vault=creator_vault  # Add creator_vault
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
        """Handle token trade event."""
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

            # NOTE: We're no longer printing each trade to reduce terminal spam
            # Only significant volume changes will be shown in the monitoring loop

        except Exception as e:
            print(f"{Fore.RED}✗ Error processing trade data: {e!s}{Style.RESET_ALL}")

    def get_token_volume(self, token_mint: str) -> float:
        """Get current trading volume for a token in SOL."""
        return self.token_volumes.get(token_mint, 0.0)

    def get_wallet_sol_balance(self):
        """Get the current SOL balance directly from the balance monitor"""
        if hasattr(self, 'balance_monitor') and hasattr(self.balance_monitor, 'helius_monitor'):
            return self.balance_monitor.helius_monitor.get_sol_balance()
        return 0.0

    def stop(self) -> None:
        """Stop the listener."""
        print(f"{Fore.YELLOW}Stopping PumpPortal listener...{Style.RESET_ALL}")
        self.running = False


class TokenMonitor:
    """Monitors token metrics like volume, price, and market conditions."""

    def __init__(self, token_info: TokenInfo, monitor_duration: int = 60):
        self.token_info = token_info
        self.monitor_start_time = monotonic()
        self.monitor_duration = monitor_duration
        self.volume_sol = 0.0
        self.price_sol = 0.0
        self.last_check_time = 0
        self.monitoring_active = True
        self.volume_target_reached = False

        # Enhanced tracking for analysis
        self.price_history = []  # List of (timestamp, price) tuples
        self.volume_history = []  # List of (timestamp, volume_delta) tuples

        # Track trading information - NEW
        self.trade_count = 0
        self.trades = []  # List to store individual trades
        self.last_volume_update = monotonic()  # Track last volume update time
        self.duration_seconds = 0  # Track duration of trading activity

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
        current_time = monotonic()

        # Track volume changes
        volume_delta = volume_sol - self.volume_sol
        if volume_delta > 0:
            self.volume_history.append((self.elapsed_time, volume_delta))

            # NEW: Track individual trades for better analysis
            self.trades.append({
                "time": self.elapsed_time,
                "volume": volume_delta,
                "price": price_sol if price_sol > 0 else None
            })
            self.trade_count += 1
            self.last_volume_update = current_time

            # Update duration of trading activity
            self.duration_seconds = self.elapsed_time

        # Track price history if price is available
        if price_sol > 0:
            self.price_history.append((self.elapsed_time, price_sol))

        self.volume_sol = volume_sol
        self.price_sol = price_sol
        self.last_check_time = current_time

    def recent_volume_acceleration(self, window_seconds: float = 5.0) -> float:
        """Calculate volume acceleration (change in volume rate)."""
        if len(self.volume_history) < 3:
            return 0.0

        current_time = self.elapsed_time
        recent_window = [(t, v) for t, v in self.volume_history
                         if (current_time - t) <= window_seconds]
        older_window = [(t, v) for t, v in self.volume_history
                        if window_seconds < (current_time - t) <= window_seconds * 2]

        if not recent_window or not older_window:
            return 0.0

        recent_vol = sum(v for _, v in recent_window)
        older_vol = sum(v for _, v in older_window)

        # Simple acceleration metric
        acceleration = recent_vol - older_vol
        return acceleration


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
            geyser_endpoint: Optional[str] = None,
            geyser_api_token: Optional[str] = None,
            helius_api_key: Optional[str] = None,  # New parameter for Helius API key
            moralis_api_key: Optional[str] = None,  # New parameter for Moralis API

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
            wait_time_after_creation: int = 15,  # seconds
            wait_time_after_buy: int = 15,
            wait_time_before_new_token: int = 15,
            max_token_age: float = 60.0,
            token_wait_timeout: int = 60,

            # Cleanup settings
            cleanup_mode: str = "disabled",
            cleanup_force_close_with_burn: bool = False,
            cleanup_with_priority_fee: bool = False,

            # Trading filters
            match_string: Optional[str] = None,
            bro_address: Optional[str] = None,
            marry_mode: bool = False,
            yolo_mode: bool = False,

            # Volume strategy settings
            volume_threshold_sol: float = 20.0,
            monitor_duration: int = 60,

            # Position management settings
            take_profit_tiers: Optional[List[float]] = None,
            sell_portions: Optional[List[float]] = None,
            stop_loss_pct: float = STOP_LOSS_PCT,
            trailing_stop_pct: float = TRAILING_STOP_PCT,
            max_positions: int = MAX_OPEN_POSITIONS,

            # Auto-sell inactive positions
            inactive_position_timeout: int = 30,

            # ATA cleanup interval
            ata_cleanup_interval: int = 300,
    ):
        """Initialize the pump trader."""
        self.solana_client = SolanaClient(rpc_endpoint)
        self.wallet = Wallet(private_key)

        # Initialize balance monitor with Helius API key
        self.balance_monitor = WalletBalanceMonitor(
            self.solana_client,
            self.wallet.pubkey,
            helius_api_key=helius_api_key
        )

        # NEW: Initialize Helius WebSocket client
        helius_ws_api_key = "f1dfa08b-c8bf-4baf-9e37-52271bc4c969"  # Your new dedicated key
        helius_ws_url = f"wss://mainnet.helius-rpc.com/?api-key={helius_ws_api_key}"
        self.helius_ws = HeliusWebSocketClient(helius_ws_url)

        self.curve_manager = BondingCurveManager(self.solana_client)
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
        )

        # Initialize token account manager with reference to the Helius monitor
        self.token_account_manager = TokenAccountManager(
            self.solana_client,
            helius_monitor=self.balance_monitor.helius_monitor
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
            extreme_fast_mode,
            balance_monitor=self.balance_monitor
        )
        self.seller = TokenSeller(
            self.solana_client,
            self.wallet,
            self.curve_manager,
            self.priority_fee_manager,
            sell_slippage,
            max_retries,
            balance_monitor=self.balance_monitor
        )

        # Use our improved PumpPortal listener
        self.token_listener = PumpPortalListener()

        # Setup position management (new)
        if take_profit_tiers is None:
            # GREEDY MODE: More aggressive take profit tiers with earlier partial exit
            take_profit_tiers = [1.3, 1.6, 2.0]  # 30%, 60%, 100%

        if sell_portions is None:
            # GREEDY MODE: Larger first portion to secure initial investment faster
            sell_portions = [0.5, 0.5, 1.0]  # Sell 50% at first tier

        self.position_manager = PositionManager(
            take_profit_tiers=take_profit_tiers,
            sell_portions=sell_portions,
            stop_loss_pct=stop_loss_pct,
            trailing_stop_pct=trailing_stop_pct,
            max_positions=max_positions
        )

        # Setup analytics (new)
        self.analytics = TradingAnalytics()

        # ====== NEW: Add Moralis and token quality analyzer ======
        self.moralis = None
        self.token_quality_analyzer = None

        # Only set up if API key is provided
        if moralis_api_key:
            try:
                # Import here to avoid issues if the files don't exist yet
                from client.moralis_client import MoralisClient
                from trading.token_quality_analyzer import TokenQualityAnalyzer

                self.moralis = MoralisClient(moralis_api_key)
                self.token_quality_analyzer = TokenQualityAnalyzer(self.moralis)
                print(
                    f"{Fore.GREEN}✅ Moralis API integration active - Enhanced token analysis enabled{Style.RESET_ALL}")
            except Exception as e:
                print(
                    f"{Fore.YELLOW}⚠️ Moralis API integration failed: {e} - Using basic analysis only{Style.RESET_ALL}")
                self.moralis = None

        # Always create the analyzer - it works without Moralis too
        if not self.token_quality_analyzer:
            try:
                from trading.token_quality_analyzer import TokenQualityAnalyzer
                self.token_quality_analyzer = TokenQualityAnalyzer()
                print(
                    f"{Fore.YELLOW}ℹ️ Using basic token analysis (set MORALIS_API_KEY for enhanced analysis){Style.RESET_ALL}")
            except Exception as e:
                print(f"{Fore.RED}⚠️ Token quality analyzer initialization failed: {e}{Style.RESET_ALL}")
                self.token_quality_analyzer = None
        # =======================================================

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

        # NEW: Auto-sell inactive positions settings
        self.inactive_position_timeout = inactive_position_timeout

        # NEW: ATA cleanup settings
        self.ata_cleanup_interval = ata_cleanup_interval
        self.last_ata_cleanup_time = 0

        # State tracking
        self.traded_mints: Set[Pubkey] = set()
        self.processed_tokens: Set[str] = set()
        self.token_timestamps: Dict[str, float] = {}

        # Active monitoring state
        self.active_monitors: Dict[str, TokenMonitor] = {}
        self.monitoring_task = None
        self.position_monitor_task = None
        self.ata_cleanup_task = None  # NEW: Task for ATA cleanup

        # NEW: WebSocket monitoring state
        self.active_monitoring = {}  # Track active WebSocket subscriptions by token mint

        # Cache for account info
        self._account_cache = {}
        self._account_cache_times = {}
        self._cache_ttl = 3.0  # Cache TTL in seconds

        # Position tracking with locking
        self.buying_lock = asyncio.Lock()  # Add a lock to prevent concurrent buys
        self.pending_buys_count = 0  # Track buys in progress
        # NEW: Add pump.fun specific constants
        self.PUMP_FUN_FLOOR_PRICE_SOL = 0.000000075  # Minimum price for abandoned token
        self.PUMP_FUN_SUPPLY = 1000000000  # 1 billion fixed supply

        # Print welcome message with colored output
        print(f"{Fore.CYAN}{'=' * 80}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}💰 PUMP.FUN VOLUME SNIPER BOT{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'=' * 80}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Volume threshold: {self.volume_threshold_sol} SOL{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Monitoring duration: {self.monitor_duration} seconds{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Buy amount: {self.buy_amount} SOL{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Take profit tiers: {take_profit_tiers}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Trailing stop: {trailing_stop_pct * 100}%{Style.RESET_ALL}")
        print(f"{Fore.GREEN}WebSocket balance monitoring: Enabled{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Auto-sell inactive positions: {inactive_position_timeout}s{Style.RESET_ALL}")
        print(f"{Fore.GREEN}ATA cleanup interval: {ata_cleanup_interval}s{Style.RESET_ALL}")

        # NEW: Add token quality analysis info
        if self.token_quality_analyzer:
            if self.moralis:
                print(f"{Fore.GREEN}Token quality analysis: Enhanced (Moralis integration){Style.RESET_ALL}")
            else:
                print(f"{Fore.YELLOW}Token quality analysis: Basic (on-chain data only){Style.RESET_ALL}")

        print(f"{Fore.CYAN}{'=' * 80}{Style.RESET_ALL}\n")

    async def start(self) -> None:
        """Start the trading bot and listen for new tokens."""
        try:
            health_resp = await self.solana_client.get_health()
            print(f"{Fore.GREEN}✓ RPC connection established{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.RED}✗ RPC connection failed: {e!s}{Style.RESET_ALL}")
            return

        # Start the balance monitor
        await self.balance_monitor.start()

        # NEW: Start Helius WebSocket connection
        await self.helius_ws.connect()
        print(f"{Fore.GREEN}✓ Helius WebSocket connected{Style.RESET_ALL}")

        try:
            # Start the monitoring loops as background tasks
            self.monitoring_task = asyncio.create_task(self._monitor_tokens_loop())
            self.position_monitor_task = asyncio.create_task(self._monitor_positions_loop())

            # NEW: Start the ATA cleanup task
            self.ata_cleanup_task = asyncio.create_task(self._ata_cleanup_loop())
            self.position_display_task = asyncio.create_task(self._display_positions_loop())

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

            if self.position_monitor_task:
                self.position_monitor_task.cancel()
                try:
                    await self.position_monitor_task
                except asyncio.CancelledError:
                    pass

            # NEW: Cancel ATA cleanup task
            if self.ata_cleanup_task:
                self.ata_cleanup_task.cancel()
                try:
                    await self.ata_cleanup_task
                except asyncio.CancelledError:
                    pass

            # NEW: Cancel position display task
            if hasattr(self, 'position_display_task') and self.position_display_task:
                self.position_display_task.cancel()
                try:
                    await self.position_display_task
                except asyncio.CancelledError:
                    pass

            await self.balance_monitor.stop()

            # NEW: Stop WebSocket connection
            self.helius_ws.stop()

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

    async def should_buy_token(self, token_info: TokenInfo, monitor: TokenMonitor) -> bool:
        """Determine if a token meets all buying criteria using verified volume data."""

        # Skip if blacklisted
        if hasattr(self, 'blacklisted_tokens') and self.is_blacklisted(str(token_info.mint)):
            print(f"{Fore.YELLOW}⚠️ FILTER: Skipping {token_info.symbol}: blacklisted token{Style.RESET_ALL}")
            return False

        # Prepare data for quality check - use Moralis for verification
        if hasattr(self, 'token_quality_analyzer') and self.token_quality_analyzer:
            try:
                # Prepare pump data for quality check
                pump_data = {
                    "volume": monitor.volume_sol,
                    "trades": len(monitor.volume_history),
                    "age_seconds": monitor.elapsed_time
                }

                # Run quality analysis which will verify the volume with Moralis
                token_mint = str(token_info.mint)
                quality_metrics = await self.token_quality_analyzer.analyze_token(token_mint, pump_data)

                # Log the decision explanation
                explanation = self.token_quality_analyzer.get_decision_explanation(quality_metrics)
                print(explanation)

                # Use verified volume to make the decision
                if quality_metrics.get("verified", False):
                    # If the volume was verified with Moralis but is too low
                    if quality_metrics.get("volume_sol", 0) < self.volume_threshold_sol:
                        print(f"{Fore.YELLOW}⚠️ VERIFIED VOLUME FILTER: Skipping {token_info.symbol}: "
                              f"{quality_metrics.get('volume_sol', 0):.2f} SOL (need {self.volume_threshold_sol} SOL){Style.RESET_ALL}")
                        return False

                    # If the trade count was verified with Moralis but is too low
                    if quality_metrics.get("trades", 0) < 3:
                        print(f"{Fore.YELLOW}⚠️ VERIFIED TRADE COUNT FILTER: Skipping {token_info.symbol}: "
                              f"low trade count ({quality_metrics.get('trades', 0)}){Style.RESET_ALL}")
                        return False

                # If quality check fails, skip this token
                if not quality_metrics.get("buy_recommended", True):
                    print(
                        f"{Fore.YELLOW}⚠️ QUALITY FILTER: Skipping {token_info.symbol} due to low quality score{Style.RESET_ALL}")
                    return False

            except Exception as e:
                print(
                    f"{Fore.RED}✗ Error during quality analysis: {e!s}. Continuing with standard checks.{Style.RESET_ALL}")
                # Fall back to pump portal checks if Moralis fails

                # Check volume threshold
                if monitor.volume_sol < self.volume_threshold_sol:
                    if monitor.volume_sol >= (self.volume_threshold_sol * 0.5):
                        print(f"{Fore.YELLOW}⚠️ VOLUME FILTER: Skipping {token_info.symbol}: "
                              f"{monitor.volume_sol:.2f} SOL (need {self.volume_threshold_sol} SOL){Style.RESET_ALL}")
                    return False

                # Check trade count
                trade_count = len(monitor.volume_history)
                if trade_count < 3:
                    print(f"{Fore.YELLOW}⚠️ TRADE COUNT FILTER: Skipping {token_info.symbol}: "
                          f"low trade count ({trade_count}){Style.RESET_ALL}")
                    return False

        # PRICE FILTER with one‐time on‐chain fallback
        current_price = monitor.price_sol
        if current_price <= 0:
            try:
                # force a fresh RPC fetch
                curve_state = await self.curve_manager.get_curve_state(
                    token_info.bonding_curve,
                    force_refresh=True
                )
                current_price = curve_state.calculate_price()
            except Exception:
                current_price = 0

        if current_price <= 0:
            # warn but do not skip
            print(f"{Fore.YELLOW}⚠️ PRICE WARNING: Couldn't determine price for "
                  f"{token_info.symbol}, proceeding on volume only{Style.RESET_ALL}")
        else:
            # risk‐reward: skip tokens that are already way above the floor
            if current_price > self.PUMP_FUN_FLOOR_PRICE_SOL * 20:
                multiplier = current_price / self.PUMP_FUN_FLOOR_PRICE_SOL
                print(f"{Fore.YELLOW}⚠️ PRICE FILTER: Skipping {token_info.symbol}: "
                      f"price too high ({multiplier:.1f}× floor){Style.RESET_ALL}")
                return False

        # Market cooling?
        if hasattr(self, 'is_market_cooling') and self.is_market_cooling():
            print(f"{Fore.YELLOW}⚠️ MARKET FILTER: Skipping {token_info.symbol}: "
                  f"market in cooling period{Style.RESET_ALL}")
            return False

        return True

    def _prune_account_cache(self, max_age: float = 10.0) -> None:
        """Remove old entries from account cache to avoid memory bloat."""
        current_time = monotonic()

        # Prune account cache
        for key in list(self._account_cache_times.keys()):
            if current_time - self._account_cache_times[key] > max_age:
                if key in self._account_cache:
                    del self._account_cache[key]
                del self._account_cache_times[key]

    # Call this method at the end of each monitoring loop iteration
    # Add this line at the bottom of _monitor_tokens_loop and _monitor_positions_loop:
    # self._prune_tracking_dictionaries()

    def get_dynamic_exit_parameters(self, token_info: TokenInfo, monitor: TokenMonitor) -> dict:
        """Calculate dynamic exit parameters based on token metrics and floor price."""
        # GREEDY MODE: Default parameters optimized for securing initial investment
        default_params = {
            "take_profit_tiers": [1.3, 1.6, 2.0],  # 30%, 60%, 100%
            "sell_portions": [0.5, 0.5, 1.0],  # Sell 50% at first tier
            "stop_loss_pct": 0.10,  # 10%
            "trailing_stop_pct": 0.08  # 8%
        }

        # Get current price and calculate risk profile
        current_price = monitor.price_sol
        if current_price <= 0:
            return default_params  # Can't analyze without price

        # Calculate how far above floor the entry price is
        floor_ratio = current_price / self.PUMP_FUN_FLOOR_PRICE_SOL

        # For tokens already high above floor (pumped significantly)
        if floor_ratio > 15:  # More than 15x above floor
            # GREEDY MODE: Very aggressive early exit for high-risk tokens
            return {
                "take_profit_tiers": [1.15, 1.4, 2.0],  # Take profits sooner
                "sell_portions": [0.6, 0.4, 1.0],  # Secure 60% of initial investment early
                "stop_loss_pct": 0.07,  # Tighter stop loss
                "trailing_stop_pct": 0.05  # Tighter trailing stop
            }

        # For tokens just starting to rise from floor (lots of potential)
        elif floor_ratio < 3:  # Less than 3x above floor
            # GREEDY MODE: Lower first target but still prioritize securing initial
            return {
                "take_profit_tiers": [1.25, 1.6, 2.0],  # First target at 25%
                "sell_portions": [0.4, 0.6, 1.0],  # Slightly smaller first portion
                "stop_loss_pct": 0.12,  # Allow more room
                "trailing_stop_pct": 0.10  # Allow more room for volatility
            }

        # For very fast tokens (high velocity), use more aggressive take profits
        elif monitor.elapsed_time < 10 and monitor.volume_sol > self.volume_threshold_sol:
            # GREEDY MODE: Faster exit for fast-moving tokens
            return {
                "take_profit_tiers": [1.2, 1.5, 2.0],  # 20%, 50%, 100%
                "sell_portions": [0.5, 0.5, 1.0],  # Standard portion split
                "stop_loss_pct": 0.09,  # 9%
                "trailing_stop_pct": 0.07  # 7%
            }

        # Default parameters for normal cases
        return default_params

    def calculate_optimal_stop_loss(self, entry_price: float, floor_ratio: float) -> float:
        """Calculate optimal stop-loss based on floor price ratio."""

        # GREEDY MODE: Stop loss never below floor price
        floor_price = self.PUMP_FUN_FLOOR_PRICE_SOL

        # If entry price is already very close to floor
        if entry_price < floor_price * 1.3:  # Within 30% of floor
            return max(entry_price * 0.93, floor_price)  # 7% below entry or floor

        # For tokens with different floor ratios
        if floor_ratio > 10:  # Very high above floor
            # More aggressive stop loss (15% below entry but not below 150% of floor)
            return max(entry_price * 0.85, floor_price * 1.5)
        elif floor_ratio > 5:  # Moderately above floor
            # Medium risk (12% below entry but not below 130% of floor)
            return max(entry_price * 0.88, floor_price * 1.3)
        else:  # Close to floor
            # Lower risk (10% below entry but not below 110% of floor)
            return max(entry_price * 0.90, floor_price * 1.1)

    async def _display_positions_loop(self):
        """Periodically display positions in a clear, formatted way"""
        while True:
            try:
                # Get SOL balance DIRECTLY from helius_monitor!
                sol_balance = 0.0
                if hasattr(self, 'balance_monitor') and hasattr(self.balance_monitor, 'helius_monitor'):
                    sol_balance = self.balance_monitor.helius_monitor.get_sol_balance()

                # Format positions using position manager's active positions
                active_positions = []
                active_count = 0
                max_positions = 3  # Default max positions

                if hasattr(self, 'position_manager'):
                    if hasattr(self.position_manager, 'positions'):
                        active_positions = list(self.position_manager.positions.values())
                        active_count = len(active_positions)
                    if hasattr(self.position_manager, 'max_positions'):
                        max_positions = self.position_manager.max_positions

                # Print header
                print("\n" + "=" * 60)
                print(f"{Fore.CYAN}💰 OPEN POSITIONS{Style.RESET_ALL}")
                print("=" * 60)

                # Print each position with details
                if active_positions:
                    print(f"{Fore.YELLOW}💼 ACTIVE POSITIONS: {active_count}/{max_positions}{Style.RESET_ALL}")

                    for pos in active_positions:
                        # Get token name and price info
                        token_name = pos.symbol if hasattr(pos, 'symbol') else str(pos.token_key)[:6]

                        # Get entry price
                        entry_price = pos.entry_price if hasattr(pos, 'entry_price') else 0

                        # CRITICAL FIX: Use position's roi_pct property instead of calculating directly
                        roi_pct = pos.websocket_roi if pos.is_websocket_monitored() else pos.roi_pct

                        # Determine ROI color
                        if roi_pct > 0:
                            roi_color = Fore.GREEN
                            roi_symbol = "🟢"
                        elif roi_pct < 0:
                            roi_color = Fore.RED
                            roi_symbol = "🔴"
                        else:
                            roi_color = Fore.WHITE
                            roi_symbol = "⚪"

                        # Check if WebSocket monitoring is active - FIXED: Call as method
                        ws_status = "✅" if hasattr(pos,
                                                   'is_websocket_monitored') and pos.is_websocket_monitored() else "❌"

                        # Print position details
                        print(
                            f"  {token_name}: Entry {entry_price:.8f} | {roi_color}ROI: {roi_symbol} {roi_pct:.2f}%{Style.RESET_ALL} | WS: {ws_status}")

                        # ENHANCEMENT: Show initial investment secured status if applicable
                        if hasattr(pos, 'initial_recovered') and pos.initial_recovered:
                            print(f"    {Fore.GREEN}💰 Initial investment secured!{Style.RESET_ALL}")
                else:
                    print(f"{Fore.YELLOW}💼 NO ACTIVE POSITIONS{Style.RESET_ALL}")

                # Print summary and balance
                print(
                    f"{Fore.CYAN}Volume threshold: {self.volume_threshold_sol:.2f} SOL | Bot balance: {sol_balance:.4f} SOL{Style.RESET_ALL}")
                print("=" * 60)

                # Update every 15 seconds
                await asyncio.sleep(15)
            except Exception as e:
                print(f"{Fore.RED}Error displaying position status: {str(e)}{Style.RESET_ALL}")
                await asyncio.sleep(15)



    # Call this method every 30 seconds
    async def position_summary_loop(self):
        """Display position summary every 30 seconds"""
        while True:
            try:
                await self.display_position_summary()  # This calls the method below
                await asyncio.sleep(30)
            except Exception as e:
                print(f"✗ Error in position monitoring loop: {e}")
                await asyncio.sleep(30)

    def _display_position_summary(self):
        """
        Display a summary of all current positions.
        Now uses correct ROI calculation.
        """
        positions = self.position_manager.get_all_positions()
        if not positions:
            print(f"{Fore.YELLOW}No active positions.{Style.RESET_ALL}")
            return

        print(f"\n{Fore.CYAN}===== POSITION SUMMARY ====={Style.RESET_ALL}")
        print(f"{'Symbol':<15} {'Entry':<12} {'Current':<12} {'ROI%':<10} {'Stop':<12} {'Trail':<12}")
        print("-" * 70)

        total_value = 0
        total_cost = 0

        for position in positions:
            # Use the corrected ROI calculation
            roi = position.roi_pct
            roi_color = Fore.GREEN if roi >= 0 else Fore.RED

            # Get other values with safety checks
            entry = getattr(position, 'entry_price', 0)
            current = getattr(position, 'current_price', 0)
            stop = getattr(position, 'stop_loss', 0)
            trail = getattr(position, 'trailing_stop_price', 0)

            # Calculate position values for portfolio total
            current_value = position.amount_remaining * current
            original_cost = position.original_amount * entry
            total_value += current_value
            total_cost += original_cost

            print(f"{position.symbol:<15} {entry:.8f} {current:.8f} "
                  f"{roi_color}{roi:+.2f}%{Style.RESET_ALL} {stop:.8f} {trail:.8f}")

        # Calculate portfolio ROI
        portfolio_roi = ((total_value / total_cost) - 1) * 100 if total_cost > 0 else 0
        roi_color = Fore.GREEN if portfolio_roi >= 0 else Fore.RED

        print("-" * 70)
        print(f"Total cost: {total_cost:.6f} SOL | Current value: {total_value:.6f} SOL | "
              f"Total ROI: {roi_color}{portfolio_roi:+.2f}%{Style.RESET_ALL}")
        print(f"{Fore.CYAN}============================{Style.RESET_ALL}\n")

    async def _ata_cleanup_loop(self) -> None:
        """Background task that periodically cleans up unused token accounts to recover rent."""
        print(f"{Fore.CYAN}🧹 Starting ATA cleanup task (every {self.ata_cleanup_interval}s)...{Style.RESET_ALL}")

        try:
            # First cleanup should happen sooner to handle any existing empty accounts
            await asyncio.sleep(60)  # Wait 60 seconds after startup
            await self._perform_ata_cleanup()
            self.last_ata_cleanup_time = monotonic()

            while True:
                try:
                    current_time = monotonic()

                    # Check if it's time for cleanup
                    if current_time - self.last_ata_cleanup_time >= self.ata_cleanup_interval:
                        await self._perform_ata_cleanup()
                        self.last_ata_cleanup_time = current_time

                    # Sleep for a while before checking again
                    await asyncio.sleep(60)  # Check every minute if cleanup is needed

                except asyncio.CancelledError:
                    # Handle cancellation
                    break
                except Exception as e:
                    print(f"{Fore.RED}✗ Error in ATA cleanup loop: {e!s}{Style.RESET_ALL}")
                    await asyncio.sleep(60)  # Wait before retrying

        except asyncio.CancelledError:
            # Handle cancellation
            pass

        print(f"{Fore.YELLOW}ATA cleanup task stopped{Style.RESET_ALL}")

    async def _monitor_tokens_loop(self) -> None:
        """Background task that continuously monitors active tokens."""
        status_update_interval = 8  # how often to print status lines
        trade_counter = {}  # count of "significant" volume deltas
        last_status_times = {}  # last status-print timestamp per token
        price_check_interval = 3.0  # min seconds between on-chain price pulls
        last_price_check = {}  # last price-pull timestamp per token

        while True:
            try:
                expired_tokens = []
                now = monotonic()

                # Batch price checks: first identify which tokens need price updates
                tokens_needing_price = []
                token_keys_needing_price = []
                curve_addresses = []

                for token_key, monitor in list(self.active_monitors.items()):
                    if not monitor.monitoring_active:
                        continue

                    # Calculate volume delta
                    volume = self.token_listener.get_token_volume(token_key)
                    delta = volume - monitor.volume_sol
                    if delta >= 0.5:
                        trade_counter[token_key] = trade_counter.get(token_key, 0) + 1

                    # Check if price needs update
                    last_pulled = last_price_check.get(token_key, 0)
                    need_price = ((monitor.volume_sol >= self.volume_threshold_sol * 0.5) or (delta > 0.5)) and \
                                 ((now - last_pulled) >= price_check_interval)

                    if need_price:
                        token_info = monitor.token_info
                        tokens_needing_price.append(token_info)
                        token_keys_needing_price.append(token_key)
                        curve_addresses.append(token_info.bonding_curve)

                # Batch request price updates if needed
                if tokens_needing_price:
                    try:
                        # Use the batched method to get curve states
                        curve_states = await self.curve_manager.get_multiple_curve_states(curve_addresses,
                                                                                          force_refresh=False)

                        # Update prices from batch results
                        for i, token_key in enumerate(token_keys_needing_price):
                            monitor = self.active_monitors.get(token_key)
                            if monitor:
                                curve_key = str(curve_addresses[i])
                                curve_state = curve_states.get(curve_key)
                                if curve_state:
                                    price = curve_state.calculate_price()
                                    monitor.update_metrics(monitor.volume_sol, price)
                                    last_price_check[token_key] = now
                    except Exception as e:
                        print(f"{Fore.RED}✗ Error batch fetching prices: {e!s}{Style.RESET_ALL}")

                # Now process each active monitor with updated prices
                for token_key, monitor in list(self.active_monitors.items()):
                    if not monitor.monitoring_active:
                        continue

                    short = f"{token_key[:6]}…{token_key[-4:]}"
                    token_info = monitor.token_info

                    # handle expiration
                    if monitor.is_expired:
                        if not monitor.volume_target_reached:
                            print(
                                f"{Fore.RED}⏱️ Timeout: {token_info.symbol} ({short}) – Vol: {monitor.volume_sol:.2f} SOL{Style.RESET_ALL}")
                            expired_tokens.append(token_key)
                            await self.token_listener._unsubscribe_from_token_trades(token_key)
                        continue

                    # Update volume metrics (we already fetched volume above)
                    volume = self.token_listener.get_token_volume(token_key)
                    monitor.update_metrics(volume, monitor.price_sol)

                    # target reached?
                    if monitor.volume_sol >= self.volume_threshold_sol and not monitor.volume_target_reached:
                        monitor.volume_target_reached = True

                        # optional acceleration message
                        accel = monitor.recent_volume_acceleration()
                        accel_msg = f"{Fore.GREEN}🚀 HIGH VELOCITY (+{accel:.1f} SOL){Style.RESET_ALL}" if accel > 5 else ""

                        print(
                            f"{Fore.GREEN}🚀 VOLUME TARGET REACHED: {token_info.symbol} ({short}) {accel_msg}{Style.RESET_ALL}"
                        )
                        print(
                            f"{Fore.GREEN}   Vol: {monitor.volume_sol:.2f} SOL | Time: {monitor.elapsed_time:.1f}s | Trades: {len(monitor.trades)}{Style.RESET_ALL}"
                        )

                        # simple trade-count filter
                        trade_count = len(monitor.trades)
                        min_trades = getattr(self, "min_trades", 4)
                        if trade_count < min_trades:
                            print(
                                f"{Fore.RED}⚠️ TRADE COUNT FILTER: Skipping {token_info.symbol}: only {trade_count} trades (need ≥{min_trades}){Style.RESET_ALL}"
                            )
                            continue

                        # other filters & buy decision
                        if not await self.should_buy_token(token_info, monitor):
                            continue

                        total_positions = len(self.position_manager.positions) + self.pending_buys_count
                        if total_positions >= self.position_manager.max_positions and not self.marry_mode:
                            print(
                                f"{Fore.YELLOW}⚠️ Max positions reached ({total_positions}/{self.position_manager.max_positions}). Skipping {token_info.symbol}.{Style.RESET_ALL}"
                            )
                            continue

                        print(f"{Fore.YELLOW}⚡ Buying token...{Style.RESET_ALL}")
                        self.pending_buys_count += 1
                        asyncio.create_task(self._buy_token_with_lock(token_info))

                    # periodic status updates
                    last_status = last_status_times.get(token_key, 0)
                    if (now - last_status) >= status_update_interval and not monitor.volume_target_reached:
                        if monitor.volume_sol >= 5.0 or (volume - monitor.volume_sol) > 0.5 or trade_counter.get(
                                token_key, 0) > 3:
                            progress = int((monitor.elapsed_time / monitor.monitor_duration) * 20)
                            bar = f"[{'#' * progress}{' ' * (20 - progress)}]"
                            tc = trade_counter.get(token_key, 0)
                            print(
                                f"{Fore.CYAN}📊 {token_info.symbol}: Vol: {monitor.volume_sol:.2f} SOL | {bar} {int(monitor.remaining_time)}s | {tc} trades{Style.RESET_ALL}"
                            )
                        last_status_times[token_key] = now

                # clean out expired
                for key in expired_tokens:
                    self.active_monitors.pop(key, None)
                    last_status_times.pop(key, None)
                    trade_counter.pop(key, None)
                    last_price_check.pop(key, None)

                # prune any caches you need
                self._prune_account_cache()

                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"{Fore.RED}✗ Error in monitoring loop: {e!s}{Style.RESET_ALL}")
                await asyncio.sleep(1)

    async def _buy_token_with_lock(self, token_info: TokenInfo):
        """Buy token with lock to prevent exceeding position limits"""
        try:
            async with self.buying_lock:
                # Double-check position limit inside lock
                total_positions = len(
                    self.position_manager.positions) + self.pending_buys_count - 1  # -1 because we already incremented
                if not self.marry_mode and total_positions >= self.position_manager.max_positions:
                    print(
                        f"{Fore.YELLOW}⚠️ Position limit reached during lock check. Skipping {token_info.symbol}.{Style.RESET_ALL}")
                    return

                # Execute the buy
                await self._buy_token(token_info)
        finally:
            # Always decrement the counter, even if buy fails
            self.pending_buys_count -= 1

    async def _handle_bonding_curve_update(self, account_pubkey: Pubkey, account_data: Dict) -> None:
        """Handle updates to bonding curve accounts from WebSocket."""
        try:
            # Get token info for this bonding curve
            curve_address_str = str(account_pubkey)

            # Find which token this curve belongs to
            token_info = None
            mint_str = None
            for mint, monitoring in self.active_monitoring.items():
                if str(monitoring["token_info"].bonding_curve) == curve_address_str:
                    token_info = monitoring["token_info"]
                    mint_str = mint
                    break

            if not token_info:
                return

            # Get position for this token
            position = self.position_manager.get_position(mint_str)
            if not position:
                return

            try:
                # Import BondingCurveState at the beginning of the method
                from core.curve import BondingCurveState

                # Check if we have data in the correct format
                if isinstance(account_data, dict) and "data" in account_data:
                    # Handle data based on its type
                    raw_data = account_data["data"]

                    # If it's bytes, we can use it directly
                    if isinstance(raw_data, bytes):
                        # Create BondingCurveState directly
                        curve_state = BondingCurveState(raw_data)
                        current_price = curve_state.calculate_price()

                    # If it's base64 string, decode it first
                    elif isinstance(raw_data, str):
                        import base64
                        try:
                            decoded_data = base64.b64decode(raw_data)
                            curve_state = BondingCurveState(decoded_data)
                            current_price = curve_state.calculate_price()
                        except Exception as e:
                            print(f"Error decoding base64 data: {e}, falling back to RPC")
                            curve_state = await self.curve_manager.get_curve_state(
                                account_pubkey,
                                force_refresh=True
                            )
                            current_price = curve_state.calculate_price()

                    # Any other format, use RPC fallback
                    else:
                        print(f"Unrecognized data format, falling back to RPC")
                        curve_state = await self.curve_manager.get_curve_state(
                            account_pubkey,
                            force_refresh=True
                        )
                        current_price = curve_state.calculate_price()
                else:
                    # No data available, fallback to RPC
                    curve_state = await self.curve_manager.get_curve_state(
                        account_pubkey,
                        force_refresh=True
                    )
                    current_price = curve_state.calculate_price()

                # Update position with current price
                position.update(current_price)

                # Access roi_pct as a property
                roi = position.roi_pct

                print(
                    f"{Fore.CYAN}💹 {position.symbol} WebSocket update: {current_price:.8f} | ROI: {roi:.2f}%{Style.RESET_ALL}")

                # Get the token mint from position
                token_mint = position.token_info.mint

                # Check if we need to exit
                exit_info = self.position_manager.update_position_from_websocket(token_mint, current_price)

                # If we should sell based on exit conditions, do it
                if exit_info:
                    await self._sell_token(
                        str(token_mint),
                        position,
                        exit_info
                    )

            except Exception as parse_error:
                print(f"Error parsing curve data for {token_info.symbol}: {parse_error}")

        except Exception as e:
            print(f"Error handling bonding curve update: {e}")
            logging.error(f"Error in WebSocket handler: {e}", exc_info=True)

    # NEW: Check if position is inactive
    def _is_position_inactive(self, position: Position) -> bool:
        """
        Check if a position has been inactive for too long.
        Inactivity means no significant price changes and no WebSocket updates.
        """
        current_time = monotonic()

        # Never consider a position inactive if it's been less than 30 seconds since buying
        if current_time - position.buy_time < 30:
            return False

        # GREEDY MODE: If we've recovered initial investment, be more patient
        inactive_timeout = self.inactive_position_timeout
        if hasattr(position, 'initial_recovered') and position.initial_recovered:
            inactive_timeout = inactive_timeout * 1.5  # 50% more patience after securing initial

        # Check WebSocket inactivity - FIXED: Call is_websocket_monitored as a method
        if position.is_websocket_monitored():
            time_since_update = current_time - position.last_websocket_update
            if time_since_update > inactive_timeout:
                print(
                    f"{Fore.YELLOW}⚠️ Position {position.symbol} WebSocket inactive for {time_since_update:.1f}s{Style.RESET_ALL}")
                return True

        # Check price activity
        if hasattr(position, 'last_valid_price'):
            # Initialize price check time if needed
            if not hasattr(position, 'last_price_check_time'):
                position.last_price_check_time = current_time
                position.last_checked_price = position.last_valid_price
                return False

            time_since_price_check = current_time - position.last_price_check_time

            # Only check for price changes every 5 seconds
            if time_since_price_check >= 5:
                # If price has changed significantly, update the time and price
                if abs(position.last_valid_price - position.last_checked_price) > 0.00000001:
                    position.last_price_check_time = current_time
                    position.last_checked_price = position.last_valid_price
                    return False

                # If price hasn't changed in the timeout period, consider inactive
                if time_since_price_check > inactive_timeout:
                    print(
                        f"{Fore.YELLOW}⚠️ Position {position.symbol} price inactive for {time_since_price_check:.1f}s{Style.RESET_ALL}")
                    return True

        # Default: not inactive
        return False

    async def _monitor_positions_loop(self) -> None:
        """Background task that continuously monitors position status for exit conditions."""
        print(f"{Fore.CYAN}📈 Starting position monitor...{Style.RESET_ALL}")

        update_interval = 5  # Show position updates every 5 seconds
        price_check_interval = 2.0  # Check prices every 2 seconds (reduced from original)
        position_display_interval = 30  # Show position summary every 30 seconds
        last_update_time = {}
        last_price_check = {}  # Track when we last checked price for each token
        last_position_display = 0
        error_counts = {}

        while True:
            try:
                current_time = monotonic()

                # Display position summary periodically
                if (current_time - last_position_display) >= position_display_interval:
                    self._display_position_summary()
                    last_position_display = current_time

                # MODIFIED: Only process positions that need RPC updates (not getting WebSocket updates)
                positions_needing_updates = self.position_manager.get_positions_needing_rpc_updates()

                # Process positions that need RPC updates
                for position in positions_needing_updates:
                    token_key = position.token_key

                    # CRITICAL FIX: Check if position is already being sold
                    if hasattr(position, '_being_sold') and position._being_sold:
                        logger.debug(f"Position {position.symbol} is currently being sold, skipping check")
                        continue

                    try:
                        # Don't check price too frequently to avoid API spam
                        last_price = last_price_check.get(token_key, 0)
                        if current_time - last_price < price_check_interval:
                            continue

                        last_price_check[token_key] = current_time

                        # Get current price with appropriate error handling
                        try:
                            # Use cached curve state when possible
                            curve_state = await self.curve_manager.get_curve_state(
                                position.token_info.bonding_curve,
                                force_refresh=False  # Use cache when possible
                            )
                            current_price = curve_state.calculate_price()

                            # Reset error count on success
                            error_counts[token_key] = 0

                        except Exception as e:
                            # Count consecutive errors
                            error_counts[token_key] = error_counts.get(token_key, 0) + 1

                            # Only log every 10th error to avoid flooding
                            if error_counts[token_key] % 10 == 1:
                                print(
                                    f"{Fore.YELLOW}⚠️ Error getting price for {position.symbol}: Attempt {error_counts[token_key]}{Style.RESET_ALL}")

                            # Skip this iteration after too many errors
                            if error_counts[token_key] > 30:
                                print(
                                    f"{Fore.RED}✗ Too many errors getting price for {position.symbol}. Consider manual intervention.{Style.RESET_ALL}")

                            continue  # Skip to next token

                        # Update position with current price
                        position.update(current_price)

                        # Print position status update less frequently to reduce spam
                        last_time = last_update_time.get(token_key, 0)
                        if (current_time - last_time) >= update_interval:
                            last_update_time[token_key] = current_time

                            # Use position's roi_pct property which now uses the correct formula
                            roi = position.roi_pct

                            # Add debug information about ROI components for verification
                            if position.amount_remaining > 0 and hasattr(position, 'entry_price'):
                                # Calculate components of ROI for debugging
                                unrealized_profit = (current_price - position.entry_price) * position.amount_remaining
                                realized_profit = getattr(position, 'realized_profit', 0)

                                # Show detailed ROI components in log every 30 seconds
                                if (current_time - last_time) >= 30:
                                    print(f"ROI Components for {position.symbol}:")
                                    print(f"  Entry Price: {position.entry_price:.8f}")
                                    print(f"  Current Price: {current_price:.8f}")
                                    print(f"  Unrealized Profit: {unrealized_profit:.6f}")
                                    print(f"  Realized Profit: {realized_profit:.6f}")

                            roi_color = Fore.GREEN if roi >= 0 else Fore.RED
                            print(
                                f"{Fore.CYAN}💼 {position.symbol}: Current: {current_price:.8f} | ROI: {roi_color}{roi:.2f}%{Style.RESET_ALL}")

                        # NEW: Check for inactive positions
                        if self._is_position_inactive(position):
                            print(
                                f"{Fore.RED}⚠️ Position {position.symbol} has been inactive for over {self.inactive_position_timeout}s. Auto-selling.{Style.RESET_ALL}")
                            await self._sell_token(
                                token_key,
                                position,
                                {"reason": "inactive", "price": current_price, "portion": 1.0}
                            )
                            continue

                        # Check for exit conditions only if we have a valid price
                        if current_price > 0:
                            # CRITICAL FIX: Double check we're using position's correct get_exit_info
                            exit_info = position.get_exit_info(current_price)
                            if exit_info:
                                # Add calculated ROI to the exit message for verification
                                exit_info["message"] += f" | ROI: {position.roi_pct:.2f}%"
                                print(exit_info["message"])

                                # Mark position as being sold to prevent duplicate sells
                                position._being_sold = True

                                try:
                                    # Pass the full exit_info dict into _sell_token
                                    await self._sell_token(token_key, position, exit_info)
                                finally:
                                    # Clear flag when sell completes (success or fail)
                                    position._being_sold = False
                                continue

                    except Exception as e:
                        print(f"{Fore.RED}✗ Error monitoring position {position.symbol}: {e!s}{Style.RESET_ALL}")
                        # Clear the selling flag if there was an error
                        if hasattr(position, '_being_sold'):
                            position._being_sold = False

                # MODIFIED: Adjust sleep time based on monitoring needs
                sleep_time = 1 if positions_needing_updates else 3
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                # Handle cancellation
                break
            except Exception as e:
                print(f"{Fore.RED}✗ Error in position monitoring loop: {e!s}{Style.RESET_ALL}")
                await asyncio.sleep(1)

    async def _is_curve_initialized(self, curve_address: Pubkey, program_id: Pubkey) -> bool:
        """
        Check if bonding curve account is owned by `program_id`, using a short‐lived cache
        and `get_multiple_accounts` under the hood to batch RPCs.
        """
        key = str(curve_address)
        now = time.monotonic()

        # return cached result if fresh
        last = self._account_cache_times.get(key, 0)
        if key in self._account_cache and now - last < self._cache_ttl:
            acct = self._account_cache[key]
            return acct is not None and acct.owner == program_id

        # batch‐fetch the single curve account
        response = await self.solana_client.get_multiple_accounts([curve_address], encoding="base64")
        acct = response.value[0]

        # cache
        self._account_cache[key] = acct
        self._account_cache_times[key] = now

        if acct is None:
            return False
        return acct.owner == program_id

    # NEW: Set up WebSocket monitoring for a token
    async def _setup_token_monitoring(self, token_info: TokenInfo, position: Position) -> None:
        """Set up WebSocket monitoring with retries for a token we've bought."""
        max_retries = 3
        retry_count = 0

        while retry_count < max_retries:
            try:
                # Ensure WebSocket is connected
                if not self.helius_ws.websocket:
                    await self.helius_ws.connect()
                    await asyncio.sleep(1)  # Brief pause

                mint_str = str(token_info.mint)
                if mint_str in self.active_monitoring:
                    return  # Already monitoring

                # Initialize monitoring data structure
                self.active_monitoring[mint_str] = {
                    "token_info": token_info,
                    "subscriptions": []
                }

                # Create callback for WebSocket updates
                async def handle_update(data):
                    await self._handle_bonding_curve_update(token_info.bonding_curve, data)

                # Subscribe to bonding curve updates
                bonding_curve = token_info.bonding_curve
                sub_id = await self.helius_ws.accountSubscribe(
                    bonding_curve,
                    handle_update
                )

                if sub_id:
                    self.active_monitoring[mint_str]["subscriptions"].append({
                        "type": "bonding_curve",
                        "account": bonding_curve,
                        "sub_id": sub_id
                    })

                    # Mark the position as using WebSocket monitoring
                    position.mark_as_websocket_monitored()
                    print(f"{Fore.GREEN}✓ WebSocket monitoring set up for {token_info.symbol}{Style.RESET_ALL}")
                    return  # Success

                # No sub_id, try again
                retry_count += 1
                await asyncio.sleep(1)

            except Exception as e:
                print(
                    f"{Fore.YELLOW}⚠️ WebSocket setup error (attempt {retry_count + 1}/{max_retries}): {e!s}{Style.RESET_ALL}")
                retry_count += 1
                await asyncio.sleep(1)

        # Fallback if all retries failed
        print(
            f"{Fore.YELLOW}⚠️ WebSocket monitoring failed after {max_retries} attempts. Using RPC fallback.{Style.RESET_ALL}")

    # NEW: Clean up WebSocket monitoring for a token
    async def _cleanup_token_monitoring(self, token_info) -> None:
        """Clean up WebSocket monitoring for a sold token."""
        try:
            # Handle both TokenInfo objects and string token keys
            if isinstance(token_info, str):
                mint_str = token_info
                token_symbol = "unknown"  # We don't know the symbol if only string is passed
            else:
                mint_str = str(token_info.mint)
                token_symbol = token_info.symbol

            if mint_str not in self.active_monitoring:
                return

            monitoring = self.active_monitoring.pop(mint_str)

            # Unsubscribe from all subscriptions
            for sub in monitoring["subscriptions"]:
                await self.helius_ws.accountUnsubscribe(sub["account"])

            print(f"{Fore.GREEN}✓ Cleaned up WebSocket monitoring for {token_symbol}{Style.RESET_ALL}")

        except Exception as e:
            print(f"{Fore.RED}✗ Error cleaning up WebSocket monitoring: {e!s}{Style.RESET_ALL}")

    async def _buy_token(self, token_info: TokenInfo) -> None:
        """Buy a token that has met the volume threshold."""
        token_key = str(token_info.mint)
        token_address_short = f"{token_key[:6]}...{token_key[-4:]}"

        try:
            # Skip if we're at max positions and not in marry mode - REDUNDANT BUT KEPT AS SAFEGUARD
            total_positions = len(self.position_manager.positions) + self.pending_buys_count - 1
            if not self.marry_mode and total_positions >= self.position_manager.max_positions:
                print(
                    f"{Fore.YELLOW}⚠️ Max positions reached ({total_positions}/{self.position_manager.max_positions}). Skipping {token_info.symbol}.{Style.RESET_ALL}")
                return

            # Update the user field before buying
            token_info.user = self.wallet.pubkey

            # Try to create ATA before buying to ensure it exists
            await self._ensure_ata_exists(token_info.mint)

            # Ensure bonding curve is initialized before buying - WITH CACHING
            curve_initialized = False
            for attempt in range(4):  # Reduced from 8 to 4 attempts
                curve_initialized = await self._is_curve_initialized(
                    token_info.bonding_curve,
                    PumpAddresses.PROGRAM
                )

                if curve_initialized:
                    logger.debug(f"✓ Bonding curve verified for {token_info.symbol}")
                    break

                wait_time = 2 + attempt  # Increasing delays
                logger.debug(f"⏳ Waiting for bonding curve initialization... (attempt {attempt + 1}/4) {wait_time}s")
                await asyncio.sleep(wait_time)

            if not curve_initialized:
                print(f"{Fore.RED}🔍 Bonding curve not initialized for {token_info.symbol}. Skipping.{Style.RESET_ALL}")
                return

            # Execute buy operation directly - no pre-checking balance
            buy_result: TradeResult = await self.buyer.execute(token_info)

            if buy_result.success:
                print(f"{Fore.GREEN}✅ BOUGHT {token_info.symbol} ({token_address_short}){Style.RESET_ALL}")
                print(
                    f"{Fore.GREEN}   Amount: {buy_result.amount:.6f} tokens | Price: {buy_result.price:.8f} SOL{Style.RESET_ALL}")
                print(f"{Fore.GREEN}   TX: {buy_result.tx_signature}{Style.RESET_ALL}")

                self.traded_mints.add(token_info.mint)
                self._log_trade("buy", token_info, buy_result.price, buy_result.amount, buy_result.tx_signature)

                # Short wait to let WebSocket catch up
                await asyncio.sleep(1)

                if self.marry_mode:
                    # In marry mode, just hold the token
                    print(f"{Fore.YELLOW}💍 MARRY MODE: Holding {token_info.symbol} indefinitely{Style.RESET_ALL}")
                else:
                    # Verify token balance after purchase
                    print(f"{Fore.YELLOW}⏱️ Verifying token balance after purchase...{Style.RESET_ALL}")

                    # Use our token_account_manager to verify token balance
                    balance, token_account_address = await self.token_account_manager.verify_token_balance(
                        self.wallet.pubkey,
                        str(token_info.mint),
                        expected_amount=buy_result.amount
                    )

                    if balance is None or balance < buy_result.amount * 0.9:
                        print(
                            f"{Fore.RED}✗ Failed to buy {token_info.symbol}: Transaction confirmed but token balance not verified{Style.RESET_ALL}")
                        return

                    print(f"{Fore.GREEN}✅ Token balance verified: {balance:.6f}{Style.RESET_ALL}")

                    # Add the token to active monitoring in balance monitor
                    self.balance_monitor.add_active_token(token_info.mint)

                    # Create a new position and add to position manager
                    # Get dynamic parameters based on token metrics
                    monitor = self.active_monitors.get(token_key)
                    exit_params = self.get_dynamic_exit_parameters(token_info, monitor) if monitor else {
                        "take_profit_tiers": [1.3, 1.6, 2.0],  # GREEDY MODE default tiers
                        "sell_portions": [0.5, 0.5, 1.0],  # GREEDY MODE default portions
                        "stop_loss_pct": 0.10,
                        "trailing_stop_pct": 0.08  # GREEDY MODE default trailing stop
                    }

                    # Calculate current floor ratio for optimal stop loss
                    floor_ratio = buy_result.price / self.PUMP_FUN_FLOOR_PRICE_SOL
                    optimal_stop = self.calculate_optimal_stop_loss(buy_result.price, floor_ratio)

                    # Override the stop loss with calculated optimal value
                    stop_loss_pct = (buy_result.price - optimal_stop) / buy_result.price

                    # Create position with dynamic parameters
                    position = await self.position_manager.add_position(
                        token_info,
                        buy_result,
                        take_profit_tiers=exit_params["take_profit_tiers"],
                        sell_portions=exit_params["sell_portions"],  # GREEDY MODE: Pass sell portions
                        stop_loss_pct=stop_loss_pct,  # Use calculated optimal stop loss
                        trailing_stop_pct=exit_params["trailing_stop_pct"]
                    )

                    # Store token account in position
                    if token_account_address:
                        position.token_account = token_account_address
                        position.balance_source = "verification"
                        print(
                            f"{Fore.GREEN}✓ Token account stored for {position.symbol}: {token_account_address}{Style.RESET_ALL}")

                    # Print position opened message
                    print(f"{Fore.GREEN}📈 Position opened: {position.symbol} at {position.entry_price:.8f} SOL")
                    print(f"   Stop loss: {optimal_stop:.8f} SOL | Take profits: {exit_params['take_profit_tiers']}")
                    print(f"   {Fore.CYAN}Price/Floor ratio: {floor_ratio:.1f}x{Style.RESET_ALL}")

                    self.analytics.log_open_position(
                        token_symbol=token_info.symbol,
                        token_address=token_key,
                        entry_price=buy_result.price,
                        amount=buy_result.amount,
                        entry_time=position.buy_time,
                        entry_tx=buy_result.tx_signature
                    )

                    # NEW: Set up WebSocket monitoring for this position
                    await self._setup_token_monitoring(token_info, position)

                    # Add delay before monitoring to ensure token account is properly initialized
                    print(
                        f"{Fore.YELLOW}⏱️ Waiting for token account initialization before monitoring...{Style.RESET_ALL}")
                    await asyncio.sleep(3)  # Shorter wait, we already verified the balance

            else:
                print(f"{Fore.RED}✗ Failed to buy {token_info.symbol}: {buy_result.error_message}{Style.RESET_ALL}")

        except Exception as e:
            print(f"{Fore.RED}✗ Error buying {token_info.symbol}: {e!s}{Style.RESET_ALL}")

        finally:
            # Remove from active monitors
            if token_key in self.active_monitors:
                self.active_monitors[token_key].monitoring_active = False

    async def _ensure_ata_exists(self, mint: Pubkey) -> bool:
        """Ensure the Associated Token Account exists before trying to buy."""
        try:
            print(f"{Fore.YELLOW}⚡ Creating token account before purchase...{Style.RESET_ALL}")

            # Instead of checking, just add this token to active monitoring
            # This will ensure it's watched after purchase without additional checks
            self.balance_monitor.add_active_token(mint)

            # Let the transaction create the ATA - no need to check in advance
            return True
        except Exception as e:
            logger.warning(f"Failed to create token account: {e}")
            # Continue anyway as the buy transaction will create it if needed
            return True

    async def _perform_ata_cleanup(self) -> None:
        """Clean up empty token accounts to recover rent."""
        try:
            # First, query for token accounts and identify empty ones
            empty_accounts = []  # Initialize the list here

            # Get list of token accounts owned by this wallet
            token_accounts = await self.client.get_token_accounts_by_owner(
                self.wallet.pubkey,
                {"programId": SystemAddresses.TOKEN_PROGRAM}
            )

            if not token_accounts or not token_accounts.value:
                logger.debug("No token accounts found for cleanup")
                return

            # Find empty accounts (zero balance)
            for account_info in token_accounts.value:
                try:
                    account_data = account_info.account.data.parsed['info']
                    mint = account_data.get('mint')
                    balance = int(account_data['tokenAmount']['amount'])

                    if balance == 0:
                        empty_accounts.append({
                            "address": account_info.pubkey,
                            "mint": mint
                        })
                except (KeyError, TypeError) as e:
                    logger.error(f"Error parsing token account data: {e}")
                    continue

            # Now proceed with your existing code to close them
            if not empty_accounts:
                logger.debug("No empty token accounts found for cleanup")
                return

            count = len(empty_accounts)
            print(f"{Fore.CYAN}🧹 Found {count} empty token accounts to clean up{Style.RESET_ALL}")

            # Close accounts in batches to avoid transaction size limits
            batch_size = 5
            for i in range(0, count, batch_size):
                batch = empty_accounts[i:i + batch_size]

                # Skip accounts for active positions
                filtered_batch = []
                for account in batch:
                    mint_str = account.get("mint")
                    if not mint_str:
                        continue

                    # Check if this mint belongs to an active position
                    is_active = False
                    for position in self.position_manager.get_positions().values():
                        if str(position.token_info.mint) == mint_str:
                            is_active = True
                            break

                    if not is_active:
                        filtered_batch.append(account)

                if not filtered_batch:
                    continue

                print(f"{Fore.CYAN}🧹 Cleaning up batch of {len(filtered_batch)} accounts{Style.RESET_ALL}")

                # Close the accounts
                result = await self.token_account_manager.close_token_accounts(
                    filtered_batch,
                    self.wallet
                )

                if result and result.get("success"):
                    print(f"{Fore.GREEN}✅ Successfully closed {len(filtered_batch)} empty accounts{Style.RESET_ALL}")
                    print(f"{Fore.GREEN}   TX: {result.get('tx_signature')}{Style.RESET_ALL}")
                else:
                    print(f"{Fore.RED}❌ Failed to close accounts: {result.get('error')}{Style.RESET_ALL}")

                # Wait between batches to avoid rate limits
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Error during ATA cleanup: {str(e)}")

        except Exception as e:
            print(f"{Fore.RED}✗ Error during ATA cleanup: {e!s}{Style.RESET_ALL}")

    async def _sell_token(self, token_key: str, position: Position, exit_info: dict) -> None:
        """Sell a token that has met exit conditions with improved balance verification."""
        # CRITICAL FIX: Prevent duplicate sells
        if hasattr(position, '_active_sell') and position._active_sell:
            print(
                f"{Fore.YELLOW}⚠️ Sell operation already in progress for {position.symbol}, skipping duplicate sell{Style.RESET_ALL}")
            return

        # Mark sell as active to prevent duplicates
        position._active_sell = True

        try:
            token_info = position.token_info
            reason = exit_info["reason"]
            portion = exit_info.get("portion", 1.0)
            tier = exit_info.get("tier", None)
            price = exit_info.get("price", 0)

            token_address_short = f"{token_key[:6]}...{token_key[-4:]}"
            is_tier_sale = "tier" in reason and portion < 1.0

            roi = position.roi_pct
            roi_color = Fore.GREEN if roi >= 0 else Fore.RED

            print(
                f"{Fore.YELLOW}🚪 Exit: {position.symbol} due to {reason} | ROI: {roi_color}{roi:.2f}%{Style.RESET_ALL}")

            balance = None
            token_account_address = None
            secondary_balance = None
            token_address = str(token_info.mint)

            # IMPROVEMENT 1: cached ATA
            if hasattr(position, 'token_account') and position.token_account:
                token_account_address = position.token_account
                print(f"Using cached token account: {token_account_address}")

            # IMPROVEMENT 2: websocket
            if hasattr(self, 'balance_monitor') and hasattr(self.balance_monitor, 'helius_monitor'):
                ws_balance = self.balance_monitor.helius_monitor.get_token_balance(token_address)
                ws_token_account = self.balance_monitor.helius_monitor.get_ata(token_address)
                if ws_balance is not None:
                    print(f"{Fore.GREEN}✓ Found token balance from WebSocket: {ws_balance}{Style.RESET_ALL}")
                    balance = ws_balance
                    if ws_token_account:
                        token_account_address = ws_token_account
                        position.token_account = token_account_address

            # IMPROVEMENT 4: RPC via token_account_manager
            try:
                secondary_balance, secondary_token_account = await self.token_account_manager.verify_token_balance(
                    self.wallet.pubkey,
                    token_address
                )
                print(f"RPC token balance check: {secondary_balance}")
                if balance is None and secondary_balance and secondary_balance > 0:
                    balance = secondary_balance
                    if not token_account_address and secondary_token_account:
                        token_account_address = secondary_token_account
                        position.token_account = token_account_address
            except Exception as e:
                print(f"{Fore.RED}Token account verification error (ignoring): {e!s}{Style.RESET_ALL}")

            # IMPROVEMENT 5: direct ATA derivation - FIXED
            if balance is None or balance <= 0:
                from spl.token.instructions import get_associated_token_address
                from solders.pubkey import Pubkey
                mint_key = Pubkey.from_string(token_address)
                owner_key = self.wallet.pubkey
                derived_ata = get_associated_token_address(owner_key, mint_key)

                try:
                    client = await self.solana_client.get_client()
                    response = await client.get_multiple_accounts([derived_ata], encoding="base64")
                    acct = response.value[0]
                    if acct is not None and acct.data:
                        # FIXED: Use proper token account layout parsing
                        from spl.token._layouts import ACCOUNT_LAYOUT as TOKEN_ACCOUNT_LAYOUT
                        parsed = TOKEN_ACCOUNT_LAYOUT.parse(acct.data)
                        if parsed.amount > 0:
                            balance = parsed.amount
                            token_account_address = derived_ata
                            print(
                                f"{Fore.GREEN}✓ Found token through direct ATA derivation: {balance}{Style.RESET_ALL}")
                            position.token_account = derived_ata
                except Exception as e:
                    print(f"{Fore.RED}Direct ATA derivation failed (ignoring): {e!s}{Style.RESET_ALL}")

            # Execute the sell transaction if we have balance and token account
            if balance and balance > 0 and token_account_address:
                try:
                    # Use your seller to execute the sale
                    trade_result = await self.seller.execute(
                        token_info,
                        amount_pct=portion
                    )

                    if trade_result.success:
                        print(f"{Fore.GREEN}✅ SOLD {position.symbol}: {trade_result.tx_signature}{Style.RESET_ALL}")

                        # Update position status based on portion sold
                        if portion >= 1.0:
                            # Complete position close - FIXED METHOD NAME
                            await self.position_manager.remove_position(token_key)

                            # Clean up monitoring
                            if hasattr(self, "_cleanup_token_monitoring"):
                                await self._cleanup_token_monitoring(token_key)
                        else:
                            # Partial exit for tiered take-profits
                            # Update realized profit
                            position.update_realized_profit(balance * portion, trade_result.price)

                        # Track profit and update stats
                        if hasattr(self, "_track_profit"):
                            await self._track_profit(token_key, position, int(balance * portion), reason)

                        return True
                    else:
                        print(f"{Fore.RED}✗ Sell transaction failed: {trade_result.error_message}{Style.RESET_ALL}")
                        return False

                except Exception as e:
                    print(f"{Fore.RED}✗ Sell execution error: {e!s}{Style.RESET_ALL}")
                    return False
            else:
                print(f"{Fore.RED}✗ Cannot sell: No valid balance ({balance}) or token account{Style.RESET_ALL}")
                return False

        except Exception as e:
            print(f"{Fore.RED}✗ Error selling {position.symbol}: {e!s}{Style.RESET_ALL}")
            return False
        finally:
            # always clear the active-sell flag
            position._active_sell = False

    async def _retry_sell_after_delay(self, token_key: str, position: Position, exit_info: dict,
                                      delay_seconds: float = 2.0):
        """Helper method to retry selling after a delay."""
        try:
            print(
                f"{Fore.YELLOW}⏱️ Waiting {delay_seconds} seconds before retrying sell for {position.symbol}...{Style.RESET_ALL}")
            await asyncio.sleep(delay_seconds)
            print(f"{Fore.YELLOW}🔄 Retrying sell for {position.symbol}...{Style.RESET_ALL}")

            # Clear active sell flag before retrying
            if hasattr(position, '_active_sell'):
                position._active_sell = False

            # Retry the sell
            await self._sell_token(token_key, position, exit_info)
        except Exception as e:
            print(f"{Fore.RED}Error in retry sell: {e!s}{Style.RESET_ALL}")
            # Make sure active sell flag is cleared
            if hasattr(position, '_active_sell'):
                position._active_sell = False


    # Add this new helper method to sell remaining tokens
    async def _sell_remaining_tokens(self, token_info, position, reason, last_price):
        """
        Attempt to sell remaining tokens in wallet after a supposedly complete sell.
        This addresses the issue of tokens being left behind.
        """
        try:
            # Wait a moment for any transactions to settle
            await asyncio.sleep(2)

            token_key = str(token_info.mint)

            # Check wallet balance to see if there are still tokens
            balance = None
            if hasattr(self, 'balance_monitor') and hasattr(self.balance_monitor, 'helius_monitor'):
                balance = self.balance_monitor.helius_monitor.get_token_balance(token_key)

            if not balance or balance <= 0:
                # Double check with token_account_manager
                balance, _ = await self.token_account_manager.verify_token_balance(
                    self.wallet.pubkey,
                    token_key
                )

            if balance and balance > 100:
                print(f"{Fore.YELLOW}📊 Selling remaining {balance:.6f} {position.symbol} tokens{Style.RESET_ALL}")

                # Create a new exit_info for the remaining tokens
                exit_info = {
                    "reason": f"remaining_{reason}",
                    "price": last_price,
                    "portion": 1.0
                }

                # Attempt to sell
                await self._sell_token(token_key, position, exit_info)
            else:
                # No significant tokens remaining, close position
                await self.position_manager.remove_position(token_key)
                self.balance_monitor.remove_active_token(token_info.mint)
                await self._cleanup_token_monitoring(token_info)

        except Exception as e:
            logger.error(f"Error in _sell_remaining_tokens: {e}")
            # If there was an error, remove the position to prevent further issues
            await self.position_manager.remove_position(str(token_info.mint))

    def _log_trade(
            self,
            action: str,
            token_info: TokenInfo,
            price: float,
            amount: float,
            tx_hash: str | None,
            reason: str = "manual",
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
                "reason": reason,
            }

            with open("trades/trades.log", "a") as log_file:
                log_file.write(json.dumps(log_entry) + "\n")
        except Exception as e:
            print(f"{Fore.RED}✗ Failed to log trade information: {e!s}{Style.RESET_ALL}")
