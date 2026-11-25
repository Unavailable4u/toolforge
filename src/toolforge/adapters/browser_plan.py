"""Browser plan DSL and validator.

A browser plan is a declarative JSON specification of web interactions that MiniLocker's
BrowserSandbox executes. The plan vocabulary is closed; any step not listed here is rejected.

Vocabulary:
  goto(url)           - navigate to URL (must be in approved hosts list)
  click(selector)     - click element matching CSS selector
  type(selector, text) - type text into input element
  extract_text(selector, field) - extract text content to output field
  extract_attr(selector, attr, field) - extract attribute value to output field
  wait_for(selector, timeout_ms) - wait for element to appear
  screenshot(name)    - take screenshot (saved to workspace)

Plans may contain {{arg}} templates that are replaced with tool input values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

# JSON Schema for browser plans
PLAN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "array",
    "items": {
        "type": "object",
        "oneOf": [
            {
                "properties": {
                    "step": {"const": "goto"},
                    "url": {"type": "string", "format": "uri"},
                },
                "required": ["step", "url"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "step": {"const": "click"},
                    "selector": {"type": "string"},
                },
                "required": ["step", "selector"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "step": {"const": "type"},
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["step", "selector", "text"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "step": {"const": "extract_text"},
                    "selector": {"type": "string"},
                    "field": {"type": "string", "pattern": "^[a-zA-Z_][a-zA-Z0-9_]*$"},
                },
                "required": ["step", "selector", "field"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "step": {"const": "extract_attr"},
                    "selector": {"type": "string"},
                    "attr": {"type": "string"},
                    "field": {"type": "string", "pattern": "^[a-zA-Z_][a-zA-Z0-9_]*$"},
                },
                "required": ["step", "selector", "attr", "field"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "step": {"const": "wait_for"},
                    "selector": {"type": "string"},
                    "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000},
                },
                "required": ["step", "selector"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "step": {"const": "screenshot"},
                    "name": {"type": "string", "pattern": "^[a-zA-Z0-9_-]+$"},
                },
                "required": ["step", "name"],
                "additionalProperties": False,
            },
        ],
    },
    "minItems": 1,
    "maxItems": 50,  # Maximum steps per plan
}

_validator = Draft202012Validator(PLAN_SCHEMA)

# Template pattern: {{arg_name}}
TEMPLATE_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


class BrowserPlanError(Exception):
    """Browser plan validation error."""


def validate_plan(plan: Sequence[dict[str, Any]], approved_hosts: list[str]) -> None:
    """Validate a browser plan against the DSL and approved hosts.
    
    Args:
        plan: The plan steps to validate
        approved_hosts: List of allowed URL hosts (e.g., ["example.com", "api.example.com"])
    
    Raises:
        BrowserPlanError: If the plan is invalid
    """
    try:
        _validator.validate(plan)
    except ValidationError as e:
        raise BrowserPlanError(f"Invalid plan schema: {e.message}") from e
    
    # Validate URLs against approved hosts
    for i, step in enumerate(plan):
        if step["step"] == "goto":
            url = step["url"]
            
            # Check for template injection in URL
            if TEMPLATE_PATTERN.search(url):
                raise BrowserPlanError(
                    f"Step {i}: URL cannot contain templates for security"
                )
            
            # Parse host from URL
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if not parsed.netloc:
                raise BrowserPlanError(f"Step {i}: Invalid URL: {url}")
            
            # Check if host is approved
            host = parsed.netloc.lower()
            approved = any(
                host == approved_host.lower() or host.endswith(f".{approved_host.lower()}")
                for approved_host in approved_hosts
            )
            if not approved:
                raise BrowserPlanError(
                    f"Step {i}: Host '{host}' not in approved list: {approved_hosts}"
                )
        
        # Validate template usage in text fields
        if step["step"] in ("type", "extract_text", "extract_attr"):
            text_field = "text" if step["step"] == "type" else "field"
            if text_field in step:
                value = step[text_field]
                if not isinstance(value, str):
                    continue
                
                # Check for malformed templates
                for match in re.finditer(r"{{\s*[^}]*\s*}}", value):
                    if not TEMPLATE_PATTERN.fullmatch(match.group(0)):
                        raise BrowserPlanError(
                            f"Step {i}: Invalid template syntax: {match.group(0)}"
                        )


def render_templates(plan: Sequence[dict[str, Any]], inputs: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace {{arg}} templates in plan with input values.
    
    Args:
        plan: The plan with templates
        inputs: Tool input values
        
    Returns:
        Rendered plan with templates replaced
    """
    rendered = []
    
    for step in plan:
        step_copy = step.copy()
        
        # Replace templates in relevant fields
        for field in ("text", "selector", "url", "name"):
            if field in step_copy and isinstance(step_copy[field], str):
                value = step_copy[field]
                
                def replace_template(match):
                    arg_name = match.group(1)
                    if arg_name not in inputs:
                        raise BrowserPlanError(f"Missing input for template: {arg_name}")
                    return str(inputs[arg_name])
                
                step_copy[field] = TEMPLATE_PATTERN.sub(replace_template, value)
        
        rendered.append(step_copy)
    
    return rendered


def extract_output_fields(plan: Sequence[dict[str, Any]]) -> set[str]:
    """Extract all output field names from a plan.
    
    Returns:
        Set of field names that will be extracted
    """
    fields = set()
    
    for step in plan:
        if step["step"] in ("extract_text", "extract_attr"):
            fields.add(step["field"])
    
    return fields


# Example plan for documentation
EXAMPLE_PLAN = [
    {
        "step": "goto",
        "url": "https://news.example.com",
    },
    {
        "step": "wait_for",
        "selector": ".headline",
        "timeout_ms": 5000,
    },
    {
        "step": "extract_text",
        "selector": ".headline:first-child",
        "field": "headline",
    },
    {
        "step": "extract_attr",
        "selector": ".headline:first-child a",
        "attr": "href",
        "field": "link",
    },
]