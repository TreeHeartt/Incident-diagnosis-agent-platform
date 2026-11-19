"""Compatibility entry point. All execution uses the durable real Agent worker."""
from .api import app
from .database import initialize

if __name__ == '__main__':
    import asyncio
    import os
    from .worker import main
    if os.name == 'nt': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
