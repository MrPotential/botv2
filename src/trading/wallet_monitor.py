# src/trading/wallet_monitor.py
from typing import Dict, Optional, Set, Tuple
from solders.pubkey import Pubkey
from core.client import SolanaClient
from utils.logger import get_logger
import asyncio

# Import our new WebSocket-based monitor
from trading.helius_token_monitor import HeliusWebSocketMonitor, WalletBalanceMonitor as HeliusWalletMonitor

logger = get_logger(__name__)

# This class maintains the same interface as the original WalletBalanceMonitor
# but uses the new Helius WebSocket implementation under the hood
class WalletBalanceMonitor(HeliusWalletMonitor):
    """Monitors and caches token balances using Helius WebSockets."""
    
    def __init__(self, client: SolanaClient, wallet_pubkey: Pubkey, helius_api_key: str = None):
        super().__init__(client, wallet_pubkey, helius_api_key)
