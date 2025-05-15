# src/trading/helius_token_monitor.py
import asyncio
import websockets
import json
import time
from typing import Dict, Optional, Set, Tuple, Callable, Coroutine, Any
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address
from core.client import SolanaClient
from utils.logger import get_logger
from colorama import Fore, Style
from client.token_account_manager import TokenAccountManager

logger = get_logger(__name__)

class HeliusWebSocketMonitor:
    """Real-time token balance monitoring using Helius WebSockets"""
    
    def __init__(self, helius_api_key: str, wallet_pubkey: Pubkey):
        self.helius_api_key = helius_api_key
        self.helius_wss_url = f"wss://mainnet.helius-rpc.com/?api-key={helius_api_key}"
        self.wallet_pubkey = str(wallet_pubkey)
        
        self.ws = None
        self.running = False
        self.receiver_task = None
        
        # Store monitoring state
        self.monitored_accounts: Dict[str, str] = {"Wallet (SOL)": self.wallet_pubkey}
        self.token_labels: Dict[str, str] = {}  # Mint address -> readable label
        self.sub_labels: Dict[int, str] = {}  # Sub ID -> label
        self.subscription_ids: Dict[int, str] = {}  # Result ID -> label
        self.next_sub_id = 1
        self.current_mints: Set[str] = set()
        
        # Balance tracking
        self.sol_balance: float = 0.0
        self.token_balances: Dict[str, float] = {}
        self.last_update: Dict[str, float] = {}
        
        # For reconnection
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        
        # Callback functions
        self.on_balance_change: Optional[Callable[[str, float], Coroutine[Any, Any, None]]] = None
        self.on_sol_change: Optional[Callable[[float], Coroutine[Any, Any, None]]] = None
    
    def get_ata(self, mint: str) -> str:
        """Get associated token account address for this wallet and mint"""
        wallet_pk = Pubkey.from_string(self.wallet_pubkey)
        mint_pk = Pubkey.from_string(mint)
        ata = get_associated_token_address(wallet_pk, mint_pk)
        return str(ata)
    
    async def subscribe(self, pubkey: str, label: str) -> None:
        """Subscribe to an account for updates"""
        if not self.ws:
            logger.warning(f"WebSocket not connected. Queuing subscription to {label}")
            return
            
        try:
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": self.next_sub_id,
                "method": "accountSubscribe",
                "params": [
                    pubkey,
                    {"encoding": "jsonParsed", "commitment": "confirmed"}
                ]
            }
            
            self.sub_labels[self.next_sub_id] = label
            self.next_sub_id += 1
            
            await self.ws.send(json.dumps(subscribe_msg))
            logger.info(f"Subscribed to {label}: {pubkey}")
        except Exception as e:
            logger.error(f"Error subscribing to {label}: {e}")
    
    async def add_token_monitor(self, mint: str, symbol: Optional[str] = None) -> None:
        """Add a token to monitor"""
        if mint in self.current_mints:
            return
            
        try:
            ata = self.get_ata(mint)
            short_mint = f"{mint[:6]}...{mint[-4:]}"
            label = symbol or f"TOKEN {short_mint}"
            self.token_labels[mint] = label
            
            self.monitored_accounts[label] = ata
            self.current_mints.add(mint)
            
            if self.ws and self.running:
                await self.subscribe(ata, label)
        except Exception as e:
            logger.error(f"Failed to add token monitor for {mint}: {e}")
    
    def parse_account_update(self, msg) -> Optional[Dict]:
        """Parse account update message from WebSocket"""
        if "method" not in msg or msg["method"] != "accountNotification":
            return None
            
        params = msg.get("params", {})
        result = params.get("result", {})
        value = result.get("value", {})
        
        # Parse SOL account
        if value.get("owner") == "11111111111111111111111111111111":
            lamports = value.get("lamports", 0)
            sol = lamports / 1_000_000_000
            old_balance = self.sol_balance
            self.sol_balance = sol
            self.last_update["SOL"] = time.time()
            
            return {
                "type": "sol",
                "balance": sol,
                "lamports": lamports,
                "change": sol - old_balance
            }
            
        # Parse Token account
        if value.get("owner") == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
            data = value.get("data", {})
            parsed = data.get("parsed", {})
            info = parsed.get("info", {})
            token_amount = info.get("tokenAmount", {})
            mint = info.get("mint", "Unknown mint")
            amount = float(token_amount.get("uiAmount", 0))
            decimals = token_amount.get("decimals", 0)
            
            old_balance = self.token_balances.get(mint, 0)
            self.token_balances[mint] = amount
            self.last_update[mint] = time.time()
            
            return {
                "type": "token",
                "mint": mint,
                "balance": amount,
                "decimals": decimals,
                "change": amount - old_balance,
                "label": self.token_labels.get(mint, f"Token {mint[:6]}...")
            }
            
        return None
    
    async def message_receiver(self) -> None:
        """Process incoming WebSocket messages"""
        if not self.ws:
            return
            
        while self.running:
            try:
                raw_msg = await self.ws.recv()
                try:
                    msg = json.loads(raw_msg)
                except Exception:
                    logger.error(f"Could not parse WebSocket message: {raw_msg}")
                    continue
                
                # Handle subscription confirmation
                if "result" in msg and "id" in msg and msg.get("jsonrpc") == "2.0":
                    sub_id = msg["id"]
                    sub_result = msg["result"]
                    self.subscription_ids[sub_result] = self.sub_labels.get(sub_id, f"Sub{sub_result}")
                    logger.debug(f"Subscription confirmed: {self.sub_labels.get(sub_id, 'Unknown')}")
                    continue
                
                # Handle account notification
                if "method" in msg and msg["method"] == "accountNotification":
                    sub_number = msg.get("params", {}).get("subscription")
                    label = self.subscription_ids.get(sub_number, f"Sub{sub_number}")
                    
                    parsed = self.parse_account_update(msg)
                    if not parsed:
                        continue
                    
                    # Handle SOL balance update
                    if parsed["type"] == "sol":
                        if abs(parsed["change"]) > 0.00001:  # Only log significant changes
                            logger.info(f"SOL balance updated: {parsed['balance']:.6f} SOL ({parsed['change']:+.6f})")
                        
                        # Call callback if defined
                        if self.on_sol_change:
                            asyncio.create_task(self.on_sol_change(parsed["balance"]))
                    
                    # Handle token balance update
                    elif parsed["type"] == "token":
                        mint = parsed["mint"]
                        token_label = self.token_labels.get(mint, f"Token {mint[:6]}...")
                        
                        # Only log changes
                        if abs(parsed["change"]) > 0:
                            logger.info(f"Token {token_label} balance updated: {parsed['balance']} ({parsed['change']:+})")
                        
                        # Call callback if defined
                        if self.on_balance_change:
                            asyncio.create_task(self.on_balance_change(mint, parsed["balance"]))
            
            except websockets.ConnectionClosed:
                logger.warning("WebSocket connection closed.")
                
                if self.running:
                    # Try to reconnect
                    self.reconnect_attempts += 1
                    if self.reconnect_attempts <= self.max_reconnect_attempts:
                        wait_time = min(2 ** self.reconnect_attempts, 60)  # Exponential backoff, max 60s
                        logger.info(f"Attempting to reconnect in {wait_time}s (attempt {self.reconnect_attempts}/{self.max_reconnect_attempts})...")
                        await asyncio.sleep(wait_time)
                        
                        connected = await self.connect()
                        if connected:
                            self.reconnect_attempts = 0
                            continue
                    
                    logger.error(f"Failed to reconnect after {self.reconnect_attempts} attempts.")
                break
            except Exception as e:
                logger.error(f"Error in WebSocket receiver: {e}")
                await asyncio.sleep(1)  # Prevent tight loop on errors
    
    async def connect(self) -> bool:
        """Connect to Helius WebSocket"""
        try:
            self.ws = await asyncio.wait_for(
                websockets.connect(self.helius_wss_url), 
                timeout=10.0
            )
            
            # Subscribe to SOL wallet
            await self.subscribe(self.wallet_pubkey, "Wallet (SOL)")
            
            # Subscribe to all known token accounts
            for mint in self.current_mints:
                await self.add_token_monitor(mint)
            
            logger.info(f"{Fore.GREEN}Connected to Helius WebSocket ✓{Style.RESET_ALL}")
            return True
            
        except asyncio.TimeoutError:
            logger.error("Connection to Helius WebSocket timed out")
            self.ws = None
            return False
            
        except Exception as e:
            logger.error(f"Failed to connect to Helius WebSocket: {e}")
            self.ws = None
            return False
    
    async def start(self) -> bool:
        """Start the WebSocket monitor"""
        if self.running:
            return True
            
        self.running = True
        connected = await self.connect()
        
        if connected and self.ws:
            print(f"{Fore.CYAN}📡 Helius WebSocket monitor started for {self.wallet_pubkey}{Style.RESET_ALL}")
            self.receiver_task = asyncio.create_task(self.message_receiver())
            return True
        else:
            self.running = False
            return False
    
    async def stop(self) -> None:
        """Stop the WebSocket monitor"""
        self.running = False
        
        if self.receiver_task:
            self.receiver_task.cancel()
            try:
                await self.receiver_task
            except asyncio.CancelledError:
                pass
            self.receiver_task = None
        
        if self.ws:
            await self.ws.close()
            self.ws = None
        
        logger.info("Helius WebSocket monitor stopped")
    
    def get_token_balance(self, mint: str) -> Optional[float]:
        """Get cached token balance"""
        return self.token_balances.get(mint)
    
    def get_sol_balance(self) -> float:
        """Get cached SOL balance"""
        return self.sol_balance

    # Add this method to HeliusWebSocketMonitor class
    def update_sol_balance(self, balance: float) -> None:
        """
        Update SOL balance manually when discovered via RPC.
        This helps keep WebSocket monitor in sync when WebSocket updates are delayed.

        Args:
            balance: The SOL balance to set
        """
        old_balance = self.sol_balance
        self.sol_balance = balance
        self.last_update["SOL"] = time.time()

        # Log significant changes
        if abs(balance - old_balance) > 0.0001:
            logger.info(f"SOL balance manually updated: {balance:.6f} SOL ({balance - old_balance:+.6f})")




