from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Dict, List, Optional
import asyncio
import logging  # Added missing import
from solders.pubkey import Pubkey

from trading.base import TokenInfo, TradeResult
from utils.logger import get_logger
from colorama import Fore, Style

logger = get_logger(__name__)


@dataclass
class Position:
    """Represents a trading position."""
    token_info: Any
    symbol: str
    token_key: str
    entry_price: float
    amount: float
    tx_signature: str = None
    take_profit_tiers: List[float] = field(default_factory=lambda: [1.3, 1.6, 2.0])
    sell_portions: List[float] = field(default_factory=lambda: [0.5, 0.5, 1.0])
    stop_loss_pct: float = 0.10
    trailing_stop_pct: float = 0.08

    # These will be initialized in __post_init__
    stop_price: float = 0.0
    trailing_stop_price: float = 0.0

    def __post_init__(self):
        """Initialize additional attributes after creation."""
        # Set up initial values for position tracking
        self.current_price = self.entry_price
        self.peak_price = self.entry_price
        self.stop_loss = self.entry_price * (1 - self.stop_loss_pct)
        self.stop_price = self.stop_loss  # Alias for compatibility

        # Initialize trailing stop variables
        self.max_price = self.entry_price
        self.highest_price = self.entry_price
        self.trailing_stop_price = self.entry_price * (1 - self.trailing_stop_pct)
        self.trailing_activated = False

        # Initialize take profit tracking
        self.take_profit_hit = [False] * len(self.take_profit_tiers)
        self.current_tier = 0
        self.last_take_profit_time = 0
        self.take_profit_cooldown = 10  # Reduced from 30 to 10 seconds

        # Track realized profit
        self.realized_profit = 0.0
        self.amount_remaining = self.amount  # FIXED: Initialize with amount instead of original_amount
        self.original_amount = self.amount  # Add original_amount tracking
        self.original_cost = self.amount * self.entry_price  # Add original cost tracking
        self.buy_time = monotonic()  # Add buy time tracking
        self.price_check_count = 0  # Add price check counter
        self.last_valid_price = self.entry_price  # Initialize last valid price
        self.last_checked_price = self.entry_price  # Initialize last checked price
        self.last_price_check_time = monotonic()  # Initialize last price check time

        # WebSocket monitoring status
        self.using_websocket = False  # Initialize using_websocket
        self.last_websocket_update = monotonic()
        self._is_websocket_monitored = False  # Backing field for property

        # Position flag for marked positions
        self._marked_for_exit = False
        self.is_trailing_stop_active = False  # Add trailing stop active flag
        self._breakeven_message_shown = False  # Add breakeven message shown flag

        # GREEDY MODE: Track initial recovery flag
        self.initial_recovered = False

        # NEW: Token account tracking
        self.token_account = None  # Will store the Associated Token Account address
        self.balance_source = None  # Will track which source provided our balance ("websocket" or "rpc")
        self.failed_sell_attempts = 0
        self._active_sell = False

    def mark_as_websocket_monitored(self):
        """Mark this position as being monitored via WebSocket."""
        self._is_websocket_monitored = True  # Set the backing field
        self.using_websocket = True
        self.last_websocket_update = monotonic()

    def mark_take_profit_hit(self, tier_index):
        """Mark a take-profit tier as hit and set cooldown."""
        if tier_index < len(self.take_profit_hit):
            self.take_profit_hit[tier_index] = True
            self.last_take_profit_time = monotonic()

            # GREEDY MODE: Always update trailing stop after any take profit hit
            if hasattr(self, 'last_valid_price') and self.last_valid_price > 0:
                self.max_price = max(self.max_price, self.last_valid_price)
                self.trailing_activated = True
                print(
                    f"{Fore.YELLOW}🔒 GREEDY MODE: Trailing stop activated at {self.last_valid_price:.8f} SOL{Style.RESET_ALL}")

                # GREEDY MODE: If we've secured more than 50% of initial, mark initial as recovered
                if tier_index >= 1 or (self.realized_profit >= self.entry_price * self.original_amount * 0.5):
                    self.initial_recovered = True
                    print(f"{Fore.GREEN}💰 GREEDY MODE: Initial investment secured!{Style.RESET_ALL}")

    def is_in_take_profit_cooldown(self):
        """Check if position is in cooldown period after take-profit."""
        if not hasattr(self, 'last_take_profit_time'):
            self.last_take_profit_time = 0

        if not hasattr(self, 'take_profit_cooldown'):
            self.take_profit_cooldown = 10  # GREEDY MODE: Short 10 second cooldown

        # GREEDY MODE: Shorter cooldowns for higher tiers
        adjusted_cooldown = self.take_profit_cooldown
        if hasattr(self, 'current_tier') and self.current_tier > 0:
            # Cooldown decreases with each tier hit
            adjusted_cooldown = max(3, self.take_profit_cooldown - (self.current_tier * 3))

        # GREEDY MODE: No cooldown if we've already recovered initial investment
        if hasattr(self, 'initial_recovered') and self.initial_recovered:
            adjusted_cooldown = 2  # Just 2 seconds to allow order processing

        elapsed = monotonic() - self.last_take_profit_time
        return elapsed < adjusted_cooldown

    @property
    def roi_pct(self) -> float:
        """
        Calculate ROI including realized profits.
        Returns the total return percentage based on original investment.
        """
        # Ensure we have required attributes with safeguards
        if not hasattr(self, 'entry_price') or self.entry_price <= 0:
            self.entry_price = 0.00000001  # Prevent division by zero

        if not hasattr(self, 'original_cost') or self.original_cost <= 0:
            self.original_cost = self.original_amount * self.entry_price

        if not hasattr(self, 'realized_profit'):
            self.realized_profit = 0.0

        # Calculate unrealized profit on remaining position
        current_price = max(0.00000001, getattr(self, 'current_price', self.entry_price))
        profit_unrealized = (current_price - self.entry_price) * self.amount_remaining

        # Add realized profit from previous sales
        total_profit = self.realized_profit + profit_unrealized

        # Calculate ROI as percentage of original investment
        roi = (total_profit / self.original_cost) * 100

        return roi

    # In position_manager.py
    # Inside the Position class definition
    # Add this method after the existing methods like update(), roi_pct(), etc.

    def update_realized_profit(self, sell_amount: float, sell_price: float) -> None:
        """
        Update realized profit after a partial sell.

        Args:
            sell_amount: Amount of tokens sold
            sell_price: Price per token received
        """
        # Calculate profit just from this sale (only the profit portion)
        cost_basis_per_token = self.entry_price
        profit_per_token = sell_price - cost_basis_per_token
        profit_from_sale = profit_per_token * sell_amount

        # Add to total realized profit
        if not hasattr(self, 'realized_profit'):
            self.realized_profit = 0.0

        self.realized_profit += profit_from_sale

        # Update remaining amount
        self.amount_remaining -= sell_amount

        # If we've sold almost everything, ensure amount_remaining is exactly 0
        if self.amount_remaining < 0.001:
            self.amount_remaining = 0

        # Log the progress
        original_investment = self.original_amount * self.entry_price
        percent_recovered = (self.realized_profit / original_investment) * 100

        # Track if we've secured our initial investment
        if percent_recovered >= 100 and not getattr(self, 'initial_recovered', False):
            self.initial_recovered = True
            print(f"{Fore.GREEN}💰 GREEDY MODE: Initial investment secured!{Style.RESET_ALL}")

    @property
    def websocket_roi(self) -> float:
        """
        Calculate ROI based on WebSocket price updates, including realized profit.
        """
        # Ensure we have valid prices
        if not hasattr(self, 'last_valid_price') or self.last_valid_price <= 0:
            return self.roi_pct  # Fall back to standard ROI

        # Ensure entry price is positive
        if not hasattr(self, 'entry_price') or self.entry_price <= 0:
            self.entry_price = max(abs(self.entry_price), 0.00000001)

        # Calculate unrealized profit using WebSocket price
        profit_unrealized = (self.last_valid_price - self.entry_price) * self.amount_remaining

        # Get realized profit (default to 0 if not available)
        realized_profit = getattr(self, 'realized_profit', 0.0)

        # Calculate total profit and ROI
        total_profit = realized_profit + profit_unrealized

        # Ensure we have original_cost to avoid division by zero
        if not hasattr(self, 'original_cost') or self.original_cost <= 0:
            self.original_cost = self.original_amount * self.entry_price

        # Calculate ROI
        roi = (total_profit / self.original_cost) * 100

        return roi

    def update(self, current_price: float) -> None:
        """
        Update the position with the current market price.

        Args:
            current_price: Current price of the token
        """
        # Validate price
        if current_price <= 0:
            if hasattr(self, 'price_check_count'):
                if self.price_check_count % 10 == 0:  # Don't spam logs
                    logger.warning(f"Received invalid zero/negative price for {self.symbol}")
                self.price_check_count += 1
            else:
                self.price_check_count = 1
            return

        # CRITICAL FIX: Ensure entry price is valid
        if not hasattr(self, 'entry_price') or self.entry_price <= 0:
            old_price = self.entry_price if hasattr(self, 'entry_price') else 0
            self.entry_price = max(abs(old_price), 0.00000001)
            logger.warning(f"⚠️ Fixed invalid entry price for {self.symbol}: {old_price} → {self.entry_price:.8f}")

            # Also update original_cost based on fixed entry price
            if hasattr(self, 'original_amount'):
                self.original_cost = self.original_amount * self.entry_price
            elif hasattr(self, 'amount'):
                self.original_cost = self.amount * self.entry_price

        # Check for extreme price changes (possible errors)
        if hasattr(self, 'last_valid_price') and self.last_valid_price > 0:
            price_ratio = current_price / self.last_valid_price
            if price_ratio > 5 or price_ratio < 0.2:
                logger.warning(
                    f"Extreme price change detected for {self.symbol}: {self.last_valid_price:.8f} → {current_price:.8f}")
                # Still accept the price, but log warning

        # Reset invalid count and update price
        self.price_check_count = 0
        self.last_valid_price = current_price
        self.current_price = current_price

        # CORRECTED ROI CALCULATION USING PROFIT-BASED FORMULA
        if self.entry_price > 0:
            # Calculate unrealized profit/loss percentage for price movement only
            self.unrealized_pnl_pct = ((current_price / self.entry_price) - 1) * 100

            # FIXED ROI CALCULATION: Use profit-based formula
            if hasattr(self, 'amount_remaining') and hasattr(self, 'original_cost') and self.original_cost > 0:
                # Calculate unrealized profit on remaining position
                profit_unrealized = (current_price - self.entry_price) * self.amount_remaining

                # Get realized profit (default to 0 if not available)
                realized_profit = getattr(self, 'realized_profit', 0.0)

                # Calculate total profit and ROI
                total_profit = realized_profit + profit_unrealized
                self.total_roi_pct = (total_profit / self.original_cost) * 100
        else:
            self.unrealized_pnl_pct = 0

        # Update peak price tracking
        if current_price > self.peak_price:
            self.peak_price = current_price

        # GREEDY MODE: Always update max price when price is higher
        # This ensures trailing stop works optimally
        if current_price > self.max_price:
            self.max_price = current_price

        # Update trailing-stop thresholds on new peaks
        if hasattr(self, 'update_trailing_stop'):
            self.update_trailing_stop(current_price)

        # If this was from a WebSocket update, mark the time
        if hasattr(self, 'using_websocket') and self.using_websocket:
            self.last_websocket_update = monotonic()

        # Track significant price changes for inactivity detection
        if not hasattr(self, 'last_checked_price'):
            self.last_checked_price = current_price
            self.last_price_check_time = monotonic()
        elif abs(current_price - self.last_checked_price) > 0.00000001:
            self.last_price_check_time = monotonic()
            self.last_checked_price = current_price

    def update_trailing_stop(self, current_price: float) -> None:
        """Update trailing stop with enhanced profit protection"""
        # Validate price
        if current_price <= 0:
            return

        # Track highest price for trailing stop
        if current_price > self.peak_price:
            self.peak_price = current_price

        if current_price > self.max_price:
            old_max = self.max_price
            self.max_price = current_price

            # Calculate new trailing stop
            trail_factor = self.trailing_stop_pct

            # PROFIT SECURITY: Tighter stops for larger profits
            if hasattr(self, 'entry_price') and self.entry_price > 0:
                profit_multiple = current_price / self.entry_price
                # Tighten trailing stop for higher profits
                if profit_multiple > 1.5:  # 50%+ profit
                    trail_factor = trail_factor * 0.7  # 30% tighter
                elif profit_multiple > 1.3:  # 30%+ profit
                    trail_factor = trail_factor * 0.8  # 20% tighter

            # If we've recovered initial investment, use extra-tight stops
            if hasattr(self, 'initial_recovered') and self.initial_recovered:
                trail_factor = trail_factor * 0.6  # 40% tighter

            # Calculate new stop price
            new_trailing_stop = current_price * (1 - trail_factor)

            # Only raise the trailing stop, never lower it
            if new_trailing_stop > self.trailing_stop_price:
                self.trailing_stop_price = new_trailing_stop

                # PROFIT SECURITY: Never let trailing stop go below entry after 20% profit
                if hasattr(self, 'entry_price') and self.entry_price > 0:
                    if current_price > self.entry_price * 1.2 and self.trailing_stop_price < self.entry_price:
                        self.trailing_stop_price = self.entry_price  # Ensure we at least break even
                        print(f"🛡️ Enhanced trailing stop: Break-even floor set for {self.symbol}")

    def get_exit_info(self, current_price: float) -> Optional[Dict]:
        """
        Check if this position should exit based on its current price.
        Returns exit info if an exit condition is met, None otherwise.
        GREEDY MODE: Enhanced to secure profits and exit at 2x
        """
        # Use the highest high price for trailing stops
        if current_price > self.max_price:
            self.max_price = current_price
            if self.max_price > self.entry_price and not self.trailing_activated:
                print(
                    f"{Fore.YELLOW}🔒 Trailing stop activated for {self.symbol} at {current_price:.8f}{Style.RESET_ALL}")
                self.trailing_activated = True

        # CORRECTED ROI CALCULATION using profit-based formula
        profit_unrealized = (current_price - self.entry_price) * self.amount_remaining
        realized_profit = getattr(self, 'realized_profit', 0.0)
        total_profit = realized_profit + profit_unrealized
        roi = (total_profit / self.original_cost) * 100

        # GREEDY MODE: Check for 2x profit - immediate full exit
        if current_price >= self.entry_price * 2.0:
            return {
                "reason": "greedy_2x_target",
                "price": current_price,
                "portion": 1.0,  # Sell everything at 2x
                "message": f"{Fore.GREEN}💰 JACKPOT! 2X PROFIT for {self.symbol} at {current_price:.8f} ({roi:.2f}%){Style.RESET_ALL}"
            }

        # Check stop-loss first - should always trigger regardless of cooldowns
        # FIX: Only trigger stop loss if price is BELOW entry price
        if self.stop_loss > 0 and current_price <= self.stop_loss and current_price < self.entry_price:
            return {
                "reason": "stop_loss",
                "price": current_price,
                "portion": 1.0,  # Always sell everything on stop loss
                "message": f"{Fore.RED}⚠️ STOP LOSS triggered for {self.symbol} at {current_price:.8f} ({roi:.2f}%){Style.RESET_ALL}"
            }

        # GREEDY MODE: Add ROI decline protection
        if hasattr(self, 'initial_recovered') and (self.initial_recovered or self.current_tier > 0):
            # Calculate how much ROI has declined from peak
            highest_profit_unrealized = (self.max_price - self.entry_price) * self.amount_remaining
            highest_total_profit = realized_profit + highest_profit_unrealized
            highest_roi = (highest_total_profit / self.original_cost) * 100
            roi_decline = highest_roi - roi

            # More aggressive protection if we've secured initial investment
            decline_threshold = 15 if self.initial_recovered else 25

            # If ROI has dropped significantly from peak after taking some profit
            if roi_decline > decline_threshold and roi > 0:
                return {
                    "reason": "roi_decline",
                    "price": current_price,
                    "portion": 1.0,  # Sell everything on significant decline
                    "message": f"{Fore.YELLOW}⚠️ ROI DECLINE triggered for {self.symbol}: {roi_decline:.1f}% drop from peak ROI {highest_roi:.1f}%{Style.RESET_ALL}"
                }

        # GREEDY MODE: Custom take-profit tiers
        # Default tiers are now [1.3, 1.6, 2.0]
        # Default portions are now [0.5, 0.5, 1.0]
        if self.current_tier < len(self.take_profit_tiers):
            # Calculate target price using the multiplier
            target_price = self.entry_price * self.take_profit_tiers[self.current_tier]

            if current_price >= target_price:
                # This tier has been hit
                portion = self.sell_portions[self.current_tier]
                tier_num = self.current_tier + 1
                self.current_tier += 1  # Move to the next tier

                return {
                    "reason": f"take_profit_tier_{tier_num}",
                    "price": current_price,
                    "portion": portion,  # Sell this portion of remaining tokens
                    "tier": self.current_tier - 1,  # The tier we just hit
                    "message": f"{Fore.GREEN}🚀 TAKE PROFIT TIER {tier_num} for {self.symbol} @ {current_price:.8f} (+{roi:.2f}%) | Entry: {self.entry_price}{Style.RESET_ALL}"
                }

        # Check trailing stop - but skip if in cooldown after take-profit
        if self.trailing_activated and not self.is_in_take_profit_cooldown():
            # GREEDY MODE: More aggressive trailing stop after securing initial investment
            trail_factor = self.trailing_stop_pct
            if hasattr(self, 'initial_recovered') and self.initial_recovered:
                # Tighter trailing stop (half of original) after initial recovery
                trail_factor = trail_factor / 2

            # Calculate trailing stop price
            trail_price = self.max_price * (1 - trail_factor)

            # Make sure trailing stop is never below entry price if we've hit take profit tier 1
            if self.current_tier > 0 and trail_price < self.entry_price:
                trail_price = self.entry_price
                if not hasattr(self, '_breakeven_message_shown') or not self._breakeven_message_shown:
                    print(
                        f"{Fore.CYAN}🛡️ GREEDY MODE: Trailing stop raised to breakeven for {self.symbol}{Style.RESET_ALL}")
                    self._breakeven_message_shown = True

            # Check if price dropped below trailing stop
            if current_price <= trail_price:
                return {
                    "reason": "trailing_stop",
                    "price": current_price,
                    "portion": 1.0,  # Always sell everything on trailing stop
                    "message": f"{Fore.YELLOW}⚠️ TRAILING STOP triggered for {self.symbol} at {current_price:.8f} (max: {self.max_price:.8f}) | ROI: {roi:.2f}%{Style.RESET_ALL}"
                }

        return None  # No exit condition met

    def mark_tier_taken(self) -> None:
        """
        Mark the current take profit tier as completed and move to next tier.
        Updates remaining amount based on sell portion.
        """
        if self.current_tier < len(self.sell_portions):
            portion = self.sell_portions[self.current_tier]
            self.amount_remaining *= (1 - portion)
            self.current_tier += 1

    def is_websocket_healthy(self, current_time=None):
        """Check if WebSocket updates for this position are current and healthy.

        Args:
            current_time: Optional timestamp for testing, uses monotonic() by default

        Returns:
            bool: True if WebSocket updates are recent, False otherwise
        """
        if current_time is None:
            current_time = monotonic()

        # Default timeout (2 minutes)
        timeout = getattr(self, "websocket_timeout", 120)

        # Get last update time
        last_update = getattr(self, "last_websocket_update", 0)

        # Consider healthy if using_websocket is true and we've had an update within timeout
        return getattr(self, "using_websocket", False) and (current_time - last_update) < timeout

    def is_websocket_monitored(self) -> bool:
        """Check if position is being monitored by WebSocket and the connection is healthy"""
        if hasattr(self, 'using_websocket') and self.using_websocket:
            return self.is_websocket_healthy()
        return self._is_websocket_monitored

    def is_inactive(self, inactive_timeout: float = 180.0) -> bool:
        """
        Check if this position has been inactive for too long.

        Args:
            inactive_timeout: Time in seconds before considering a position inactive

        Returns:
            bool: True if position has been inactive too long, False otherwise
        """
        current_time = monotonic()

        # Don't consider new positions inactive
        if not hasattr(self, 'buy_time') or current_time - self.buy_time < 30:
            return False

        # First check WebSocket updates for WebSocket-monitored positions
        if hasattr(self, 'using_websocket') and self.using_websocket:
            time_since_update = current_time - self.last_websocket_update
            if time_since_update > inactive_timeout:
                logger.warning(
                    f"Position {self.symbol} WebSocket inactive for {time_since_update:.1f}s (>{inactive_timeout}s)")
                return True

        # Second check price changes
        if hasattr(self, 'last_price_check_time'):
            time_since_price_change = current_time - self.last_price_check_time
            if time_since_price_change > inactive_timeout:
                logger.warning(
                    f"Position {self.symbol} price inactive for {time_since_price_change:.1f}s (>{inactive_timeout}s)")
                return True

        # Position is active
        return False


