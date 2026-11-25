"""Recipe kind registry.

All recipe kinds available in ToolForge. Each kind must have:
- A recipe document in adapters/recipes/<kind>.md
- A corresponding adapter in sandbox/ or the MiniLocker core
- Tests for at least one golden pack using the kind
"""

# All recipe kinds supported by ToolForge
RECIPE_KINDS = [
    # SQL queries (sqlite3/DuckDB)
    "sql",
    
    # File format parsing (CSV, JSON, YAML, XML, etc.)
    "file-format",
    
    # HTTP API calls
    "http-api",
    
    # Game state manipulation (chess, blackjack, frozenlake)
    "game-state",
    
    # Browser automation plans
    "browser-plan",
    
    # Future P14b recipe kinds:
    # "text-transform",
    # "image-process",
    # "audio-process",
    # "data-transform",
]

# Mapping of recipe kind to adapter class
# (Adapter names are discovered dynamically via MiniLocker's adapter discovery)
ADAPTER_MAPPING = {
    "sql": "toolforge.adapters.sql_adapter.SqlAdapter",
    "file-format": "toolforge.adapters.file_format_adapter.FileFormatAdapter",
    "http-api": "toolforge.adapters.http_adapter.HttpAdapter",
    "game-state": "toolforge.adapters.game_adapter.GameAdapter",
    "browser-plan": "sandbox.browser_adapter.BrowserSandboxAdapter",
}

# Runtime image required for each recipe kind
# If None, uses the default runtime image
RUNTIME_IMAGE_REQUIREMENTS = {
    "sql": "toolforge-runtime:py312",  # Requires duckdb, sqlite3
    "file-format": "toolforge-runtime:py312",  # Requires pyyaml, lxml, beautifulsoup4
    "http-api": "toolforge-runtime:py312",  # Requires requests
    "game-state": "toolforge-runtime:py312",  # Requires chess, gymnasium
    "browser-plan": "toolforge-runtime:py312",  # Requires all for general use
}

def validate_recipe_kind(kind: str) -> bool:
    """Check if a recipe kind is valid/supported.
    
    Args:
        kind: Recipe kind string
        
    Returns:
        True if valid, False otherwise
    """
    return kind in RECIPE_KINDS


def get_adapter_class(kind: str) -> str:
    """Get the adapter class name for a recipe kind.
    
    Args:
        kind: Recipe kind string
        
    Returns:
        Adapter class name as string
        
    Raises:
        ValueError: If kind is not valid
    """
    if not validate_recipe_kind(kind):
        raise ValueError(f"Invalid recipe kind: {kind}")
    
    return ADAPTER_MAPPING.get(kind, "")


def get_required_runtime_image(kind: str) -> str:
    """Get the required runtime image for a recipe kind.
    
    Args:
        kind: Recipe kind string
        
    Returns:
        Runtime image name, defaults to 'toolforge-runtime:py312'
    """
    if not validate_recipe_kind(kind):
        raise ValueError(f"Invalid recipe kind: {kind}")
    
    return RUNTIME_IMAGE_REQUIREMENTS.get(kind, "toolforge-runtime:py312")


def list_kinds() -> list[dict[str, str]]:
    """List all recipe kinds with descriptions.
    
    Returns:
        List of dicts with 'kind', 'description', 'adapter', 'runtime_image'
    """
    descriptions = {
        "sql": "Read-only SQL queries on SQLite/DuckDB databases",
        "file-format": "Parse and validate structured file formats (CSV, JSON, YAML, XML)",
        "http-api": "Make HTTP requests to APIs with rate limiting and validation",
        "game-state": "Manipulate game states (chess, blackjack, FrozenLake)",
        "browser-plan": "Execute declarative browser automation plans",
    }
    
    return [
        {
            "kind": kind,
            "description": descriptions.get(kind, "No description available"),
            "adapter": get_adapter_class(kind),
            "runtime_image": get_required_runtime_image(kind),
        }
        for kind in RECIPE_KINDS
    ]