import asyncio
import struct
import base64
from typing import Final, List, Optional

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

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
from trading.base import TokenInfo, TradeResult
from utils.logger import get_logger
import colorama
from colorama import Fore, Style

logger = get_logger(__name__)

# Discriminator for the sell instruction - from the IDL
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 12502976635542562355)


def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID
    seeds = [
        bytes(owner),
        bytes(TOKEN_PROGRAM_ID),
        bytes(mint),
    ]
    return Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)[0]


class TokenSeller:
    """Handles selling tokens on pump.fun with support for partial sells and diagnostics."""

    def __init__(
            self,
            client: SolanaClient,
            wallet: Wallet,
            curve_manager: BondingCurveManager,
            priority_fee_manager: PriorityFeeManager,
            slippage: float = 0.25,
            max_retries: int = 5,
            balance_monitor=None,  # Add balance monitor parameter
    ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.priority_fee_manager = priority_fee_manager
        self.slippage = slippage
        self.max_retries = max_retries
        self.grace_period = 15  # seconds
        self.balance_monitor = balance_monitor  # Store balance monitor reference

    async def execute(self, token_info: TokenInfo, amount_pct=1.0, *args, **kwargs) -> TradeResult:
        """Execute sell operation.

        Args:
            token_info: Token information
            amount_pct: Percentage of tokens to sell (0.0-1.0, default 1.0 = 100%)

        Returns:
            TradeResult with sell outcome
        """
        try:
            # Get associated token account
            associated_token_account = self.wallet.get_associated_token_address(
                token_info.mint
            )

            # Get token balance
            token_balance = await self.client.get_token_account_balance(
                associated_token_account
            )
            token_balance_decimal = token_balance / 10 ** TOKEN_DECIMALS

            logger.info(f"Token balance: {token_balance_decimal}")

            if token_balance == 0:
                logger.info("No tokens to sell.")
                return TradeResult(success=False, error_message="No tokens to sell")

            # Calculate amount to sell based on percentage
            if amount_pct < 1.0:
                # If selling partial amount
                amount = int(token_balance * amount_pct)
                amount_decimal = amount / 10 ** TOKEN_DECIMALS
                logger.info(
                    f"📊 Selling {amount_pct * 100:.1f}% of tokens: {amount_decimal:.6f} of {token_balance_decimal:.6f}")
            else:
                # If selling all
                amount = token_balance
                amount_decimal = token_balance_decimal
                logger.info(f"Selling 100% of tokens: {token_balance_decimal:.6f}")

            # Fetch token price
            curve_state = await self.curve_manager.get_curve_state(
                token_info.bonding_curve
            )
            token_price_sol = curve_state.calculate_price()

            logger.info(f"Price per Token: {token_price_sol:.8f} SOL")

            # Calculate minimum SOL output with slippage
            expected_sol_output = float(amount_decimal) * float(token_price_sol)
            slippage_factor = 1 - self.slippage
            min_sol_output = int(
                (expected_sol_output * slippage_factor) * LAMPORTS_PER_SOL
            )

            logger.info(f"Selling {amount_decimal} tokens")
            logger.info(f"Expected SOL output: {expected_sol_output:.8f} SOL")
            logger.info(
                f"Minimum SOL output (with {self.slippage * 100}% slippage): {min_sol_output / LAMPORTS_PER_SOL:.8f} SOL"
            )

            tx_signature = await self._send_sell_transaction(
                token_info,
                associated_token_account,
                amount,
                min_sol_output,
            )

            success = await self.client.confirm_transaction(tx_signature)

            if success:
                logger.info(f"Sell transaction confirmed: {tx_signature}")
                return TradeResult(
                    success=True,
                    tx_signature=tx_signature,
                    amount=amount_decimal,  # Return the actual amount sold
                    price=token_price_sol,
                )
            else:
                return TradeResult(
                    success=False,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                )

        except Exception as e:
            logger.error(f"Sell operation failed: {e!s}")
            return TradeResult(success=False, error_message=str(e))

    async def _send_sell_transaction(
            self,
            token_info: TokenInfo,
            associated_token_account: Pubkey,
            token_amount: int,
            min_sol_output: int,
    ) -> str:
        """Send sell transaction.

        Args:
            mint: Token information
            associated_token_account: User's token account
            token_amount: Amount of tokens to sell in raw units
            min_sol_output: Minimum SOL to receive in lamports

        Returns:
            Transaction signature

        Raises:
            Exception: If transaction fails after all retries
        """
        # Prepare sell instruction accounts
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
                pubkey=token_info.creator_vault, is_signer=False, is_writable=True,
            ),
            AccountMeta(
                pubkey=SystemAddresses.TOKEN_PROGRAM, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=PumpAddresses.PROGRAM, is_signer=False, is_writable=False
            ),
        ]

        # Prepare sell instruction data
        data = (
                EXPECTED_DISCRIMINATOR
                + struct.pack("<Q", token_amount)
                + struct.pack("<Q", min_sol_output)
        )
        sell_ix = Instruction(PumpAddresses.PROGRAM, data, accounts)

        try:
            return await self.client.build_and_send_transaction(
                [sell_ix],
                self.wallet.keypair,
                skip_preflight=True,
                max_retries=self.max_retries,
                priority_fee=await self.priority_fee_manager.calculate_priority_fee(
                    self._get_relevant_accounts(token_info)
                ),
            )
        except Exception as e:
            logger.error(f"Sell transaction failed: {e!s}")
            raise

    def _get_relevant_accounts(self, token_info: TokenInfo) -> dict:
            """Get all relevant accounts needed for selling a token.

            Args:
                token_info: Token information

            Returns:
                Dictionary with all account pubkeys needed for the sell transaction
            """
            token_mint = token_info.mint
            bonding_curve = token_info.bonding_curve
            associated_bonding_curve = token_info.associated_bonding_curve
            creator_vault = token_info.creator_vault

            # Get or create associated token account for the user's wallet
            user_token_account = self.wallet.get_associated_token_address(token_mint)

            # Get SOL ATA for token mint
            sol_ata = get_associated_token_address(
                bonding_curve,
                SystemAddresses.SOL  # or SystemAddresses.NATIVE_MINT, if you added that alias
            )
            return {
                "token_mint": token_mint,
                "bonding_curve": bonding_curve,
                "associated_bonding_curve": associated_bonding_curve,
                "creator_vault": creator_vault,
                "user_token_account": user_token_account,
                "user_wallet": self.wallet.pubkey,
                "sol_ata": sol_ata,
                "token_program": SystemAddresses.TOKEN_PROGRAM,
                "system_program": SystemAddresses.PROGRAM,
                "associated_token_program": SystemAddresses.ASSOCIATED_TOKEN_PROGRAM
            }