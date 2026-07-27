import os
from pathlib import Path
from dotenv import load_dotenv

print("dotenv found:", load_dotenv(Path(__file__).parent / ".env"))
key = os.getenv("ANTHROPIC_API_KEY")
print("key loaded:", bool(key), "| length:", len(key) if key else 0)