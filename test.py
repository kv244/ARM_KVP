import asyncio
import sys
import logging
from kv_client import KVClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

async def run_requests(port: str):
    try:
        async with KVClient(port) as kv:
            log.info("--- Testing Automatic Key/Value Formatting ---")
            
            # Test 1: Short key and value (padding)
            # "hit" (3 chars) -> "hit\x00"
            # "small" (5 chars) -> "small\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            log.info("1. Creating with short key/value (padding)...")
            await kv.create("pkt", "msg")
            val = await kv.read("pkt")
            log.info(f"   Read 'pkt': {val}")

            # Test 2: Long key and value (truncation)
            # "very_long_key" -> "very"
            # "this_is_a_very_long_value" -> "this_is_a_very_l"
            log.info("2. Creating with long key/value (truncation)...")
            await kv.create("super_long_key", "this_is_a_very_long_value_for_testing")
            
            # We read back using the truncated key
            val = await kv.read("supe") 
            log.info(f"   Read back 'supe': {val}")

            # Test 3: Batch with mixed lengths
            log.info("3. Batch test with various lengths...")
            results = await kv.batch([
                ("create", "k1", "v1"),                # k1\x00\x00, v1\x00...
                ("read",   "super_long_key"),         # reads "supe"
                ("delete", "pkt")                      # deletes "pkt\x00"
            ])
            log.info(f"   Batch results summary: {results}")
            for i, res in enumerate(results):
                log.info(f"     Op {i}: {res}")

            # Test 4: Final verification
            log.info("4. Final verification...")
            v1 = await kv.read("k1")
            log.info(f"   Read 'k1': {v1}")
            
            help_text = await kv.get_help()
            log.info(f"   Device Help Check:\n{help_text[:50]}...")
    except Exception as e:
        log.error("Could not connect to %s: %s", port, e)
        log.error("Ensure no other application (Arduino IDE, Serial Monitor) is using the port.")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test.py <COM_PORT>")
        sys.exit(1)
    port = sys.argv[1]
    asyncio.run(run_requests(port))
