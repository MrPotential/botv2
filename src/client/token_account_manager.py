from dataclasses import dataclass, field
from time import monotonic
import asyncio
import logging
import time
from typing import Optional, Tuple, Dict, Set, List, Any, Union
from colorama import Fore, Style  # Added for better logging

from solders.pubkey import Pubkey
from core.pubkeys import SystemAddresses

# Configure logger
logger = logging.getLogger("token_account_manager")

class TokenAccountManager:
    """
    Handles token account operations with WebSocket-only approach for balance verification
    """
    def __init__(self, solana_client, helius_monitor=None, commitment="confirmed"):
        self.client = solana_client
        self.commitment = commitment
        self.cache = {}  # Simple cache for token account lookups
        self.cache_ttl = 5  # Cache TTL in seconds
        self.cache_times = {}  # Track when each cache entry was last updated
        self.verification_in_progress = set()  # Track ongoing verifications
        self.max_verify_time = 15  # Maximum verification time in seconds
        self.recently_verified = {}  # Track recently verified tokens to avoid redundant checks
        self.recent_verify_ttl = 10  # Time to remember recent verifications (seconds)
        
        # Performance settings
        self.fast_verify_timeout = 2.5  # Fast verify timeout in seconds
        self.background_verify_timeout = 60  # Background verify timeout in seconds
        self.verification_poll_interval = 0.2  # How often to check WebSocket for new balances
        self.log_prefixes = {
            "success": f"{Fore.GREEN}✅{Style.RESET_ALL}",
            "warning": f"{Fore.YELLOW}⚠️{Style.RESET_ALL}",
            "error": f"{Fore.RED}❌{Style.RESET_ALL}",
            "info": f"{Fore.CYAN}ℹ️{Style.RESET_ALL}",
            "pending": f"{Fore.BLUE}⏱️{Style.RESET_ALL}"
        }
        
        # Store reference to Helius WebSocket monitor if provided
        self.helius_monitor = helius_monitor
        
    async def get_token_accounts_by_owner_and_mint(self, owner_public_key, mint_address=None):
        """
        Get token accounts owned by a specific wallet, optionally filtered by mint
        WEBSOCKET ONLY - no RPC fallback
        """
        # If no mint address specified, we can't use WebSocket efficiently
        if not mint_address or not self.helius_monitor:
            return []
        
        # Try to get balance from WebSocket
        balance = self.helius_monitor.get_token_balance(mint_address)
        ata = self.helius_monitor.get_ata(mint_address)
        
        if balance is not None:
            # We have a balance from WebSocket, return a synthetic account
            return [{
                "pubkey": ata,
                "mint": mint_address,
                "owner": str(owner_public_key),
                "amount": str(int(balance * (10 ** 6))),  # Approximate raw amount assuming 6 decimals
                "decimals": 6,  # Assuming 6 decimals, adjust if your tokens use different decimals
                "ui_amount": float(balance)
            }]
        
        # No WebSocket data available
        return []

    async def fetch_all_token_accounts(self, wallet_pubkey: Pubkey) -> List[Dict]:
        """
        Fetch all token accounts owned by this wallet.

        Args:
            wallet_pubkey: The wallet to check

        Returns:
            List of token accounts with mint address and amount information
        """
        try:
            # Try to use the Helius monitor first if available
            if self.helius_monitor and hasattr(self.helius_monitor, 'wallet_tokens'):
                token_accounts = []
                wallet_tokens = getattr(self.helius_monitor, 'wallet_tokens', {})

                for mint_str, info in wallet_tokens.items():
                    if isinstance(info, dict) and 'account' in info and 'amount' in info:
                        token_accounts.append({
                            'mint': mint_str,
                            'account': info['account'],
                            'amount': info['amount']
                        })

                if token_accounts:
                    logger.info(f"{self.log_prefixes['success']} Found {len(token_accounts)} token accounts via WebSocket")
                    return token_accounts

            # Fall back to RPC if no Helius data available
            logger.info(f"{self.log_prefixes['info']} Falling back to RPC for token accounts")

            try:
                # Get RPC client
                solana_client = await self.client.get_client()

                # Use explicit string for token program ID to avoid any encoding issues
                token_program_id = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

                # Make the API call with explicit parameters
                accounts = await solana_client.get_token_accounts_by_owner(
                    wallet_pubkey,
                    {"programId": token_program_id}
                )

                # Process the results
                token_accounts = []

                if hasattr(accounts, 'value') and accounts.value:
                    for acc in accounts.value:
                        try:
                            # Extract the account pubkey
                            account_pubkey = str(acc.pubkey)

                            # Handle the common case where account_data has a parsed attribute
                            account_data = acc.account.data

                            # Try various formats for the token data
                            mint = None
                            amount = 0

                            # Format 1: data.parsed.info structure
                            if hasattr(account_data, 'parsed'):
                                parsed = account_data.parsed
                                if hasattr(parsed, 'type') and parsed.type == 'account':
                                    if hasattr(parsed, 'info'):
                                        info = parsed.info
                                        mint = getattr(info, 'mint', None)
                                        token_amount = getattr(info, 'tokenAmount', None)
                                        if token_amount:
                                            ui_amount = getattr(token_amount, 'uiAmount', 0)
                                            amount = float(ui_amount or 0)

                            # Format 2: Dictionary structure
                            if mint is None and isinstance(account_data, dict) and 'parsed' in account_data:
                                parsed = account_data['parsed']
                                if isinstance(parsed, dict) and 'info' in parsed:
                                    info = parsed['info']
                                    mint = info.get('mint')
                                    token_amount = info.get('tokenAmount', {})
                                    if isinstance(token_amount, dict) and 'uiAmount' in token_amount:
                                        ui_amount = token_amount.get('uiAmount')
                                        amount = float(ui_amount or 0)

                            # Only add if we found a valid mint
                            if mint:
                                token_accounts.append({
                                    'mint': mint,
                                    'account': account_pubkey,
                                    'amount': amount
                                })
                        except Exception as e:
                            # Skip problematic accounts rather than failing the whole function
                            logger.debug(f"Error parsing account data: {e}")

                logger.info(f"{self.log_prefixes['success']} Found {len(token_accounts)} token accounts via RPC")
                return token_accounts

            except Exception as e:
                logger.error(f"{self.log_prefixes['error']} Error in RPC token account fetch: {str(e)}")
                return []

        except Exception as e:
            logger.error(f"{self.log_prefixes['error']} Error fetching token accounts: {str(e)}")
            return []

    async def quick_verify_purchase(self, wallet_public_key, mint_address, expected_amount):
        """
        Special optimized verification method for post-purchase confirmation.
        Returns balance immediately if found, otherwise starts background verification.
        
        Args:
            wallet_public_key: The wallet to check
            mint_address: The token's mint address
            expected_amount: The expected token amount from the purchase
            
        Returns:
            Tuple of (balance, token_account_address)
        """
        short_mint = f"{mint_address[:6]}...{mint_address[-4:]}" if len(mint_address) > 10 else mint_address
        cache_key = f"{wallet_public_key}:{mint_address}"
        
        # 1. Check if we can get an immediate balance from WebSocket
        if self.helius_monitor:
            ws_balance = self.helius_monitor.get_token_balance(mint_address)
            if ws_balance is not None and ws_balance > 0:
                # Balance already available!
                ata = self.helius_monitor.get_ata(mint_address)
                logger.info(f"{self.log_prefixes['success']} WebSocket already has token balance: {ws_balance} for {short_mint}")
                self.recently_verified[cache_key] = (time.time(), ws_balance, ata)
                return ws_balance, ata
                
        # 2. Start background verification but return optimistically
        logger.info(f"{self.log_prefixes['pending']} Starting aggressive verification for {short_mint}")
        asyncio.create_task(self._aggressive_balance_verification(
            wallet_public_key, mint_address, expected_amount, short_mint))
            
        # Return optimistic result with ATA address
        ata = self.helius_monitor.get_ata(mint_address) if self.helius_monitor else None
        logger.info(f"{self.log_prefixes['info']} Optimistically accepting token balance for {short_mint} while verification continues")
        
        # Store in cache with provisional flag
        self.recently_verified[cache_key] = (time.time(), expected_amount, ata, True)  # Fourth param is provisional flag
        return expected_amount, ata

    async def verify_token_balance(self, wallet_public_key, mint_address, expected_amount=None) -> Tuple[
        Optional[float], Optional[str]]:
        """
        Verify token balance using ONLY WebSocket data with quick timeout and background re-check

        Args:
            wallet_public_key: The wallet public key to check
            mint_address: The mint address of the token to verify
            expected_amount: Optional expected amount for validation

        Returns:
            Tuple of (balance, token_account_address) or (None, None) if verification failed
        """
        # Get a shortened version of mint address for logs
        if mint_address:
            short_mint = f"{mint_address[:6]}...{mint_address[-4:]}" if len(mint_address) > 10 else mint_address
        else:
            short_mint = "unknown"

        # Check if we recently verified this token successfully
        cache_key = f"{wallet_public_key}:{mint_address}"
        current_time = time.time()

        # If recently verified and not waiting for a specific amount, return cached result
        if cache_key in self.recently_verified and expected_amount is None:
            entry = self.recently_verified[cache_key]
            if len(entry) >= 3:  # Make sure we have at least three elements
                last_verify_time, balance, pubkey = entry[0], entry[1], entry[2]
                if current_time - last_verify_time < self.recent_verify_ttl:
                    # Check if this was a provisional verification
                    is_provisional = len(entry) >= 4 and entry[3] == True
                    if is_provisional:
                        # For provisional entries, start a verification if not already in progress
                        if cache_key not in self.verification_in_progress:
                            asyncio.create_task(self._background_balance_verification(
                                wallet_public_key, mint_address, balance, short_mint))
                    return balance, pubkey

        # Check if verification is already in progress
        if cache_key in self.verification_in_progress:
            return None, None

        self.verification_in_progress.add(cache_key)

        # ENHANCEMENT: If expecting a specific amount, use the new quick_verify_purchase method
        if expected_amount is not None:
            self.verification_in_progress.discard(cache_key)
            return await self.quick_verify_purchase(wallet_public_key, mint_address, expected_amount)

        try:
            # Only proceed if we have a WebSocket monitor
            if not self.helius_monitor:
                logger.warning(f"{self.log_prefixes['warning']} No WebSocket monitor available for balance verification")
                return None, None

            logger.info(f"{self.log_prefixes['pending']} Verifying token balance...")

            # Ask the monitor to start tracking this token if it isn't already
            if hasattr(self.helius_monitor, "add_token_monitor"):
                asyncio.create_task(self.helius_monitor.add_token_monitor(mint_address))

            # Keep a quick timeout for normal verification flow when expected_amount is None
            websocket_wait_time = self.fast_verify_timeout
            wait_start = time.time()

            # Loop to check WebSocket data with short intervals
            while time.time() - wait_start < websocket_wait_time:
                ws_balance = self.helius_monitor.get_token_balance(mint_address)
                if ws_balance is not None:
                    logger.info(f"{self.log_prefixes['success']} WebSocket found token balance: {ws_balance}")

                    # Get the token account address
                    ata = self.helius_monitor.get_ata(mint_address)

                    # Store in recently verified cache
                    self.recently_verified[cache_key] = (time.time(), ws_balance, ata)

                    return ws_balance, ata

                # Brief pause between WebSocket checks
                await asyncio.sleep(self.verification_poll_interval)

            logger.info(f"{self.log_prefixes['info']} WebSocket reported zero balance for {short_mint}")

            # If we're here, WebSocket didn't find a balance after waiting
            ata = self.helius_monitor.get_ata(mint_address)
            self.recently_verified[cache_key] = (time.time(), 0, ata)
            return 0, ata

        finally:
            # Remove from in-progress set
            self.verification_in_progress.discard(cache_key)

    async def _aggressive_balance_verification(self, wallet_public_key, mint_address, expected_amount, short_mint):
        """
        More aggressive background verification with faster polling.
        This is specifically designed for verifying recent purchases.
        
        Args:
            wallet_public_key: The wallet to check
            mint_address: The token's mint address
            expected_amount: The expected token amount
            short_mint: Short version of the mint address for logging
        """
        cache_key = f"{wallet_public_key}:{mint_address}"
        
        try:
            if not self.helius_monitor:
                return
                
            # Ask the monitor to prioritize tracking this token
            if hasattr(self.helius_monitor, "add_token_monitor"):
                asyncio.create_task(self.helius_monitor.add_token_monitor(mint_address, priority=True))
                
            # Use a medium verification time (15 seconds) with very frequent polling
            # This is specifically for recent purchases where we expect to see the balance soon
            verification_time = 15
            wait_start = time.time()
            poll_interval = 0.1  # Poll very frequently
            
            logger.info(f"{self.log_prefixes['info']} Aggressive verification started for {short_mint}")
            
            # Loop to check WebSocket data with very short intervals
            while time.time() - wait_start < verification_time:
                ws_balance = self.helius_monitor.get_token_balance(mint_address)
                
                if ws_balance is not None and ws_balance > 0:
                    # We found the balance!
                    logger.info(f"{self.log_prefixes['success']} Aggressive verification succeeded for {short_mint}: {ws_balance}")
                    
                    # Get the token account address
                    ata = self.helius_monitor.get_ata(mint_address)
                    
                    # Update the cache with the real balance - no provisional flag
                    self.recently_verified[cache_key] = (time.time(), ws_balance, ata)
                    
                    # No need to continue - we got what we wanted
                    return
                    
                await asyncio.sleep(poll_interval)
                
            # If we didn't find it in the aggressive phase, start a longer background verification
            logger.info(f"{self.log_prefixes['pending']} Switching to long-term verification for {short_mint}")
            await self._background_balance_verification(wallet_public_key, mint_address, expected_amount, short_mint)
            
        except Exception as e:
            logger.error(f"{self.log_prefixes['error']} Error in aggressive verification for {short_mint}: {e}")

    async def _background_balance_verification(self, wallet_public_key, mint_address, expected_amount, short_mint):
        """
        Background task to verify token balance with longer timeout.
        Updates the recently_verified cache if a balance is found.
        """
        cache_key = f"{wallet_public_key}:{mint_address}"
        self.verification_in_progress.add(cache_key)

        try:
            if not self.helius_monitor:
                return

            # Long verification time for background process
            websocket_wait_time = self.background_verify_timeout
            wait_start = time.time()

            logger.info(f"{self.log_prefixes['pending']} Background verification started for {short_mint}")

            # Loop to check WebSocket data with longer intervals
            while time.time() - wait_start < websocket_wait_time:
                ws_balance = self.helius_monitor.get_token_balance(mint_address)

                if ws_balance is not None and ws_balance > 0:
                    # We found the balance!
                    logger.info(f"{self.log_prefixes['success']} Background verification succeeded for {short_mint}: {ws_balance}")

                    # Get the token account address
                    ata = self.helius_monitor.get_ata(mint_address)

                    # Update the cache with the real balance - no provisional flag
                    self.recently_verified[cache_key] = (time.time(), ws_balance, ata)

                    # No need to continue waiting
                    return

                # Longer pause between WebSocket checks since this is background
                await asyncio.sleep(2)

            # If we've reached this point, we timed out waiting for the balance
            logger.warning(f"{self.log_prefixes['warning']} Background verification timed out for {short_mint} after {websocket_wait_time}s")

        except Exception as e:
            logger.error(f"{self.log_prefixes['error']} Error in background verification for {short_mint}: {e}")
        finally:
            self.verification_in_progress.discard(cache_key)
            
    async def get_all_token_accounts(self, wallet_public_key):
        """
        Get all token accounts for a wallet with balances - stub for WebSocket approach
        WebSocket is primarily designed for tracking specific tokens, not listing all tokens
        
        Args:
            wallet_public_key: The wallet public key to check
            
        Returns:
            List of token account records with balance information
        """
        # This function can't be efficiently implemented with WebSocket only
        # As WebSocket doesn't provide a "list all tokens" functionality
        # Use fetch_all_token_accounts instead
        return await self.fetch_all_token_accounts(wallet_public_key)

    async def check_token_exists(self, wallet_public_key, mint_address, timeout=10):
        """
        Quick check if a token account exists - WebSocket ONLY
        
        Args:
            wallet_public_key: The wallet public key to check
            mint_address: The mint address of the token to verify
            timeout: Maximum time to wait in seconds
            
        Returns:
            Tuple of (exists, balance, account_pubkey)
        """
        # First check WebSocket data
        if not self.helius_monitor:
            return False, 0, None
            
        # Check if we recently verified this token successfully
        cache_key = f"{wallet_public_key}:{mint_address}"
        current_time = time.time()
        
        if cache_key in self.recently_verified:
            entry = self.recently_verified[cache_key]
            if len(entry) >= 3:
                last_verify_time, balance, pubkey = entry[0], entry[1], entry[2]
                if current_time - last_verify_time < self.recent_verify_ttl:
                    return balance > 0, balance, pubkey
                
        # If verification is in progress, don't start another one
        if cache_key in self.verification_in_progress:
            return False, 0, None
            
        self.verification_in_progress.add(cache_key)
        
        try:
            # Ask the monitor to start tracking this token if it isn't already
            if hasattr(self.helius_monitor, "add_token_monitor"):
                asyncio.create_task(self.helius_monitor.add_token_monitor(mint_address))
                
            # Wait up to the specified timeout for WebSocket data
            start_time = time.time()
            short_mint = f"{mint_address[:6]}...{mint_address[-4:]}" if len(mint_address) > 10 else mint_address
            
            # Loop to check WebSocket data with short intervals
            while time.time() - start_time < timeout:
                ws_balance = self.helius_monitor.get_token_balance(mint_address)
                if ws_balance is not None and ws_balance > 0:
                    ata = self.helius_monitor.get_ata(mint_address)
                    self.recently_verified[cache_key] = (time.time(), ws_balance, ata)
                    logger.debug(f"Token {short_mint} exists with balance {ws_balance}")
                    return True, ws_balance, ata
                    
                # Brief pause between WebSocket checks
                await asyncio.sleep(0.5)
                
            # Token doesn't exist or has zero balance
            ata = self.helius_monitor.get_ata(mint_address)
            self.recently_verified[cache_key] = (current_time, 0, ata)
            logger.debug(f"Token {short_mint} not found or has zero balance")
            return False, 0, ata
            
        finally:
            self.verification_in_progress.discard(cache_key)

    async def verify_sol_balance(self, wallet_public_key, timeout=10) -> float:
        """
        Get accurate SOL balance with WebSocket priority and RPC fallback

        Args:
            wallet_public_key: The wallet to check
            timeout: Maximum time to wait for WebSocket update

        Returns:
            Current SOL balance as float
        """
        start_time = time.time()
        sol_balance = None

        # Try WebSocket first if available
        if self.helius_monitor:
            # Get initial WebSocket balance
            sol_balance = self.helius_monitor.get_sol_balance()
            logger.info(f"{self.log_prefixes['info']} Initial WebSocket SOL balance: {sol_balance:.6f}")

            # If balance is zero or very low, we should double-check with RPC
            if sol_balance is not None and sol_balance > 0.001:
                logger.info(f"{self.log_prefixes['success']} Using WebSocket SOL balance: {sol_balance:.6f}")
                return sol_balance

            # Wait and poll for WebSocket updates
            logger.info(f"{self.log_prefixes['pending']} Waiting for WebSocket SOL balance...")

            # Loop with frequent polling for up to timeout seconds
            while time.time() - start_time < timeout:
                sol_balance = self.helius_monitor.get_sol_balance()
                if sol_balance is not None and sol_balance > 0.001:
                    logger.info(f"{self.log_prefixes['success']} Got updated WebSocket SOL balance: {sol_balance:.6f}")
                    return sol_balance

                # Small wait between checks
                await asyncio.sleep(0.2)

        # Fall back to RPC
        logger.info(f"{self.log_prefixes['info']} Falling back to RPC for SOL balance")

        try:
            # Get RPC client
            solana_client = await self.client.get_client()

            # Make the API call for SOL balance
            account_info = await solana_client.get_account_info(Pubkey.from_string(wallet_public_key))

            if account_info.value:
                lamports = account_info.value.lamports
                sol_balance = lamports / 1_000_000_000
                logger.info(f"{self.log_prefixes['success']} RPC SOL balance: {sol_balance:.6f}")

                # Update WebSocket monitor if available
                # Add to the RPC section of verify_sol_balance
                if self.helius_monitor and hasattr(self.helius_monitor, 'update_sol_balance'):
                    self.helius_monitor.update_sol_balance(sol_balance)

                return sol_balance

        except Exception as e:
            logger.error(f"{self.log_prefixes['error']} Error fetching SOL balance: {e}")

        # Return WebSocket balance as last resort (even if it might be outdated)
        if sol_balance is not None:
            logger.warning(f"{self.log_prefixes['warning']} Using potentially outdated SOL balance: {sol_balance:.6f}")
            return sol_balance

        # If all else fails
        logger.error(f"{self.log_prefixes['error']} Could not determine SOL balance")
        return 0.0


    def clear_verification_cache(self):
        """Clear the verification cache"""
        current_time = time.time()
        
        # Remove expired entries
        expired_keys = []
        for key, entry in self.recently_verified.items():
            if len(entry) >= 1:
                timestamp = entry[0]
                if current_time - timestamp > self.recent_verify_ttl:
                    expired_keys.append(key)
                
        for key in expired_keys:
            del self.recently_verified[key]
            
        if expired_keys:
            logger.debug(f"Cleared {len(expired_keys)} expired entries from verification cache")
            
    def set_helius_monitor(self, helius_monitor):
        """Set or update the Helius WebSocket monitor reference"""
        self.helius_monitor = helius_monitor
        logger.info(f"{self.log_prefixes['info']} Helius WebSocket monitor updated")
