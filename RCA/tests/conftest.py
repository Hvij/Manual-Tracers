import os

# Tests must never depend on, or send data to, real external services — regardless of what
# the real .env has configured for local dev. Force these empty before any app module runs
# its import-time load_dotenv(), since dotenv only fills in keys that are ABSENT from
# os.environ, never ones already set (even to "").
for _key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "GEMINI_API_KEY"):
    os.environ[_key] = ""
