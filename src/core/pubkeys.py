from dataclasses import dataclass
from typing import Final

from solders.pubkey import Pubkey

LAMPORTS_PER_SOL: Final[int] = 1_000_000_000
TOKEN_DECIMALS:   Final[int] = 6

# Latest protocol fee account for Pump.fun AMM (required in buy/sell)
PROTOCOL_FEE_ACCOUNT = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"

@dataclass
class SystemAddresses:
    PROGRAM: Final[Pubkey] = Pubkey.from_string("11111111111111111111111111111111")
    TOKEN_PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    )
    ASSOCIATED_TOKEN_PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
    )
    RENT: Final[Pubkey] = Pubkey.from_string(
        "SysvarRent111111111111111111111111111111111"
    )
    SOL: Final[Pubkey] = Pubkey.from_string(
        "So11111111111111111111111111111111111111112"
    )

@dataclass
class PumpAddresses:
    PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    )
    # Use the PROTOCOL_FEE_ACCOUNT as the main fee recipient for Pump.fun AMM
    GLOBAL: Final[Pubkey] = Pubkey.from_string(PROTOCOL_FEE_ACCOUNT)
    EVENT_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
        "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
    )
    # Keep FEE for legacy or for when AMM not being used
    FEE: Final[Pubkey] = Pubkey.from_string(
        "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"
    )
    LIQUIDITY_MIGRATOR: Final[Pubkey] = Pubkey.from_string(
        "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
    )

    @staticmethod
    def get_bonding_curve_address(mint_address: Pubkey) -> Pubkey:
        seeds = [b"bonding-curve", bytes(mint_address)]
        return Pubkey.find_program_address(seeds, PumpAddresses.PROGRAM)[0]

    @staticmethod
    def get_associated_bonding_curve_address(
        bonding_curve: Pubkey, mint_address: Pubkey
    ) -> Pubkey:
        seeds = [
            bytes(bonding_curve),
            bytes(SystemAddresses.TOKEN_PROGRAM),
            bytes(mint_address),
        ]
        return Pubkey.find_program_address(
            seeds, SystemAddresses.ASSOCIATED_TOKEN_PROGRAM
        )[0]

    @staticmethod
    def get_creator_vault_authority(creator_pubkey: Pubkey) -> Pubkey:
        # Use underscore per on-chain program (confirmed in latest IDL)
        seeds = [b"creator_vault", bytes(creator_pubkey)]
        return Pubkey.find_program_address(seeds, PumpAddresses.PROGRAM)[0]