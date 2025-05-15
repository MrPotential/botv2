"""
Solana client abstraction for blockchain operations.
"""

import asyncio
import json
from typing import Any

import aiohttp
import websockets
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from utils.logger import get_logger

logger = get_logger(__name__)


class SolanaClient:
    """Abstraction for Solana RPC client operations."""

    def __init__(self, rpc_endpoint: str):
        self.rpc_endpoint = rpc_endpoint
        self._client: AsyncClient | None = None
        self._cached_blockhash: Hash | None = None
        self._blockhash_last_updated = 0.0
        self._blockhash_lock = asyncio.Lock()
        self._blockhash_updater_task = asyncio.create_task(self.start_blockhash_updater())
        self._id_counter = 1  # For custom RPC requests

    async def start_blockhash_updater(self, interval: float = 5.0):
        while True:
            try:
                hb = await self.get_latest_blockhash()
                async with self._blockhash_lock:
                    self._cached_blockhash = hb
                    self._blockhash_last_updated = asyncio.get_event_loop().time()
            except Exception as e:
                logger.debug(f"Blockhash fetch failed: {e}")
            await asyncio.sleep(interval)

    async def get_cached_blockhash(self) -> Hash:
        async with self._blockhash_lock:
            now = asyncio.get_event_loop().time()
            if (
                self._cached_blockhash is None
                or (now - self._blockhash_last_updated) > 10.0
            ):
                try:
                    self._cached_blockhash = await self.get_latest_blockhash()
                    self._blockhash_last_updated = now
                except Exception as e:
                    if self._cached_blockhash is None:
                        raise RuntimeError(f"Failed to fetch blockhash: {e}")
                    logger.warning(f"Using stale blockhash after error: {e}")
            return self._cached_blockhash  # type: ignore

    async def get_client(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(self.rpc_endpoint)
        return self._client

    async def close(self):
        if self._blockhash_updater_task:
            self._blockhash_updater_task.cancel()
            try:
                await self._blockhash_updater_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.close()
            self._client = None

    async def get_health(self) -> str | None:
        body = {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
        result = await self.post_rpc(body)
        return result.get("result") if result else None

    async def get_account_info(self, pubkey: Pubkey) -> dict[str, Any] | None:
        client = await self.get_client()
        resp = await client.get_account_info(pubkey, encoding="base64")
        return resp.value or None

    async def get_multiple_accounts(
            self,
            pubkeys: list[Pubkey],
            encoding: str = "base64"
    ) -> Any:
        """
        Batch‐fetch many accounts in one RPC call.
        """
        client = await self.get_client()
        return await client.get_multiple_accounts(pubkeys, encoding=encoding)


    async def get_token_account_balance(self, token_account: Pubkey) -> int:
        client = await self.get_client()
        try:
            resp = await client.get_token_account_balance(token_account)
            return int(resp.value.amount) if resp.value else 0
        except Exception as e:
            logger.error(f"Failed to fetch token balance for {token_account}: {e}")
            return 0

    async def get_latest_blockhash(self) -> Hash:
        client = await self.get_client()
        resp = await client.get_latest_blockhash(commitment="processed")
        return resp.value.blockhash

    async def build_and_send_transaction(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        skip_preflight: bool = True,
        max_retries: int = 3,
        priority_fee: int | None = None,
    ) -> str:
        client = await self.get_client()
        if priority_fee is not None:
            logger.info(f"Priority fee in microlamports: {priority_fee}")
            fee_instr = [
                set_compute_unit_limit(100_000),
                set_compute_unit_price(priority_fee),
            ]
            instructions = fee_instr + instructions

        for attempt in range(max_retries):
            try:
                recent_blockhash = await self.get_cached_blockhash()
                message = Message(instructions, signer_keypair.pubkey())
                tx = Transaction([signer_keypair], message, recent_blockhash)
                opts = TxOpts(skip_preflight=skip_preflight, preflight_commitment=Processed)
                logger.debug(f"Sending tx attempt {attempt+1}/{max_retries}")
                resp = await client.send_transaction(tx, opts)
                if getattr(resp, "error", None):
                    err = resp.error.get("message", "Unknown")
                    logger.error(f"Transaction error: {err}")
                    raise RuntimeError(err)
                sig = resp.value
                logger.info(f"Transaction sent: {sig}")
                return sig
            except Exception as e:
                em = str(e)
                # Try to extract embedded JSON error
                if e.args and isinstance(e.args[0], str) and "{" in e.args[0]:
                    try:
                        jd = json.loads(e.args[0][e.args[0].find("{"):])
                        em = jd.get("error", {}).get("message", em)
                    except:
                        pass
                if attempt == max_retries - 1:
                    logger.error(f"All retries failed: {em}")
                    raise RuntimeError(em)
                wait = 2**attempt
                logger.warning(f"Attempt {attempt+1} failed: {em}; retrying in {wait}s")
                await asyncio.sleep(wait)

    async def confirm_transaction(
        self, signature: str, commitment: str = "confirmed", timeout: int = 60
    ) -> bool:
        """
        First try WebSocket signatureSubscribe; if that fails or times out,
        fall back to RPC confirm_transaction polling.
        """
        # build WS URL
        ws_url = self.rpc_endpoint.replace("https://", "wss://").replace("http://", "ws://")
        sub = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "signatureSubscribe",
            "params": [signature, {"commitment": commitment}],
        }

        try:
            async with websockets.connect(ws_url, ping_interval=None) as ws:
                await ws.send(json.dumps(sub))
                start = asyncio.get_event_loop().time()
                while True:
                    if asyncio.get_event_loop().time() - start > timeout:
                        logger.error(f"WS signatureSubscribe timed out for {signature}")
                        break
                    msg = json.loads(await ws.recv())
                    # look for the notification
                    if msg.get("method") == "signatureNotification":
                        res = msg["params"]["result"]
                        if res.get("err") is None:
                            logger.info(f"Transaction {signature} confirmed via WS")
                            return True
                        logger.error(f"Transaction {signature} failed on-chain: {res['err']}")
                        return False
        except Exception as e:
            logger.warning(f"WS confirm_transaction error: {e!s}")

        # fallback to RPC polling
        logger.info(f"Falling back to RPC confirm_transaction for {signature}")
        client = await self.get_client()
        try:
            await asyncio.wait_for(
                client.confirm_transaction(signature, commitment=commitment, sleep_seconds=1),
                timeout=timeout,
            )
            logger.info(f"Transaction {signature} confirmed via RPC")
            return True
        except Exception as e:
            logger.error(f"RPC confirm_transaction failed: {e!s}")
            return False

    async def post_rpc(self, body: dict[str, Any]) -> dict[str, Any] | None:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    self.rpc_endpoint,
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    resp.raise_for_status()
                    out = await resp.json()
                    if "error" in out:
                        logger.error(f"RPC error: {out['error'].get('message')}")
                    return out
        except Exception as e:
            logger.error(f"Unexpected RPC error: {e!s}")
            return None

    async def get_program_accounts(
        self, program_id: str, encoding="base64", filters=None, commitment=None
    ) -> Any:
        req = {
            "jsonrpc": "2.0",
            "id": self._id_counter,
            "method": "getProgramAccounts",
            "params": [{"programId": program_id, "encoding": encoding}],
        }
        if filters:
            req["params"][0]["filters"] = filters
        if commitment:
            req["params"][0]["commitment"] = commitment
        self._id_counter += 1
        return await self.post_rpc(req)
