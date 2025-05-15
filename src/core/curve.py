"""
Bonding curve operations for pump.fun tokens.
"""

import struct
import time
from typing import Final, Dict, Optional

from construct import Bytes, Flag, Int64ul, Struct
from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.pubkeys import LAMPORTS_PER_SOL, TOKEN_DECIMALS
from utils.logger import get_logger

logger = get_logger(__name__)

# Discriminator for the bonding curve account
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 6966180631402821399)


class BondingCurveState:
    """Represents the state of a pump.fun bonding curve."""

    _STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),  # Added new creator field - 32 bytes for Pubkey
    )

    def __init__(self, data: bytes) -> None:
        """Parse bonding curve data.

        Args:
            data: Raw account data

        Raises:
            ValueError: If data cannot be parsed
        """
        if data[:8] != EXPECTED_DISCRIMINATOR:
            raise ValueError("Invalid curve state discriminator")

        parsed = self._STRUCT.parse(data[8:])
        self.__dict__.update(parsed)

        # Convert raw bytes to Pubkey for creator field
        if hasattr(self, 'creator') and isinstance(self.creator, bytes):
            self.creator = Pubkey.from_bytes(self.creator)

    def calculate_price(self) -> float:
        """Calculate token price in SOL.

        Returns:
            Token price in SOL

        Raises:
            ValueError: If reserve state is invalid
        """
        if self.virtual_token_reserves <= 0 or self.virtual_sol_reserves <= 0:
            raise ValueError("Invalid reserve state")

        return (self.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
            self.virtual_token_reserves / 10**TOKEN_DECIMALS
        )

    @property
    def token_reserves(self) -> float:
        """Get token reserves in decimal form."""
        return self.virtual_token_reserves / 10**TOKEN_DECIMALS

    @property
    def sol_reserves(self) -> float:
        """Get SOL reserves in decimal form."""
        return self.virtual_sol_reserves / LAMPORTS_PER_SOL


