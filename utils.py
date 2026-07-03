"""
Environment and configuration utilities.

Loads secrets from .env file and validates that all required env vars are present.
"""

import os
import sys
from pathlib import Path


# Required env vars per module
TMOBILE_ENV_VARS = ["TMOBILE_PASSWORD"]
GEMINI_ENV_VARS = ["GEMINI_API_KEY"]
SPLITWISE_ENV_VARS = ["SPLITWISE_CONSUMER_KEY", "SPLITWISE_CONSUMER_SECRET"]
SPLITWISE_OPTIONAL_VARS = ["SPLITWISE_API_KEY"]

ALL_ENV_VARS = TMOBILE_ENV_VARS + GEMINI_ENV_VARS + SPLITWISE_ENV_VARS


def load_env():
    """
    Load the .env file. Exits with an error if the file is not found.
    Should be called once at the start of the program.
    """
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        print(f"Error: .env file not found at {env_path}")
        print("Please copy .env.example to .env and fill in your credentials.")
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key and _ == "=":
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_env_vars(var_names: list[str]):
    """
    Validate that all specified env vars are set and non-empty.
    Exits with a clear error listing any missing vars.
    """
    missing = [v for v in var_names if not os.environ.get(v)]
    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}")
        print("Please set them in your .env file (see .env.example).")
        sys.exit(1)


def get_env(var_name: str) -> str:
    """Get a required env var, raising an error if not set."""
    value = os.environ.get(var_name)
    if not value:
        print(f"Error: Environment variable {var_name} is not set.")
        print("Please set it in your .env file (see .env.example).")
        sys.exit(1)
    return value


def init_env(var_names: list[str] = None):
    """
    Load .env and validate required vars in one call.
    If var_names is None, validates ALL_ENV_VARS.
    """
    load_env()
    require_env_vars(var_names or ALL_ENV_VARS)


def schema_to_example(schema: dict) -> dict:
    """Generate an example JSON object from a JSON schema."""
    if schema.get("type") == "object":
        example = {}
        for key, prop in schema.get("properties", {}).items():
            example[key] = schema_to_example(prop)
        return example
    elif schema.get("type") == "array":
        return [schema_to_example(schema.get("items", {}))]
    elif schema.get("type") == "number":
        return 0.00
    elif schema.get("type") == "string":
        return schema.get("description", "string")
    return None
