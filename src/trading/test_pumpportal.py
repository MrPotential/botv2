import asyncio
import json
import websockets

async def test_pumpportal():
    uri = "wss://pumpportal.fun/api/data"
    print(f"Connecting to {uri}...")
    
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected! Subscribing to new token events...")
            
            # Subscribe to token creation events
            payload = {
                "method": "subscribeNewToken",
            }
            await websocket.send(json.dumps(payload))
            
            print("Subscription request sent. Waiting for events...")
            print("Press Ctrl+C to exit")
            
            # Just listen for messages
            while True:
                message = await websocket.recv()
                print(f"\nReceived message: {message[:200]}...\n")
                
    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        print(f"Error: {e}")

# Run the test
if __name__ == "__main__":
    asyncio.run(test_pumpportal())
