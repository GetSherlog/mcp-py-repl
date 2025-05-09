"""
Proxy package for the Sherlog REPL.
Generates Python proxies for MCP tools.
"""

from .generator import ToolProxyGenerator

__all__ = [
    'ToolProxyGenerator',
]