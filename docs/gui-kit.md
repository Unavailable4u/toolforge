# GUI Kit – Front‑end Components for ToolForge

This document describes the reusable UI components that present tool results to users
in the ToolForge web interface and CLI. Each recipe kind may have a corresponding
GUI component that knows how to render its output.

## Component Registry

The GUI kit maps recipe kinds to React components (for web) and formatters (for CLI).

```typescript
// frontend/src/components/GuiKitRegistry.tsx
import { SqlResults } from './recipes/SqlResults';
import { FileFormatResults } from './recipes/FileFormatResults';
import { HttpApiResults } from './recipes/HttpApiResults';
import { GameStateResults } from './recipes/GameStateResults';
import { BrowserPlanResults } from './recipes/BrowserPlanResults';

export const GUI_KIT_REGISTRY = {
  sql: SqlResults,
  'file-format': FileFormatResults,
  'http-api': HttpApiResults,
  'game-state': GameStateResults,
  'browser-plan': BrowserPlanResults,
};
```

## Recipe‑Specific Components

### `sql` – SQL Query Results

**Input**: `{"rows": [[...], ...], "columns": ["col1", ...]}`

**Web Component**: Interactive table with sorting, filtering, and pagination.

**CLI Formatter**:
```
╭─────────────────┬──────────────────┬───────────╮
│ id              │ name             │ salary    │
├─────────────────┼──────────────────┼───────────┤
│ 1               │ Alice            │ 85000.00  │
│ 2               │ Bob              │ 72000.00  │
│ 3               │ Charlie          │ 92000.00  │
╰─────────────────┴──────────────────┴───────────╯
3 rows returned
```

### `file-format` – File Parsing Results

**Input**: `{"valid": bool, "errors": [...], "parsed": {...}}`

**Web Component**: Syntax‑highlighted preview with error indicators.

**CLI Formatter**:
```
✓ Valid YAML (234 bytes)
├── version: 1.0.0
├── name: example-tool
└── tools: 3 items
```

### `http-api` – HTTP Response Results

**Input**: `{"status": 200, "headers": {...}, "body": {...}, "duration_ms": 123}`

**Web Component**: Expandable sections for headers, body, and timing.

**CLI Formatter**:
```
HTTP 200 OK (123 ms)
Content-Type: application/json
Content-Length: 2345

{
  "result": "success",
  "data": [...]
}
```

### `game-state` – Game State Visualization

**Input**: `{"state": "...", "legal_moves": [...], "score": 0.5}`

**Web Component**: Board visualization with move highlighting.

**CLI Formatter**:
```
♜ ♞ ♝ ♛ ♚ ♝ ♞ ♜
♟ ♟ ♟ ♟ ♟ ♟ ♟ ♟
· · · · · · · ·
· · · · · · · ·
· · · · ♙ · · ·
· · · · · · · ·
♙ ♙ ♙ ♙ · ♙ ♙ ♙
♖ ♘ ♗ ♕ ♔ ♗ ♘ ♖

Legal moves: e2-e4, d2-d4, g1-f3
Evaluation: +0.3
```

### `browser-plan` – Browser Automation Results

**Input**: `{"headline": "...", "link": "...", "screenshots": {...}}`

**Web Component**: Extracted data table with screenshot previews.

**CLI Formatter**:
```
📰 Headline: "Breaking News: Example Event"
🔗 Link: https://news.example.com/article/123
📸 Screenshots: page.png (1024x768)
```

## Component Props Interface

All GUI components receive the same base props:

```typescript
interface GuiComponentProps {
  // The tool result data
  result: Record<string, any>;
  
  // Tool metadata
  tool: {
    name: string;
    description: string;
    kind: string;
  };
  
  // Execution context
  context: {
    executionId: string;
    startedAt: string;
    durationMs: number;
  };
  
  // Callbacks
  onExpand?: () => void;
  onExport?: (format: 'json' | 'csv' | 'md') => void;
}
```

## CLI Formatters

CLI formatters are Python functions that take the same result data and return
a string suitable for terminal display.

```python
# src/toolforge/cli/formatters.py
def format_sql_result(result: dict, tool: dict) -> str:
    rows = result.get("rows", [])
    columns = result.get("columns", [])
    
    if not rows:
        return "No rows returned"
    
    # Use tabulate or similar for table formatting
    from tabulate import tabulate
    return tabulate(rows, headers=columns, tablefmt="rounded_grid")
```

## Adding a New Recipe Kind

To add a new recipe kind with GUI support:

1. **Define the result schema** in the recipe document
2. **Create React component** in `frontend/src/components/recipes/`
3. **Create CLI formatter** in `src/toolforge/cli/formatters.py`
4. **Register both** in their respective registries
5. **Add golden pack** with example output for testing

## Testing GUI Components

GUI components are tested with the golden packs:

```python
# tests/gui/test_gui_components.py
def test_sql_gui_component():
    """Test that SQL results render correctly."""
    from frontend.src.components.recipes.SqlResults import SqlResults
    
    result = {
        "rows": [[1, "Alice"], [2, "Bob"]],
        "columns": ["id", "name"],
    }
    
    component = SqlResults({ result })
    # Render and assert expected HTML structure
```

## Responsive Design

All GUI components must:
- Work on mobile (320px+) and desktop (1200px+)
- Use the project's design tokens (colors, spacing, typography)
- Support dark/light mode via CSS variables
- Be accessible (ARIA labels, keyboard navigation, screen‑reader friendly)

## Performance Considerations

- Lazy‑load large result sets (virtual scrolling for tables)
- Debounce interactive filters
- Cache rendered components when possible
- Use React.memo for pure components

## Versioning

The GUI kit follows semantic versioning:
- **Major changes**: Breaking changes to component interfaces
- **Minor changes**: New components or non‑breaking enhancements
- **Patch changes**: Bug fixes and performance improvements

Changes to the GUI kit must be reflected in:
1. This documentation
2. The golden pack test suite
3. The component registry types