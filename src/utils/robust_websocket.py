"""
Robust WebSocket client for reliable Pump.fun monitoring
"""

import asyncio
import json
import time
import random
from typing import Callable, Dict, List, Optional, Any, Coroutine
import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK
from colorama import Fore, Style

from utils.logger import get_logger

logger = get_logger("websocket")

class RobustWebSocket:
    """
    Robust WebSocket client with automatic reconnection
    and monitoring features.
    """

    def __init__(
        self, 
        url: str,
        on_message: Callable[[dict], Coroutine],
        on_connect: Optional[Callable[[], Coroutine]] = None,
        on_disconnect: Optional[Callable[[], Coroutine]] = None,
        ping_interval: int = 30,
        reconnect_interval_min: int = 1,
        reconnect_interval_max: int = 30,
        max_reconnect_attempts: int = 0,  # 0 = unlimited
        message_timeout: int = 60,
    ):
        """
        Initialize the robust WebSocket client.

        Args:
            url: WebSocket URL to connect to
            on_message: Async callback function to process messages
            on_connect: Optional async callback when connection is established
            on_disconnect: Optional async callback when connection is lost
            ping_interval: Interval between ping messages (seconds)
            reconnect_interval_min: Minimum reconnection interval (seconds)
            reconnect_interval_max: Maximum reconnection interval (seconds)
            max_reconnect_attempts: Maximum number of reconnection attempts (0 = unlimited)
            message_timeout: Maximum time to wait for a message before checking connection
        """
        self.url = url
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.ping_interval = ping_interval
        self.reconnect_interval_min = reconnect_interval_min
        self.reconnect_interval_max = reconnect_interval_max
        self.max_reconnect_attempts = max_reconnect_attempts
        self.message_timeout = message_timeout
        
        # Connection state
        self.websocket = None
        self.connection_task = None
        self.ping_task = None
        self.running = False
        self.connected = False
        self.reconnect_attempt = 0
        self.last_message_time = 0
        
        # Stats
        self.messages_received = 0
        self.successful_connections = 0
        self.connection_errors = 0
        self.last_error = None
        self.connect_time = None
        self.disconnect_time = None
        
    async def start(self):
        """Start the WebSocket client."""
        if self.running:
            logger.warning("WebSocket client already running")
            return
            
        logger.info(f"{Fore.YELLOW}🔌 Starting robust WebSocket client...{Style.RESET_ALL}")
        self.running = True
        self.reconnect_attempt = 0
        
        # Start the connection task
        self.connection_task = asyncio.create_task(self._connection_loop())
        
    async def stop(self):
        """Stop the WebSocket client gracefully."""
        if not self.running:
            return
            
        logger.info(f"{Fore.YELLOW}🔌 Stopping WebSocket client...{Style.RESET_ALL}")
        self.running = False
        
        # Cancel tasks
        if self.ping_task and not self.ping_task.done():
            self.ping_task.cancel()
            
        if self.connection_task and not self.connection_task.done():
            self.connection_task.cancel()
            
        # Close WebSocket connection
        await self._close_connection()
        logger.info(f"{Fore.YELLOW}WebSocket client stopped{Style.RESET_ALL}")
            
    async def _connection_loop(self):
        """Main connection loop with reconnection logic."""
        while self.running:
            try:
                # Clear any existing connection
                await self._close_connection()
                
                # Check if we should keep trying to reconnect
                if (self.max_reconnect_attempts > 0 and 
                    self.reconnect_attempt >= self.max_reconnect_attempts):
                    logger.error(f"{Fore.RED}✗ Maximum reconnection attempts reached ({self.max_reconnect_attempts}){Style.RESET_ALL}")
                    self.running = False
                    break
                    
                # Attempt to connect
                if self.reconnect_attempt > 0:
                    # Calculate backoff time with exponential backoff and jitter
                    backoff = min(
                        self.reconnect_interval_max,
                        self.reconnect_interval_min * (2 ** (self.reconnect_attempt - 1))
                    )
                    jitter = random.uniform(0, min(backoff * 0.1, 1.0))
                    wait_time = backoff + jitter
                    
                    logger.info(f"{Fore.YELLOW}Reconnecting in {wait_time:.1f}s (attempt {self.reconnect_attempt})...{Style.RESET_ALL}")
                    await asyncio.sleep(wait_time)
                    
                # Connect to the WebSocket
                logger.info(f"{Fore.CYAN}⚡ Connecting to WebSocket {self.url}...{Style.RESET_ALL}")
                
                # Create connection with timeout
                connect_timeout = 15  # seconds
                self.websocket = await asyncio.wait_for(
                    websockets.connect(
                        self.url,
                        ping_interval=None,  # We'll handle pings ourselves
                        ping_timeout=None,
                        close_timeout=5,     # Allow 5 seconds for closing
                        max_size=10 * 1024 * 1024  # 10MB max message size
                    ),
                    timeout=connect_timeout
                )
                
                # Connection successful
                self.connected = True
                self.successful_connections += 1
                self.connect_time = time.time()
                self.last_message_time = time.time()
                logger.info(f"{Fore.GREEN}✓ Connected to WebSocket{Style.RESET_ALL}")
                
                # Call the on_connect callback if provided
                if self.on_connect:
                    try:
                        await self.on_connect()
                    except Exception as e:
                        logger.error(f"Error in on_connect callback: {e}")
                        
                # Start the ping task
                self.ping_task = asyncio.create_task(self._ping_loop())
                
                # Process messages
                await self._process_messages()
                
                # If we get here, the connection was closed
                self.connected = False
                self.disconnect_time = time.time()
                
                # Call the on_disconnect callback if provided
                if self.on_disconnect:
                    try:
                        await self.on_disconnect()
                    except Exception as e:
                        logger.error(f"Error in on_disconnect callback: {e}")
                    
                # Increment the reconnection attempt
                self.reconnect_attempt += 1
                
            except asyncio.TimeoutError:
                # Connection timeout
                self.connection_errors += 1
                self.last_error = "Connection timeout"
                logger.error(f"{Fore.RED}✗ Connection timeout{Style.RESET_ALL}")
                self.reconnect_attempt += 1
                
            except asyncio.CancelledError:
                # Task was cancelled
                logger.info("Connection task cancelled")
                break
                
            except Exception as e:
                # General connection error
                self.connection_errors += 1
                self.last_error = str(e)
                logger.error(f"{Fore.RED}✗ Connection error: {e}{Style.RESET_ALL}")
                self.reconnect_attempt += 1
                
        # Ensure connection is closed when we exit the loop
        await self._close_connection()

    async def _process_messages(self):
        """Process incoming WebSocket messages."""
        if not self.websocket:
            return

        try:
            # Use a safer way to check if connection is open
            while self.running and self.websocket and self._is_websocket_open():
                try:
                    # Wait for a message with timeout
                    message = await asyncio.wait_for(
                        self.websocket.recv(),
                        timeout=self.message_timeout
                    )

                    # Update message stats
                    self.last_message_time = time.time()
                    self.messages_received += 1

                    # Parse and process the message
                    try:
                        data = json.loads(message)
                        await self.on_message(data)
                    except json.JSONDecodeError:
                        logger.error(f"{Fore.RED}✗ Invalid JSON received{Style.RESET_ALL}")

                except asyncio.TimeoutError:
                    # No message received within timeout, check connection
                    elapsed = time.time() - self.last_message_time
                    logger.debug(f"No messages for {elapsed:.1f}s, checking connection")

                    # Send a ping to check if the connection is still alive
                    # Use the safer method to check connection state
                    if self.websocket and self._is_websocket_open():
                        try:
                            pong_waiter = await self.websocket.ping()
                            await asyncio.wait_for(pong_waiter, timeout=5)
                            logger.debug(f"{Fore.GREEN}✓ Connection ping succeeded{Style.RESET_ALL}")
                        except Exception as e:
                            logger.error(f"{Fore.RED}✗ Connection ping failed: {e}{Style.RESET_ALL}")
                            # Connection is dead, break out of the loop to reconnect
                            break

        except ConnectionClosed as e:
            self.connected = False
            if isinstance(e, ConnectionClosedOK):
                logger.info(f"{Fore.YELLOW}WebSocket closed gracefully{Style.RESET_ALL}")
            else:
                self.connection_errors += 1
                self.last_error = str(e)
                logger.error(f"{Fore.RED}✗ WebSocket connection closed: {e}{Style.RESET_ALL}")

        except Exception as e:
            self.connected = False
            self.connection_errors += 1
            self.last_error = str(e)
            logger.error(f"{Fore.RED}✗ Error processing messages: {e}{Style.RESET_ALL}")

    async def _ping_loop(self):
        """Send periodic pings to keep the connection alive."""
        try:
            # Use the safer check for connection state
            while self.running and self.websocket and self._is_websocket_open():
                await asyncio.sleep(self.ping_interval)
                # Check again as it may have changed during the sleep
                if self.websocket and self._is_websocket_open():
                    try:
                        pong_waiter = await self.websocket.ping()
                        await asyncio.wait_for(pong_waiter, timeout=5)
                    except Exception:
                        # If ping fails, the connection is probably dead
                        # The message processing loop will detect this
                        pass
        except asyncio.CancelledError:
            # Task was cancelled
            pass
        except Exception as e:
            logger.error(f"Error in ping loop: {e}")

    def _is_websocket_open(self):
        """
        Safely check if the websocket connection is open.

        This handles different versions of the websockets library that might
        use different attributes (.open, .closed, etc.)
        """
        if not self.websocket:
            return False

        # Try different attributes that might indicate connection state
        try:
            # First try the standard closed property which most versions have
            if hasattr(self.websocket, 'closed'):
                return not self.websocket.closed

            # Some versions use .open
            if hasattr(self.websocket, 'open'):
                return self.websocket.open

            # Last resort - if we can't determine, assume it's open if we have a websocket object
            # This is risky but better than breaking
            return True
        except Exception:
            # Any error means something's wrong with the connection
            return False

    async def _close_connection(self):
        """Close the WebSocket connection gracefully."""
        if self.websocket:
            try:
                if hasattr(self.websocket, 'close') and callable(self.websocket.close):
                    await self.websocket.close(code=1000, reason="Client closing connection")
            except Exception as e:
                logger.debug(f"Error closing connection: {e}")
            finally:
                self.websocket = None
                self.connected = False

    async def send(self, data: dict) -> bool:
        """
        Send a JSON message to the WebSocket server.

        Args:
            data: Dictionary to be serialized and sent

        Returns:
            True if the message was sent, False otherwise
        """
        if not self.connected or not self.websocket:
            logger.error(f"{Fore.RED}✗ Cannot send message: not connected{Style.RESET_ALL}")
            return False

        try:
            await self.websocket.send(json.dumps(data))
            return True
        except Exception as e:
            logger.error(f"{Fore.RED}✗ Error sending message: {e}{Style.RESET_ALL}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Get connection statistics."""
        stats = {
            "connected": self.connected,
            "running": self.running,
            "messages_received": self.messages_received,
            "successful_connections": self.successful_connections,
            "connection_errors": self.connection_errors,
            "last_error": self.last_error,
            "reconnect_attempt": self.reconnect_attempt,
        }

        # Add uptime if connected
        if self.connect_time:
            if self.connected:
                uptime = time.time() - self.connect_time
                stats["uptime_seconds"] = uptime
            elif self.disconnect_time:
                downtime = time.time() - self.disconnect_time
                stats["downtime_seconds"] = downtime

        return stats