class PositionManager:
    def __init__(
            self,
            take_profit_tiers: List[float] = None,
            sell_portions: List[float] = None,
            stop_loss_pct: float = 0.10,
            trailing_stop_pct: float = 0.10,
            max_positions: int = 3

    ):
        self.positions: Dict[str, Position] = {}

        # Initialize take_profit_tiers FIRST
        self.take_profit_tiers = take_profit_tiers if take_profit_tiers is not None else [1.3, 1.6,
                                                                                          2.0]  # GREEDY MODE defaults

        # Set default values if none provided
        if sell_portions is None:
            self.sell_portions = [0.5, 0.5, 1.0]  # GREEDY MODE defaults
        else:
            self.sell_portions = sell_portions

        # NOW check if we need to adjust sell_portions
        if len(self.sell_portions) < len(self.take_profit_tiers):
            # Ensure we have enough sell portions
            while len(self.sell_portions) < len(self.take_profit_tiers):
                if len(self.sell_portions) == len(self.take_profit_tiers) - 1:
                    # Last tier sells everything remaining
                    self.sell_portions.append(1.0)
                else:
                    # Add default partial sell
                    self.sell_portions.append(0.5)  # GREEDY MODE uses 50% instead of 33%

        self.stop_loss_pct = stop_loss_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.max_positions = max_positions
        self._lock = asyncio.Lock()  # Add a lock for thread safety

        # Track pending positions for operations in progress
        self._pending_positions = 0

        logger.info(
            f"PositionManager initialized. Max positions: {max_positions}, TP tiers: {self.take_profit_tiers}, SL: {stop_loss_pct}, TS: {trailing_stop_pct}"
        )

    async def add_position(
            self,
            token_info: TokenInfo,
            buy_result: TradeResult,
            take_profit_tiers=None,
            sell_portions=None,
            stop_loss_pct=0.10,
            trailing_stop_pct=0.10,
            token_account=None  # Add this parameter to receive token account
    ) -> Position:
        """
        Add a new position to the position manager.
        GREEDY MODE: Updated to handle custom sell portions
        """
        token_key = str(token_info.mint)

        # GREEDY MODE: Default tiers and portions
        if take_profit_tiers is None:
            take_profit_tiers = [1.3, 1.6, 2.0]

        if sell_portions is None:
            sell_portions = [0.5, 0.5, 1.0]

        # Create the position
        position = Position(
            token_info=token_info,
            symbol=token_info.symbol,
            token_key=token_key,
            entry_price=buy_result.price,
            amount=buy_result.amount,
            tx_signature=buy_result.tx_signature,
            take_profit_tiers=take_profit_tiers,
            sell_portions=sell_portions,
            stop_loss_pct=stop_loss_pct,
            trailing_stop_pct=trailing_stop_pct
        )

        # Store the token account address if provided
        if token_account:
            position.token_account = token_account
            position.balance_source = "buy_transaction"

        # Add to position dictionary
        self.positions[token_key] = position

        return position

    async def reserve_position(self) -> bool:
        """
        Reserve a position slot before attempting to buy
        Returns True if a slot was reserved, False if no slots available
        """
        async with self._lock:
            if len(self.positions) + self._pending_positions >= self.max_positions:
                return False
            self._pending_positions += 1
            return True

    async def release_reserved_position(self):
        """Release a reserved position slot if the buy fails"""
        async with self._lock:
            if self._pending_positions > 0:
                self._pending_positions -= 1

    def get_position(self, token_key: str) -> Optional[Position]:
        """Get position by token key"""
        return self.positions.get(token_key)

    def get_position_by_mint(self, mint: Pubkey) -> Optional[Position]:
        """Get position by token mint address"""
        token_key = str(mint)
        return self.get_position(token_key)

    def get_position_by_bonding_curve(self, bonding_curve: Pubkey) -> Optional[Position]:
        """Get position by bonding curve address"""
        for position in self.positions.values():
            if str(position.token_info.bonding_curve) == str(bonding_curve):
                return position
        return None

    def get_all_positions(self):
        """
        Get all positions as a list.
        Used by monitoring functions.
        """
        return list(self.positions.values())

    def get_positions_needing_rpc_updates(self):
        """
        Get positions that need RPC price updates.
        This excludes positions that are actively receiving WebSocket updates.
        """
        positions = self.get_all_positions()

        # Filter positions that are not actively receiving WebSocket updates
        # or haven't received an update recently
        current_time = monotonic()

        needing_updates = []
        for pos in positions:
            # Skip positions that have recent websocket updates
            if (hasattr(pos, 'is_websocket_monitored') and
                    pos.is_websocket_monitored() and
                    hasattr(pos, 'last_websocket_update') and
                    current_time - pos.last_websocket_update < 5):
                continue

            # Include positions that need RPC updates
            needing_updates.append(pos)

        return needing_updates

    def should_sell(self, position: Position) -> bool:
        """
        Determine if a position should be sold based on its current state.
        This allows the WebSocket handler to use the same logic as the main loop.
        """
        if not position or not hasattr(position, 'last_valid_price') or position.last_valid_price <= 0:
            return False

        # First check standard exit conditions
        exit_info = position.get_exit_info(position.last_valid_price)
        if exit_info:
            return True

        # Also check if position is inactive
        if position.is_inactive():
            return True

        return False

    async def verify_token_account_ready(self, position, max_retries=3, delay_base=1.0):
        """
        Verify that a token account is ready for operations, with retry mechanism.

        Args:
            position: The position containing the token account
            max_retries: Maximum number of retry attempts
            delay_base: Base delay between retries (will use exponential backoff)

        Returns:
            bool: True if account is ready for operations, False otherwise
        """
        if not position or not position.token_account:
            return False

        token_account = position.token_account

        for attempt in range(max_retries):
            try:
                # Check if the account exists and has balance
                # This should be replaced with your actual RPC check method
                balance = await self.client.get_token_account_balance(token_account)

                if balance and balance > 0:
                    # Account exists and has balance
                    position.balance_source = "rpc_verified"
                    return True

                # Account exists but no balance - wait before retry
                logger.info(
                    f"Token account {token_account[:8]}...{token_account[-4:]} exists but no balance (attempt {attempt + 1}/{max_retries})")

            except Exception as e:
                # Account doesn't exist or error - wait before retry
                logger.info(
                    f"Token account {token_account[:8]}...{token_account[-4:]} verification failed (attempt {attempt + 1}/{max_retries}): {e}")

            # Exponential backoff for retries
            await asyncio.sleep(delay_base * (2 ** attempt))

        return False

    async def remove_position(self, token_key: str) -> bool:
        """Thread-safe position removal"""
        async with self._lock:
            if token_key in self.positions:
                logger.info(f"Position closed: {token_key}")
                del self.positions[token_key]
                return True
            return False

    async def has_capacity(self) -> bool:
        """Thread-safe capacity check"""
        async with self._lock:
            return len(self.positions) + self._pending_positions < self.max_positions

    def get_position_count(self) -> int:
        """Get current position count (not including pending)"""
        return len(self.positions)

    def get_total_position_count(self) -> int:
        """Get total position count including pending positions"""
        return len(self.positions) + self._pending_positions

    def get_all_positions_summary(self) -> str:
        """Return a formatted summary of all active positions"""
        if not self.positions:
            return "💼 NO ACTIVE POSITIONS"

        result = [f"💼 ACTIVE POSITIONS: {len(self.positions)}/{self.max_positions}"]

        for position in self.positions.values():
            # Fix any invalid entry prices
            if position.entry_price <= 0:
                position.entry_price = max(abs(position.entry_price), 0.00000001)

            # Get ROI using websocket data if available
            if position.is_websocket_monitored():
                roi = position.websocket_roi
            else:
                roi = position.roi_pct

            # Format ROI with color indicators
            if roi >= 10:
                roi_color = "🟢"
            elif roi >= 0:
                roi_color = "⚪"
            else:
                roi_color = "🔴"

            roi_display = f"{roi:.2f}%"

            # Format position line
            ws_status = "✅" if position.is_websocket_monitored() else "❌"
            position_line = f"  {position.symbol}: Entry {position.entry_price:.8f} | ROI: {roi_color} {roi_display} | WS: {ws_status}"

            # Add GREEDY MODE indicator if initial investment has been recovered
            if getattr(position, 'initial_recovered', False):
                position_line += f" | 💰 Initial Secured"

            result.append(position_line)

        return "\n".join(result)

    def update_position_from_websocket(self, mint: Pubkey, price: float) -> Optional[Dict]:
        """
        Update a position's price from WebSocket data and check if it should sell.
        Returns exit info if the position should be sold, None otherwise.
        """
        # Convert Pubkey to string if needed
        token_key = str(mint)

        # Get position
        position = self.get_position(token_key)
        if not position:
            return None

        # Mark position as using WebSocket and update last check time
        position.mark_as_websocket_monitored()

        # Update the position with the new price
        position.update(price)

        # Calculate ROI for logging
        roi = position.roi_pct

        # FIXED: Only trigger stop loss if price is BELOW entry price
        if position.stop_price > 0 and price <= position.stop_price and price < position.entry_price:
            logger.info(
                f"⚠️ STOP_LOSS triggered for {position.symbol}: {price:.8f} vs stop {position.stop_price:.8f} | ROI: {roi:.2f}%")
            return {"reason": "stop_loss", "price": price, "portion": 1.0}

        # Check for take profit tiers
        for tier_num, tp_multiplier in enumerate(position.take_profit_tiers):
            # Skip tiers already taken
            if tier_num < len(position.take_profit_hit) and position.take_profit_hit[tier_num]:
                continue

            # Calculate the actual target price using the multiplier
            target_price = position.entry_price * tp_multiplier

            if price >= target_price:
                # This tier has been hit
                print(
                    f"{Fore.GREEN}🚀 TAKE PROFIT TIER {tier_num + 1} triggered for {position.symbol} at {price:.8f} (+{roi:.2f}%){Style.RESET_ALL}")

                # Mark the tier as hit
                if tier_num < len(position.take_profit_hit):
                    position.mark_take_profit_hit(tier_num)

                # Calculate percentage of remaining holdings to sell for this tier
                sell_percent = position.sell_portions[tier_num] if tier_num < len(position.sell_portions) else 0.5

                logger.info(
                    f"💰 TAKE_PROFIT tier {tier_num + 1} triggered for {position.symbol}: {price:.8f} | ROI: {roi:.2f}% | Selling {sell_percent:.1f}%")

                return {
                    "reason": f"take_profit_tier_{tier_num + 1}",
                    "price": price,
                    "portion": sell_percent,
                    "tier": tier_num
                }

        # Check for trailing stop - honor cooldown period
        if position.trailing_activated and not position.is_in_take_profit_cooldown():
            trail_price = position.max_price * (1 - position.trailing_stop_pct)

            if price <= trail_price:
                logger.info(
                    f"⚠️ TRAILING_STOP triggered for {position.symbol}: {price:.8f} vs {position.max_price:.8f} | ROI: {roi:.2f}%")
                return {"reason": "trailing_stop", "price": price, "portion": 1.0}

        return None

    def get_positions_needing_rpc_updates(self, max_silence_seconds: float = 30.0) -> List[Position]:
        """
        Get positions that aren't receiving WebSocket updates and need RPC updates.
        This allows fallback to RPC if WebSockets stop working for any reason.
        """
        result = []
        for position in self.positions.values():
            if not position.is_websocket_healthy(max_silence_seconds):
                result.append(position)
        return result

    def get_positions(self) -> Dict[str, Position]:
        """
        Return a list of all currently tracked positions.
        """
        return self.positions

    def get_inactive_positions(self, inactive_timeout: float = 180.0) -> List[Position]:
        """
        Get positions that have been inactive for too long.

        Args:
            inactive_timeout: Time in seconds before considering a position inactive

        Returns:
            List of inactive positions
        """
        result = []
        for position in self.positions.values():
            if position.is_inactive(inactive_timeout):
                result.append(position)
        return result

    async def remove_ghost_positions(self, client):
        """
        Scan for positions with zero balance and remove them from tracking.
        This fixes "ghost positions" that show in the UI but have no actual tokens.

        Args:
            client: Solana client for RPC calls

        Returns:
            int: Number of positions removed
        """
        positions_to_remove = []

        for token_key, position in self.positions.items():
            # Skip positions currently being processed for selling
            if hasattr(position, '_active_sell') and position._active_sell:
                continue

            # 1. First check if position is marked for removal
            if hasattr(position, '_marked_for_exit') and position._marked_for_exit:
                positions_to_remove.append(token_key)
                logger.info(f"Removing marked position: {position.symbol}")
                continue

            # 2. If we don't have a token account, this might be a ghost
            if not hasattr(position, 'token_account') or not position.token_account:
                positions_to_remove.append(token_key)
                logger.info(f"Removing ghost position: {position.symbol} (no token account)")
                continue

            # 3. Check position balance
            try:
                # Try WebSocket balance first
                has_balance = False

                if hasattr(position, 'amount_remaining') and position.amount_remaining > 0:
                    if hasattr(position, 'using_websocket') and position.using_websocket:
                        # For WebSocket-monitored positions, trust the WebSocket balance
                        has_balance = True
                    else:
                        # For non-WebSocket positions, verify with RPC
                        try:
                            balance = await client.get_token_account_balance(position.token_account)
                            has_balance = balance > 0
                        except Exception as e:
                            # If we can't get the balance, the account might not exist
                            logger.warning(
                                f"Failed to check balance for possible ghost position {position.symbol}: {e}")
                            # Mark for removal if we've failed to sell multiple times
                            if hasattr(position, 'failed_sell_attempts') and position.failed_sell_attempts >= 2:
                                positions_to_remove.append(token_key)
                                logger.info(
                                    f"Removing ghost position: {position.symbol} (multiple failed sell attempts)")
                                continue

                # If we've confirmed there's no balance, remove the position
                if not has_balance:
                    positions_to_remove.append(token_key)
                    logger.info(f"Removing ghost position: {position.symbol} (zero balance confirmed)")

            except Exception as e:
                logger.error(f"Error checking ghost position status for {position.symbol}: {e}")

        # Remove the identified ghost positions
        removed_count = 0
        for token_key in positions_to_remove:
            if await self.remove_position(token_key):
                removed_count += 1

        if removed_count > 0:
            logger.info(f"Removed {removed_count} ghost positions")

        return removed_count

    async def handle_failed_sell(self, token_key, reason):
        """
        Handle a failed sell attempt for a position.

        Args:
            token_key: Token key of the position
            reason: Reason for the failure

        Returns:
            bool: True if the position should be retried, False otherwise
        """
        position = self.get_position(token_key)
        if not position:
            return False

        # Initialize failed sell attempts if not present
        if not hasattr(position, 'failed_sell_attempts'):
            position.failed_sell_attempts = 0

        position.failed_sell_attempts += 1

        # If this is a "could not find account" error, it's likely a zombie token
        if "could not find account" in reason:
            # Mark the position for clean-up by remove_ghost_positions
            position._marked_for_exit = True
            logger.warning(f"⚠️ Marking zombie position {position.symbol} for removal: token account not found")
            return False

        # For other errors, allow retry up to 3 times
        if position.failed_sell_attempts >= 3:
            position._marked_for_exit = True
            logger.warning(
                f"⚠️ Marking position {position.symbol} for removal after {position.failed_sell_attempts} failed sell attempts")
            return False

        logger.info(f"⚠️ Sell attempt failed for {position.symbol} ({position.failed_sell_attempts}/3): {reason}")
        return True  # Allow retry for other errors