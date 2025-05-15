import os
import json
from datetime import datetime
from typing import Dict, List, Optional, Any  # Make sure Optional is imported here
from statistics import mean, stdev
from colorama import Fore, Style

from utils.logger import get_logger

logger = get_logger(__name__)


class TradingAnalytics:
    """Tracks and analyzes trading performance, including open positions and completed trades."""

    def __init__(self, log_dir: str = "analytics"):
        """
        Initialize analytics engine.
        Args:
            log_dir: Directory to store trade logs, positions, and metrics
        """
        self.log_dir = log_dir
        self.trades: List[Dict[str, Any]] = []  # Completed trade records
        self.open_positions: List[Dict[str, Any]] = []  # Currently open positions
        self.equity_curve: List[float] = [1.0]  # Equity multiplier over time
        self.peak_equity: float = 1.0

        os.makedirs(log_dir, exist_ok=True)
        # File paths
        self.trade_log_path = os.path.join(log_dir, "trades.json")
        self.positions_path = os.path.join(log_dir, "positions.json")
        self.metrics_path = os.path.join(log_dir, "metrics.json")

        # Load existing data
        self._load_existing_data()
        self._load_existing_positions()

        logger.info(f"Analytics engine initialized (data dir: {log_dir})")

    def _load_existing_data(self):
        """Load existing completed trade data if available."""
        try:
            if os.path.exists(self.trade_log_path):
                with open(self.trade_log_path, "r") as f:
                    self.trades = json.load(f)
                logger.info(f"Loaded {len(self.trades)} historical trades")
        except Exception as e:
            logger.error(f"Error loading trade history: {e}")

    def _load_existing_positions(self):
        """Load existing open positions if available."""
        try:
            if os.path.exists(self.positions_path):
                with open(self.positions_path, "r") as f:
                    self.open_positions = json.load(f)
                logger.info(f"Loaded {len(self.open_positions)} open positions")
        except Exception as e:
            logger.error(f"Error loading open positions: {e}")

    def get_summary_text(self):
        """Return a summary of trading statistics."""
        summary = []
        summary.append("\n===== TRADING SUMMARY =====")

        if not self.trades:
            summary.append("No completed trades yet")
            summary.append("=========================")
            return "\n".join(summary)

        # Calculate statistics
        win_trades = [t for t in self.trades if t["roi_pct"] > 0]
        loss_trades = [t for t in self.trades if t["roi_pct"] <= 0]

        # Format metrics
        total_trades = len(self.trades)
        win_count = len(win_trades)
        loss_count = len(loss_trades)

        win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0

        total_profit = sum(t["pnl_sol"] for t in self.trades)
        avg_roi = mean([t["roi_pct"] for t in self.trades]) if self.trades else 0

        # Average hold times
        avg_hold_time = mean([t["hold_time_seconds"] for t in self.trades]) if self.trades else 0

        # Current equity vs peak
        drawdown = ((self.peak_equity - self.equity_curve[-1]) / self.peak_equity * 100) if self.peak_equity > 0 else 0

        # Add to summary
        summary.append(f"Total Trades: {total_trades} (🟢 {win_count} | 🔴 {loss_count})")
        summary.append(f"Win Rate: {win_rate:.2f}%")
        summary.append(f"Average ROI: {avg_roi:.2f}%")
        summary.append(f"Total Profit/Loss: {total_profit:.6f} SOL")
        summary.append(f"Average Hold Time: {avg_hold_time:.1f}s")
        summary.append(f"Max Drawdown: {drawdown:.2f}%")
        summary.append("=========================")

        return "\n".join(summary)


    def _save_trade_log(self) -> None:
        """Save completed trade log to disk."""
        try:
            with open(self.trade_log_path, "w") as f:
                json.dump(self.trades, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving trade log: {e}")

    def _save_positions(self) -> None:
        """Save current open positions to disk."""
        try:
            with open(self.positions_path, "w") as f:
                json.dump(self.open_positions, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving open positions: {e}")

    def log_open_position(
            self,
            token_symbol: str,
            token_address: str,
            entry_price: float,
            amount: float,
            entry_time: float,
            entry_tx: Optional[str] = None
    ) -> None:
        """
        Record a new open position.
        """
        if entry_tx is not None:
            entry_tx = str(entry_tx)
        position = {
            "token_symbol": token_symbol,
            "token_address": token_address,
            "entry_price": entry_price,
            "amount": amount,
            "entry_time": entry_time,
            "entry_tx": entry_tx,
            "timestamp": datetime.utcnow().isoformat()
        }
        self.open_positions.append(position)
        self._save_positions()
        logger.info(f"Open position logged: {token_symbol} {amount:.6f} @ {entry_price:.8f}")

    def log_trade(
            self,
            token_symbol: str,
            token_address: str,
            entry_price: float,
            exit_price: float,
            amount: float,
            entry_time: float,
            exit_time: float,
            entry_tx: Optional[str] = None,
            exit_tx: Optional[str] = None,
            exit_reason: str = "unknown",
            position: Optional[Any] = None  # Add this parameter
    ) -> None:
        """
        Log a completed trade and remove the position from open positions.
        """
        # Calculate metrics
        hold_time = exit_time - entry_time

        # Use position's ROI if available (this will include realized profits)
        if position and hasattr(position, 'roi_pct'):
            roi_pct = position.roi_pct
            # Use position's realized profit if available
            if hasattr(position, 'realized_profit'):
                pnl_sol = position.realized_profit
            else:
                pnl_sol = (exit_price - entry_price) * amount
        else:
            # Traditional calculation as fallback
            pnl_sol = (exit_price - entry_price) * amount
            roi_pct = (exit_price / entry_price - 1) * 100

        if entry_tx is not None:
            entry_tx = str(entry_tx)
        if exit_tx is not None:
            exit_tx = str(exit_tx)

        # Build trade record
        trade = {
            "token_symbol": token_symbol,
            "token_address": token_address,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "amount": amount,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "hold_time_seconds": hold_time,
            "pnl_sol": pnl_sol,
            "roi_pct": roi_pct,
            "entry_tx": entry_tx,
            "exit_tx": exit_tx,
            "exit_reason": exit_reason,
            "timestamp": datetime.utcnow().isoformat()
        }
        self.trades.append(trade)
        self._save_trade_log()

        # Update equity
        self.equity_curve.append(self.equity_curve[-1] * (1 + roi_pct / 100))
        if self.equity_curve[-1] > self.peak_equity:
            self.peak_equity = self.equity_curve[-1]

        # Remove from open_positions
        self.open_positions = [pos for pos in self.open_positions if not (
                pos["token_address"] == token_address and abs(pos["entry_time"] - entry_time) < 1e-6
        )]
        self._save_positions()

        # Log summary
        roi_color = "🟢" if roi_pct > 0 else "🔴"
        logger.info(
            f"Trade logged: {token_symbol} {roi_color} {roi_pct:.2f}% ({pnl_sol:.6f} SOL) | "
            f"Hold: {hold_time:.1f}s | Reason: {exit_reason}"
        )