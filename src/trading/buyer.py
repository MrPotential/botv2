import struct
import asyncio
import base64
from typing import Final, Optional

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

from core.client import SolanaClient
from core.curve import BondingCurveManager
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    LAMPORTS_PER_SOL,
    TOKEN_DECIMALS,
    PumpAddresses,
    SystemAddresses,
)
from core.wallet import Wallet
from trading.base import TokenInfo, Trader, TradeResult
from utils.logger import get_logger
import colorama
from colorama import Fore, Style

logger = get_logger(__name__)

# Discriminators from the IDL
BUY_WITH_EXACT_SOL_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 15602980922370526681)  # hex: 0x6c617125 0ebb3bd9
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 16927863322537952870)
EXTEND_ACCOUNT_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 16521388798243881202)


class TokenBuyer(Trader):
    """Handles buying tokens on pump.fun."""

    def __init__(
            self,
            client: SolanaClient,
            wallet: Wallet,
            curve_manager: BondingCurveManager,
            priority_fee_manager: PriorityFeeManager,
            amount: float,
            slippage: float = 0.01,
            max_retries: int = 5,
            extreme_fast_token_amount: int = 0,
            extreme_fast_mode: bool = False,
            balance_monitor=None,
    ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.priority_fee_manager = priority_fee_manager
        self.amount = amount
        self.slippage = slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount
        self.balance_monitor = balance_monitor

    async def execute(self, token_info: TokenInfo, *args, **kwargs) -> TradeResult:
        """Execute buy operation.

        Args:
            token_info: Token information

        Returns:
            TradeResult with buy outcome
        """
        try:
            # Convert amount to lamports - this is how much SOL we want to spend
            sol_amount_lamports = int(self.amount * LAMPORTS_PER_SOL)

            # Get token price for estimation/logging only
            if self.extreme_fast_mode:
                # Skip the wait and use a simple estimate
                estimated_token_amount = self.extreme_fast_token_amount
                estimated_token_price = self.amount / estimated_token_amount
            else:
                # Get the curve state - this is already cached if recently fetched
                # so it's reasonably efficient as a single operation
                curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve)
                estimated_token_price = curve_state.calculate_price()

                # Calculate how many tokens we might get with our SOL amount
                estimated_token_amount = self.amount / estimated_token_price

            # Request only 10% of the estimated token amount to ensure we stay under the SOL limit
            # This is a conservative approach to make sure the transaction succeeds
            conservative_token_amount = estimated_token_amount * 0.1
            token_amount_raw = int(conservative_token_amount * 10 ** TOKEN_DECIMALS)

            associated_token_account = self.wallet.get_associated_token_address(
                token_info.mint
            )

            logger.info(
                f"{Fore.GREEN}Buying with {self.amount:.6f} SOL - Requesting {conservative_token_amount:.6f} tokens (10% of est. {estimated_token_amount:.6f}) at ~{estimated_token_price:.8f} SOL/token{Style.RESET_ALL}"
            )

            # Get priority fee if applicable
            priority_fee = await self.priority_fee_manager.calculate_priority_fee(
                self._get_relevant_accounts(token_info)
            )
            if priority_fee > 0:
                logger.info(f"Priority fee in microlamports: {priority_fee}")

            # Use the regular buy instruction but with specific parameters:
            # - token_amount is a very conservative 10% of what we estimate
            # - max_sol_cost is our desired SOL spend
            tx_signature = await self._send_buy_transaction(
                token_info,
                associated_token_account,
                token_amount_raw,  # Conservative token amount (10% of estimate)
                sol_amount_lamports,  # Our exact SOL amount
            )

            success = await self.client.confirm_transaction(tx_signature)

            if success:
                logger.info(f"{Fore.GREEN}✅ Buy transaction confirmed: {tx_signature}{Style.RESET_ALL}")
                return TradeResult(
                    success=True,
                    tx_signature=tx_signature,
                    amount=conservative_token_amount,  # Report the conservative amount we requested
                    price=estimated_token_price,
                    token_account=str(associated_token_account)
                )
            else:
                logger.error(f"{Fore.RED}❌ Transaction failed to confirm: {tx_signature}{Style.RESET_ALL}")
                return TradeResult(
                    success=False,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                )

        except Exception as e:
            logger.error(f"{Fore.RED}❌ Buy operation failed: {e!s}{Style.RESET_ALL}")
            return TradeResult(success=False, error_message=str(e))

    async def _send_buy_transaction(
            self,
            token_info: TokenInfo,
            associated_token_account: Pubkey,
            token_amount: int,  # This is already the raw token amount (scaled by 10^9)
            max_amount_lamports: int,
    ) -> str:
        """Send buy transaction.

        Args:
            token_info: Token information
            associated_token_account: User's token account
            token_amount: Amount of tokens to buy (already in raw form)
            max_amount_lamports: Maximum SOL to spend in lamports

        Returns:
            Transaction signature

        Raises:
            Exception: If transaction fails after all retries
        """
        accounts = [
            AccountMeta(
                pubkey=PumpAddresses.GLOBAL, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=PumpAddresses.FEE, is_signer=False, is_writable=True),
            AccountMeta(pubkey=token_info.mint, is_signer=False, is_writable=False),
            AccountMeta(
                pubkey=token_info.bonding_curve, is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=token_info.associated_bonding_curve,
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=associated_token_account, is_signer=False, is_writable=True
            ),
            AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
            AccountMeta(
                pubkey=SystemAddresses.PROGRAM, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=SystemAddresses.TOKEN_PROGRAM, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=token_info.creator_vault, is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=PumpAddresses.PROGRAM, is_signer=False, is_writable=False
            ),
        ]

        # Prepare idempotent create ATA instruction: it will not fail if ATA already exists
        idempotent_ata_ix = create_idempotent_associated_token_account(
            self.wallet.pubkey,
            self.wallet.pubkey,
            token_info.mint,
            SystemAddresses.TOKEN_PROGRAM
        )

        # Prepare buy instruction data
        data = (
                EXPECTED_DISCRIMINATOR
                + struct.pack("<Q", token_amount)  # token_amount is already raw (scaled by 10^9)
                + struct.pack("<Q", max_amount_lamports)
        )
        buy_ix = Instruction(PumpAddresses.PROGRAM, data, accounts)

        try:
            return await self.client.build_and_send_transaction(
                [idempotent_ata_ix, buy_ix],
                self.wallet.keypair,
                skip_preflight=True,
                max_retries=self.max_retries,
                priority_fee=await self.priority_fee_manager.calculate_priority_fee(
                    self._get_relevant_accounts(token_info)
                ),
            )
        except Exception as e:
            logger.error(f"Buy transaction failed: {e!s}")
            raise

    def _get_relevant_accounts(self, token_info: TokenInfo) -> dict:
        """Get all relevant accounts needed for buying a token.

        Args:
            token_info: Token information

        Returns:
            Dictionary with all account pubkeys needed for the buy transaction
        """
        token_mint = token_info.mint
        bonding_curve = token_info.bonding_curve
        associated_bonding_curve = token_info.associated_bonding_curve
        creator_vault = token_info.creator_vault

        # Get or create associated token account for the user's wallet
        user_token_account = self.wallet.get_associated_token_address(token_mint)

        return {
            "token_mint": token_mint,
            "bonding_curve": bonding_curve,
            "associated_bonding_curve": associated_bonding_curve,
            "creator_vault": creator_vault,
            "user_token_account": user_token_account,
            "user_wallet": self.wallet.pubkey,
            "token_program": SystemAddresses.TOKEN_PROGRAM,
            "system_program": SystemAddresses.PROGRAM,
            "associated_token_program": SystemAddresses.ASSOCIATED_TOKEN_PROGRAM
        }

    def calculate_buffer_factor(self, token_info):
        """
        Calculate a dynamic buffer factor based on trading activity.

        Args:
            token_info: Information about the token

        Returns:
            float: Buffer factor between 0.1 (10%) and 0.7 (70%)
        """
        # If extreme fast mode, use minimum buffer
        if self.extreme_fast_mode:
            return 0.1  # Always use 10% in extreme fast mode

        # If token info has trading velocity data
        if hasattr(token_info, 'trades_per_minute') and token_info.trades_per_minute > 0:
            # More active tokens (higher trades_per_minute) get smaller buffer
            # Scale from 0.1 (10%) for very active to 0.7 (70%) for less active
            buffer_factor = max(0.1, min(0.7, 1.0 - (token_info.trades_per_minute / 30.0)))
        elif hasattr(token_info, 'recent_trades') and hasattr(token_info, 'time_window'):
            # Calculate trades per minute from recent_trades if available
            trades_per_minute = len(token_info.recent_trades) / (token_info.time_window / 60)
            buffer_factor = max(0.1, min(0.7, 1.0 - (trades_per_minute / 30.0)))
        else:
            # Default to moderately conservative buffer (30%)
            buffer_factor = 0.3

        return buffer_factor