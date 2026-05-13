import logging
import time
from app.infrastructure.services.cache_warmup import run_startup_cache_warmup_sync

logging.basicConfig(level=logging.DEBUG)
print("Starting cache warmup...")
run_startup_cache_warmup_sync()
print("Sleeping for 15 seconds to allow daemon threads to finish...")
time.sleep(15)
print("Done. Check if files are created.")
