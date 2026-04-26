"""Allow running as: uv run python -m chapters.ch07_pub_sub.publisher"""
from .publisher import main
import asyncio

asyncio.run(main())
