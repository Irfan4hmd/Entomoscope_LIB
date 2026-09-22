"""
Helper module for safely cleaning up multiprocessing resources.
"""
import gc
import logging
import os
import signal
import sys
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

def safe_shutdown_pool(pool: Any, wait: bool = True, timeout: int = 3) -> None:
    """
    Safely shut down a process pool with proper resource cleanup.
    
    Args:
        pool: The process pool to shut down (typically a ProcessPoolExecutor).
        wait: Whether to wait for completion of pending work.
        timeout: Maximum time to wait for pool shutdown in seconds.
    """
    if pool is None:
        return
    
    try:
        logger.info("Shutting down process pool...")
        pool.shutdown(wait=wait, cancel_futures=True)
        
        # Force garbage collection to help release resources
        gc.collect()
        
        # On Windows, sometimes resources aren't released properly
        # even after pool shutdown. This is a last resort cleanup.
        if sys.platform == 'win32' and hasattr(pool, '_processes') and pool._processes:
            for pid in pool._processes:
                try:
                    # Try to terminate any lingering processes
                    if pid is not None:
                        os.kill(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    # Process already gone, which is what we want
                    pass
    except Exception as e:
        logger.error(f"Error during pool shutdown: {e}")
    finally:
        # Make sure references are cleaned up
        del pool
        gc.collect()