class WalletBalanceMonitor:
    """Monitors and caches token balances with Helius WebSockets."""
    
    def __init__(self, client: SolanaClient, wallet_pubkey: Pubkey, helius_api_key: str = None):
        self.client = client
        self.wallet = wallet_pubkey
        self.balances: Dict[str, float] = {}
        self.last_update: Dict[str, float] = {}
        self._monitor_task = None
        
        # Track failed tokens to avoid spamming logs
        self.failed_tokens = set()
        
        # Track ATAs that have been created
        self.created_atas = set()
        
        # RPC optimization: track refresh times and limit frequency
        self.refresh_cooldown = 10.0  # Only refresh a token once per 10 seconds in background
        self.active_tokens = set()  # Only monitor tokens we're actually trading
        
        # Always use the hardcoded Helius API key for reliability
        self.helius_api_key = "5ae4ff39-29d5-44b1-87b0-84dabce1c645"
        self.helius_monitor = None
        
        # Always initialize the Helius WebSocket monitor with our hardcoded key
        self.helius_monitor = HeliusWebSocketMonitor(
            helius_api_key=self.helius_api_key,
            wallet_pubkey=wallet_pubkey
        )
            
        # Initialize our improved token account manager
        self.token_account_manager = TokenAccountManager(client)
        
        # Connect token manager to helius monitor
        self.token_account_manager.set_helius_monitor(self.helius_monitor)
        
        # Connect callbacks for helius monitor
        self.helius_monitor.on_balance_change = self._on_token_balance_change
        self.helius_monitor.on_sol_change = self._on_sol_balance_change
        
    async def _on_token_balance_change(self, mint: str, balance: float) -> None:
        """Callback for token balance changes from WebSocket"""
        old_balance = self.balances.get(mint, None)
        self.balances[mint] = balance
        self.last_update[mint] = time.monotonic()
        
        # Only print if it's a significant change for a token we're tracking
        if mint in self.active_tokens and (old_balance is None or abs(balance - old_balance) > 0.00001):
            token_label = self.helius_monitor.token_labels.get(mint, f"Token {mint[:6]}...")
            logger.info(f"{Fore.CYAN}💰 WebSocket: {token_label} balance updated: {balance}{Style.RESET_ALL}")
            
            # Remove from failed tokens if previously failed
            if mint in self.failed_tokens:
                self.failed_tokens.remove(mint)
        
    async def _on_sol_balance_change(self, balance: float) -> None:
        """Callback for SOL balance changes from WebSocket"""
        old_balance = self.balances.get("SOL", None)
        self.balances["SOL"] = balance
        self.last_update["SOL"] = time.monotonic()
        
        # Only print if it's a significant change
        if old_balance is None or abs(balance - old_balance) > 0.001:
            logger.info(f"{Fore.CYAN}💰 WebSocket: SOL balance updated: {balance:.6f} SOL{Style.RESET_ALL}")
        
    async def start(self):
        """Start the balance monitoring."""
        print(f"{Fore.GREEN}Started wallet balance monitor for {self.wallet}{Style.RESET_ALL}")
        
        # Always start Helius WebSocket monitor
        started = await self.helius_monitor.start()
        if started:
            print(f"{Fore.GREEN}✓ Helius WebSocket monitor started successfully{Style.RESET_ALL}")
        else:
            print(f"{Fore.YELLOW}⚠️ Helius WebSocket monitor failed to start, falling back to RPC{Style.RESET_ALL}")
        
        # Also start traditional RPC monitor as backup
        self._monitor_task = asyncio.create_task(self._background_monitor())
        
    async def stop(self):
        """Stop all monitoring."""
        # Stop Helius WebSocket
        if self.helius_monitor:
            await self.helius_monitor.stop()
        
        # Stop background RPC monitoring
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            
        logger.info("Stopped wallet balance monitor")
            
    def add_active_token(self, mint: Pubkey):
        """Add a token to the active monitoring list."""
        mint_str = str(mint)
        self.active_tokens.add(mint_str)
        
        # Also add to WebSocket monitor
        if self.helius_monitor:
            asyncio.create_task(self.helius_monitor.add_token_monitor(mint_str))
        
    def remove_active_token(self, mint: Pubkey):
        """Remove a token from the active monitoring list."""
        mint_str = str(mint)
        if mint_str in self.active_tokens:
            self.active_tokens.remove(mint_str)
            
    async def get_token_balance(self, mint: Pubkey, force_refresh: bool = False) -> Optional[float]:
        """Get token balance with WebSocket priority, falling back to RPC."""
        mint_str = str(mint)
        
        # Add to active tokens when explicitly checked
        self.add_active_token(mint)
        
        # First check if WebSocket has a balance
        if self.helius_monitor:
            ws_balance = self.helius_monitor.get_token_balance(mint_str)
            if ws_balance is not None and ws_balance > 0:
                # Update our own cache
                self.balances[mint_str] = ws_balance
                self.last_update[mint_str] = time.monotonic()
                return ws_balance
            
        # Otherwise try RPC if we need to force refresh or don't have a cached value
        if force_refresh or mint_str not in self.balances:
            # Use our robust token_account_manager
            balance, _ = await self.token_account_manager.verify_token_balance(
                self.wallet, 
                mint_str
            )
            
            if balance is not None:
                # Update our cache
                self.balances[mint_str] = balance
                self.last_update[mint_str] = time.monotonic()
                
                # If successful, remove from failed tokens
                if balance > 0 and mint_str in self.failed_tokens:
                    self.failed_tokens.remove(mint_str)
                    
                return balance
        
        # Return cached balance or 0
        return self.balances.get(mint_str, 0.0)
    
    async def get_token_account(self, mint: Pubkey) -> Tuple[Optional[float], Optional[str]]:
        """
        Get both the token balance and account address.
        Returns a tuple of (balance, account_address)
        """
        mint_str = str(mint)
        
        # First try WebSocket
        if self.helius_monitor:
            ws_balance = self.helius_monitor.get_token_balance(mint_str)
            if ws_balance is not None and ws_balance > 0:
                # We have a balance from WebSocket, get the ATA
                ata = self.helius_monitor.get_ata(mint_str)
                return ws_balance, ata
        
        # Otherwise use direct RPC
        balance, token_account = await self.token_account_manager.verify_token_balance(
            self.wallet,
            mint_str
        )
        
        # Update our cache if we got a valid balance
        if balance is not None:
            self.balances[mint_str] = balance
            self.last_update[mint_str] = time.monotonic()
            
            # If we successfully got a balance, remove from failed tokens
            if balance > 0 and mint_str in self.failed_tokens:
                self.failed_tokens.remove(mint_str)
        
        return balance, token_account
    
    async def _background_monitor(self):
        """Background task to refresh balances (as backup to WebSocket)."""
        try:
            await asyncio.sleep(5)  # Wait a moment before starting
            
            while True:
                try:
                    # Only check tokens that aren't being reported by WebSocket
                    tokens_to_refresh = []
                    
                    current_time = time.monotonic()
                    
                    for mint_str in self.active_tokens:
                        # Skip if WebSocket has reported this recently
                        if self.helius_monitor and self.helius_monitor.get_token_balance(mint_str) is not None:
                            ws_update_time = self.helius_monitor.last_update.get(mint_str, 0)
                            if time.time() - ws_update_time < 30:  # Skip if WebSocket updated in last 30s
                                continue
                        
                        # Skip failed tokens (reduces RPC calls)
                        if mint_str in self.failed_tokens:
                            continue
                        
                        # Skip if we've refreshed recently (reduces RPC calls)
                        last_refresh = self.last_update.get(mint_str, 0)
                        if current_time - last_refresh < self.refresh_cooldown:
                            continue
                        
                        tokens_to_refresh.append(mint_str)
                    
                    # Process only a few tokens at a time
                    for mint_str in tokens_to_refresh[:3]:
                        try:
                            # Use the token_account_manager
                            balance, _ = await self.token_account_manager.verify_token_balance(
                                self.wallet,
                                mint_str
                            )
                            
                            if balance is not None:
                                # Update cache
                                self.balances[mint_str] = balance
                                self.last_update[mint_str] = current_time
                                
                                # If successful, remove from failed tokens
                                if balance > 0 and mint_str in self.failed_tokens:
                                    self.failed_tokens.remove(mint_str)
                                    
                        except Exception as e:
                            # Only log serious errors, not just missing tokens
                            if "could not find account" not in str(e):
                                logger.error(f"Error in balance monitor refresh: {e}")
                    
                    # Sleep between refresh cycles
                    await asyncio.sleep(15)  # Use longer interval since WebSocket is primary
                    
                except Exception as e:
                    logger.error(f"Error in background monitor cycle: {e}")
                    await asyncio.sleep(5)
                
        except asyncio.CancelledError:
            # Clean shutdown
            pass
        except Exception as e:
            logger.error(f"Unexpected error in balance monitor: {e}")

    async def refresh_balance(self, mint: Pubkey):
        """Refresh token balance from RPC (used by seller)"""
        mint_str = str(mint)
        
        # Use token_account_manager for robust balance verification
        balance, _ = await self.token_account_manager.verify_token_balance(
            self.wallet,
            mint_str
        )
        
        # Update our cache if we got a valid balance
        if balance is not None:
            self.balances[mint_str] = balance
            self.last_update[mint_str] = time.monotonic()
            
        return balance
