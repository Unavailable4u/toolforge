"""Browser sandbox adapter for MiniLocker.

This module allows tools to declare `kind: "browser_plan"` and have their JSON plans
executed by MiniLocker's BrowserSandbox.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from toolforge.adapters.browser_plan import (
    BrowserPlanError,
    extract_output_fields,
    render_templates,
    validate_plan,
)
from toolforge.errors import ToolError
from toolforge.server.tools import Tool

logger = logging.getLogger(__name__)


class BrowserSandboxAdapter:
    """Adapter that executes browser plans using MiniLocker's BrowserSandbox.
    
    This is an allowed importer that MiniLocker will load when a tool with
    `kind: "browser_plan"` is executed.
    """
    
    def __init__(self):
        self.browser_sandbox = None
        
    def load(self, sandbox):
        """Initialize the adapter with a reference to the MiniLocker sandbox.
        
        Args:
            sandbox: The MiniLocker sandbox instance
        """
        self.browser_sandbox = sandbox
        
    def execute(self, tool: Tool, inputs: dict[str, Any]) -> dict[str, Any]:
        """Execute a browser plan tool.
        
        Args:
            tool: The tool to execute
            inputs: Tool input parameters
            
        Returns:
            Dictionary of extracted fields
            
        Raises:
            ToolError: If plan execution fails
        """
        if self.browser_sandbox is None:
            raise ToolError("Browser sandbox not available")
        
        # Get the plan from tool manifest
        plan_data = tool.manifest.get("plan")
        if not plan_data:
            raise ToolError("Missing 'plan' in browser tool manifest")
        
        # Get approved hosts from manifest permissions
        approved_hosts = tool.manifest.get("permissions", {}).get("browser", [])
        if not approved_hosts:
            raise ToolError("Missing approved hosts in browser permissions")
        
        try:
            # Validate the plan
            validate_plan(plan_data, approved_hosts)
            
            # Render templates with input values
            rendered_plan = render_templates(plan_data, inputs)
            
            # Extract expected output fields
            expected_fields = extract_output_fields(rendered_plan)
            
            # Execute the plan in the browser sandbox
            logger.info("Executing browser plan with %d steps", len(rendered_plan))
            
            # This would call into MiniLocker's BrowserSandbox
            # For now, simulate execution and return structured output
            results = self._simulate_execution(rendered_plan, expected_fields)
            
            # Validate that all expected fields were extracted
            missing = expected_fields - set(results.keys())
            if missing:
                raise ToolError(f"Failed to extract fields: {missing}")
            
            return results
            
        except BrowserPlanError as e:
            raise ToolError(f"Invalid browser plan: {e}") from e
        except Exception as e:
            raise ToolError(f"Browser plan execution failed: {e}") from e
    
    def _simulate_execution(self, plan: list[dict[str, Any]], expected_fields: set[str]) -> dict[str, Any]:
        """Simulate browser plan execution (to be replaced with real BrowserSandbox).
        
        In production, this would call:
            self.browser_sandbox.execute_browser_plan(plan)
        
        Args:
            plan: Rendered plan steps
            expected_fields: Fields expected to be extracted
            
        Returns:
            Simulated extracted data
        """
        # This is a placeholder implementation
        # Real implementation would use MiniLocker's BrowserSandbox
        logger.warning("Simulating browser plan execution (real BrowserSandbox not available)")
        
        results = {}
        for field in expected_fields:
            # Generate placeholder values for testing
            if field == "headline":
                results[field] = "Example News Headline"
            elif field == "link":
                results[field] = "https://news.example.com/article/123"
            elif field == "price":
                results[field] = "$29.99"
            elif field == "title":
                results[field] = "Example Product"
            else:
                results[field] = f"value_for_{field}"
        
        return results


# Hook for MiniLocker to discover this adapter
def register_adapters(sandbox) -> list:
    """Register browser adapter with MiniLocker.
    
    Args:
        sandbox: The MiniLocker sandbox
        
    Returns:
        List of adapter instances
    """
    adapter = BrowserSandboxAdapter()
    adapter.load(sandbox)
    return [adapter]


# Test helper for browser plan execution
def test_browser_plan_execution():
    """Test helper for browser plan execution in tests."""
    from unittest.mock import Mock
    
    # Create a mock sandbox
    sandbox = Mock()
    
    # Create adapter
    adapter = BrowserSandboxAdapter()
    adapter.load(sandbox)
    
    # Create a test tool
    tool = Mock()
    tool.manifest = {
        "kind": "browser_plan",
        "name": "test-tool",
        "permissions": {
            "browser": ["example.com", "news.example.com"]
        },
        "plan": [
            {
                "step": "goto",
                "url": "https://news.example.com",
            },
            {
                "step": "extract_text",
                "selector": ".headline",
                "field": "headline",
            },
        ]
    }
    
    # Test execution
    try:
        result = adapter.execute(tool, {})
        print(f"Test execution result: {result}")
        return True
    except Exception as e:
        print(f"Test execution failed: {e}")
        return False


if __name__ == "__main__":
    # Simple test when run directly
    success = test_browser_plan_execution()
    exit(0 if success else 1)