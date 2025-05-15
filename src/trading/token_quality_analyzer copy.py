# src/trading/token_quality_analyzer.py

from typing import Dict, Optional, Any
from client.moralis_client import MoralisClient
from utils.logger import get_logger
from colorama import Fore, Style

logger = get_logger(__name__)

class TokenQualityAnalyzer:
    """
    Analyzes Pump.fun token quality using Moralis Pump.fun-specific endpoints.
    Uses Moralis verified volume data instead of relying solely on Pump Portal.
    """
    
    def __init__(self, moralis_client: Optional[MoralisClient] = None):
        self.moralis = moralis_client
        self.use_moralis = moralis_client is not None and moralis_client.enabled
        
        # Configuration
        self.min_score_threshold = 65  # Min score to recommend buy
        self.volume_threshold = 3.0    # Min volume in SOL
        self.trade_threshold = 2       # Min number of trades
        
    def set_thresholds(self, min_score=65, min_volume=3.0, min_trades=2):
        """Set quality threshold values."""
        self.min_score_threshold = min_score
        self.volume_threshold = min_volume
        self.trade_threshold = min_trades
        
    async def analyze_token(self, token_mint: str, pump_data: Dict) -> Dict[str, Any]:
        """
        Analyze token quality prioritizing Moralis verified volume data.
        Falls back to Pump Portal data if Moralis is unavailable.
        """
        # Extract pump data metrics as backup
        pump_volume_sol = pump_data.get("volume", 0)
        pump_trades = pump_data.get("trades", 0)
        time_since_creation = pump_data.get("age_seconds", 0)
        
        # Default to unverified pump data
        volume_sol = pump_volume_sol
        trade_count = pump_trades
        verified = False
        
        # Calculate pump portal score (0-100) as backup
        pump_score = min(100, pump_volume_sol * 5)  # 20 SOL → 100 score
        
        # Default metrics
        metrics = {
            "pump_score": pump_score,
            "moralis_score": 0,
            "volume_sol": volume_sol,
            "trades": trade_count,
            "moralis_available": False,
            "moralis_enabled": self.use_moralis,
            "verified": False
        }
        
        # Default to pump portal data
        final_score = pump_score
        buy_recommended = (pump_volume_sol >= self.volume_threshold and 
                          pump_trades >= self.trade_threshold and
                          pump_score >= self.min_score_threshold)
        
        # If Moralis is enabled, get verified volume and quality data
        if self.use_moralis:
            try:
                # Get detailed analysis with verified volume data
                moralis_data = await self.moralis.analyze_token(token_mint)
                
                if moralis_data.get("enabled", False) and "error" not in moralis_data:
                    # Replace pump portal data with verified Moralis data
                    if "verified_volume_sol" in moralis_data:
                        volume_sol = moralis_data["verified_volume_sol"]
                        metrics["volume_sol"] = volume_sol
                        verified = True
                        
                    if "verified_swap_count" in moralis_data:
                        trade_count = moralis_data["verified_swap_count"]
                        metrics["trades"] = trade_count
                    
                    metrics["verified"] = verified
                    moralis_score = moralis_data.get("score", 0)
                    metrics["moralis_score"] = moralis_score
                    metrics["moralis_available"] = True
                    
                    # Copy other metrics
                    for key in ["top_holder_pct", "unique_wallets", "bonding_ratio", 
                                "distribution_score", "activity_score", "bonding_score"]:
                        if key in moralis_data:
                            metrics[key] = moralis_data[key]
                    
                    if "red_flags" in moralis_data and moralis_data["red_flags"]:
                        metrics["red_flags"] = moralis_data["red_flags"]
                    
                    # Only trust Moralis data for volume validation if verification is complete
                    if verified:
                        # Hybrid score calculation - weighted average
                        final_score = moralis_score
                        
                        # Apply red flag penalties
                        if moralis_data.get("top_holder_pct", 100) > 90:
                            # Creator holds >90% of pool value - major red flag
                            final_score *= 0.3  # Severe penalty
                            
                        if moralis_data.get("bonding_ratio", 100) > 95:
                            # Over 95% of tokens still in bonding curve - red flag
                            final_score *= 0.7  # Significant penalty
                            
                        # Update buy recommendation based on VERIFIED volume from Moralis
                        # This is the key change - we now use verified volume data
                        volume_meets_threshold = volume_sol >= self.volume_threshold
                        trades_meet_threshold = trade_count >= self.trade_threshold
                        score_meets_threshold = final_score >= self.min_score_threshold
                        
                        buy_recommended = volume_meets_threshold and trades_meet_threshold and score_meets_threshold
            except Exception as e:
                logger.error(f"Error during Moralis verification: {e}")
                # Fall back to pump portal data if Moralis fails
        
        # Final decision and metrics
        metrics["final_score"] = int(final_score)
        metrics["buy_recommended"] = buy_recommended
        
        return metrics
    
    def get_decision_explanation(self, metrics: Dict) -> str:
        """Generate a human-readable explanation of the token quality decision."""
        verified_tag = f"{Fore.GREEN}[VERIFIED]{Style.RESET_ALL} " if metrics.get("verified", False) else ""
        
        if metrics["buy_recommended"]:
            base_msg = f"{Fore.GREEN}✅ {verified_tag}Token quality check passed - Score: {metrics['final_score']}/100{Style.RESET_ALL}"
            
            details = []
            details.append(f"Volume: {metrics['volume_sol']:.2f} SOL")
            details.append(f"Trades: {metrics['trades']}")
            
            if metrics.get("moralis_available", False):
                if "distribution_score" in metrics:
                    details.append(f"Distribution score: {metrics['distribution_score']}")
                
                if "unique_wallets" in metrics:
                    details.append(f"Unique wallets: {metrics['unique_wallets']}")
                    
                if "bonding_ratio" in metrics:
                    details.append(f"Bonding ratio: {metrics['bonding_ratio']}%")
            
            if details:
                return f"{base_msg}\n   {' | '.join(details)}"
            return base_msg
        else:
            base_msg = f"{Fore.YELLOW}⚠️ {verified_tag}Token quality check failed - Score: {metrics['final_score']}/100{Style.RESET_ALL}"
            
            reasons = []
            if metrics.get("volume_sol", 0) < self.volume_threshold:
                reasons.append(f"Low volume ({metrics.get('volume_sol', 0):.2f} SOL)")
                
            if metrics.get("trades", 0) < self.trade_threshold:
                reasons.append(f"Few trades ({metrics.get('trades', 0)})")
                
            if metrics.get("moralis_available", False):
                if metrics.get("top_holder_pct", 0) > 90:
                    reasons.append(f"Creator holds {metrics.get('top_holder_pct', 0):.1f}% of pool value")
                
                if metrics.get("unique_wallets", 0) < 5:
                    reasons.append(f"Only {metrics.get('unique_wallets', 0)} unique wallets")
                    
                if metrics.get("bonding_ratio", 0) > 95:
                    reasons.append(f"{metrics.get('bonding_ratio', 0):.1f}% of tokens still in bonding curve")
            
            if "red_flags" in metrics and metrics["red_flags"]:
                reasons.extend(metrics["red_flags"])
                
            if reasons:
                return f"{base_msg}\n   {Fore.YELLOW}🚩 Reasons: {', '.join(reasons)}{Style.RESET_ALL}"
            return base_msg
