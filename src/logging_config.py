import logging
import sys

def setup_logging(console_level=logging.INFO, file_level=logging.DEBUG):
    """Simple logging setup that accepts parameters for compatibility"""
    # Remove any existing handlers
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    
    # Create a console handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(console_level)  # Use the passed level
    
    # Use a simple formatter that won't require special attributes
    formatter = logging.Formatter('%(message)s')
    handler.setFormatter(formatter)
    
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)  # Root logger captures everything
    
    # Optionally add file logging
    try:
        file_handler = logging.FileHandler("trading_bot.log")
        file_handler.setLevel(file_level)
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)
    except Exception:
        pass
    
    # Set lower levels for some noisy modules
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    
    return root