class BondingCurveManager:
    """Manager for bonding curve operations."""

    def __init__(self, client: SolanaClient):
        """Initialize with Solana client.

        Args:
            client: Solana client for RPC calls
        """
        self.client = client

        # Add caching to reduce RPC calls
        self._curve_cache: Dict[str, BondingCurveState] = {}  # Cache for curve states
        self._curve_cache_times: Dict[str, float] = {}  # Last update timestamps
        self._account_cache: Dict[str, Optional[object]] = {}  # Cache for account info
        self._account_cache_times: Dict[str, float] = {}  # Last update timestamps
        self._cache_ttl = 3.0  # Cache TTL in seconds

    async def get_curve_state(self, curve_address: Pubkey, force_refresh: bool = False) -> BondingCurveState:
        """Get the state of a bonding curve.

        Args:
            curve_address: Address of the bonding curve account
            force_refresh: Whether to force a refresh from the network

        Returns:
            Bonding curve state

        Raises:
            ValueError: If curve data is invalid
        """
        key_str = str(curve_address)
        current_time = time.monotonic()

        # Use cached value if not expired and not forced refresh
        if not force_refresh and key_str in self._curve_cache:
            last_update = self._curve_cache_times.get(key_str, 0)
            if current_time - last_update < self._cache_ttl:
                return self._curve_cache[key_str]

        try:
            account = await self.client.get_account_info(curve_address)
            if not account.data:
                raise ValueError(f"No data in bonding curve account {curve_address}")

            curve_state = BondingCurveState(account.data)

            # Cache the result
            self._curve_cache[key_str] = curve_state
            self._curve_cache_times[key_str] = current_time

            return curve_state

        except Exception as e:
            logger.error(f"Failed to get curve state: {str(e)}")
            raise ValueError(f"Invalid curve state: {str(e)}")

    async def get_multiple_curve_states(self, curve_addresses: list[Pubkey], force_refresh: bool = False) -> Dict[
        str, BondingCurveState]:
        """Get the state of multiple bonding curves in a single RPC call."""
        if not curve_addresses:
            return {}

        current_time = time.monotonic()
        results = {}
        addresses_to_fetch = []

        # First check cache for any curves we don't need to fetch
        for curve_address in curve_addresses:
            key_str = str(curve_address)
            if not force_refresh and key_str in self._curve_cache:
                last_update = self._curve_cache_times.get(key_str, 0)
                if current_time - last_update < self._cache_ttl:
                    results[key_str] = self._curve_cache[key_str]
                    continue
            addresses_to_fetch.append(curve_address)

        # If all were in cache, return early
        if not addresses_to_fetch:
            return results

        try:
            # Fetch all accounts in a single RPC call
            client = await self.client.get_client()
            accounts_response = await client.get_multiple_accounts(addresses_to_fetch, encoding="base64")

            # Process each account
            for i, account in enumerate(accounts_response.value):
                curve_address = addresses_to_fetch[i]
                key_str = str(curve_address)

                if account is None or not account.data:
                    logger.debug(f"No data in bonding curve account {curve_address}")
                    continue

                try:
                    # IMPORTANT: Decode base64 data from get_multiple_accounts
                    import base64
                    if isinstance(account.data, list) and len(account.data) >= 2 and account.data[0] == "base64":
                        # Data is in [encoding, data] format
                        data = base64.b64decode(account.data[1])
                    elif isinstance(account.data, str):
                        # Data is a base64 string
                        data = base64.b64decode(account.data)
                    else:
                        # Data is already in binary format (unlikely)
                        data = account.data

                    curve_state = BondingCurveState(data)

                    # Cache the result
                    self._curve_cache[key_str] = curve_state
                    self._curve_cache_times[key_str] = current_time

                    # Add to results
                    results[key_str] = curve_state

                except Exception as e:
                    logger.error(f"Failed to parse curve state for {curve_address}: {str(e)}")

        except Exception as e:
            logger.error(f"Failed to fetch multiple curve states: {str(e)}")

        return results

    async def check_multiple_curves_initialized(self, curve_addresses: list[Pubkey], program_id: Pubkey) -> Dict[
        str, bool]:
        """Check if multiple bonding curves are initialized in a single RPC call."""
        if not curve_addresses:
            return {}

        current_time = time.monotonic()
        results = {}
        addresses_to_fetch = []

        # First check cache
        for curve_address in curve_addresses:
            key_str = str(curve_address)
            if key_str in self._account_cache:
                last_update = self._account_cache_times.get(key_str, 0)
                if current_time - last_update < self._cache_ttl:
                    account_info = self._account_cache[key_str]
                    results[key_str] = account_info is not None and account_info.owner == program_id
                    continue
            addresses_to_fetch.append(curve_address)

        # If all were in cache, return early
        if not addresses_to_fetch:
            return results

        try:
            # Fetch all accounts in a single RPC call
            client = await self.client.get_client()
            accounts_response = await client.get_multiple_accounts(addresses_to_fetch)

            # Process each account
            for i, account in enumerate(accounts_response.value):
                curve_address = addresses_to_fetch[i]
                key_str = str(curve_address)

                # Cache the result - make sure we're storing objects with the right format
                self._account_cache[key_str] = account
                self._account_cache_times[key_str] = current_time

                # Set the result - ensure we're checking the owner correctly
                if account is not None and hasattr(account, 'owner'):
                    results[key_str] = account.owner == program_id
                else:
                    results[key_str] = False

        except Exception as e:
            logger.error(f"Failed to check multiple curves initialized: {str(e)}")

        return results



    async def is_curve_initialized(self, curve_address: Pubkey, program_id: Pubkey) -> bool:
        """Check if bonding curve is initialized with caching.

        Args:
            curve_address: Address of the bonding curve account
            program_id: Expected program ID owning the account

        Returns:
            True if initialized, False otherwise
        """
        key_str = str(curve_address)
        current_time = time.monotonic()

        # Use cached account info if available and recent
        if key_str in self._account_cache:
            last_update = self._account_cache_times.get(key_str, 0)
            if current_time - last_update < self._cache_ttl:
                account_info = self._account_cache[key_str]
                if account_info is None:
                    return False
                return account_info.owner == program_id

        # Fetch account info
        account_info = await self.client.get_account_info(curve_address)

        # Cache the result
        self._account_cache[key_str] = account_info
        self._account_cache_times[key_str] = current_time

        if account_info is None:
            return False
        return account_info.owner == program_id

    async def calculate_price(self, curve_address: Pubkey, force_refresh: bool = False) -> float:
        """Calculate the current price of a token.

        Args:
            curve_address: Address of the bonding curve account
            force_refresh: Whether to force a refresh from the network

        Returns:
            Token price in SOL
        """
        curve_state = await self.get_curve_state(curve_address, force_refresh)
        return curve_state.calculate_price()

    async def calculate_expected_tokens(
        self, curve_address: Pubkey, sol_amount: float, force_refresh: bool = False
    ) -> float:
        """Calculate the expected token amount for a given SOL input.

        Args:
            curve_address: Address of the bonding curve account
            sol_amount: Amount of SOL to spend
            force_refresh: Whether to force a refresh from the network

        Returns:
            Expected token amount
        """
        curve_state = await self.get_curve_state(curve_address, force_refresh)
        price = curve_state.calculate_price()
        return sol_amount / price