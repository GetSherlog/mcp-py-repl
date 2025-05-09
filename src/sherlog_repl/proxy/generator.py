"""
Tool Proxy Generator for the Sherlog REPL.
Dynamically creates Python function proxies for MCP tools.
"""

import asyncio
import functools
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Type, Union

from mcp.server.fastmcp import Context
import mcp.types as types

from ..connectors.base import MCPConnector

logger = logging.getLogger(__name__)


class ToolProxyGenerator:
    """
    Dynamically generates Python function proxies for MCP tools.
    These proxies can be called as regular Python functions but will
    route the call to the appropriate MCP connector.
    """
    
    def __init__(self):
        """Initialize the tool proxy generator."""
        self.proxies: Dict[str, Callable] = {}
        self.connectors: Dict[str, MCPConnector] = {}
        self.tool_info: Dict[str, Dict[str, Any]] = {}
    
    def register_connector(self, connector: MCPConnector):
        """
        Register an MCP connector.
        
        Args:
            connector: The connector to register.
        """
        self.connectors[connector.name] = connector
        logger.info(f"Registered connector: {connector.name}")
    
    async def discover_tools(self, connector_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Discover all tools from registered connectors.
        
        Args:
            connector_name: Optional name of a specific connector to discover tools from.
            
        Returns:
            A list of discovered tool information.
        """
        all_tools = []
        
        if connector_name:
            if connector_name not in self.connectors:
                raise ValueError(f"Connector not found: {connector_name}")
            connectors = {connector_name: self.connectors[connector_name]}
        else:
            connectors = self.connectors
        
        for name, connector in connectors.items():
            try:
                if not connector.initialized:
                    await connector.initialize()
                
                tools = await connector.list_tools()
                for tool in tools:
                    tool_id = tool.get('id')
                    if not tool_id:
                        logger.warning(f"Tool from {name} missing ID, skipping")
                        continue
                    
                    # Store full tool info for later reference
                    full_tool_id = f"{name}_{tool_id}"
                    self.tool_info[full_tool_id] = {
                        'connector': name,
                        'tool_id': tool_id,
                        **tool
                    }
                    all_tools.append(self.tool_info[full_tool_id])
            except Exception as e:
                logger.error(f"Error discovering tools from {name}: {e}")
        
        return all_tools
    
    def _generate_proxy_doc(self, tool_info: Dict[str, Any]) -> str:
        """
        Generate documentation for a tool proxy.
        
        Args:
            tool_info: Information about the tool.
            
        Returns:
            Documentation string for the proxy function.
        """
        description = tool_info.get('description', 'No description available')
        connector = tool_info.get('connector', 'unknown')
        
        # Add parameter info if available
        params_info = ""
        parameters = tool_info.get('parameters', {})
        if parameters:
            params_list = []
            for name, param in parameters.items():
                param_type = param.get('type', 'any')
                param_desc = param.get('description', '')
                required = name in parameters.get('required', [])
                params_list.append(f"    {name}: {param_type}{' (required)' if required else ''}\n        {param_desc}")
            
            if params_list:
                params_info = "Parameters:\n" + "\n".join(params_list)
        
        doc = f"{description}\n\nProvided by: {connector} MCP"
        if params_info:
            doc += f"\n\n{params_info}"
        
        return doc
    
    async def create_proxy(self, tool_info: Dict[str, Any]) -> Callable:
        """
        Create a proxy function for an MCP tool.
        
        Args:
            tool_info: Information about the tool.
            
        Returns:
            A proxy function that calls the MCP tool.
        """
        connector_name = tool_info['connector']
        tool_id = tool_info['tool_id']
        full_tool_id = f"{connector_name}_{tool_id}"
        
        # Get parameter information
        parameters = tool_info.get('parameters', {})
        required_params = parameters.get('required', [])
        
        # Create an async proxy function
        async def proxy_function(ctx: Context, **kwargs) -> Any:
            """Proxy function that calls the MCP tool."""
            connector = self.connectors.get(connector_name)
            if not connector:
                raise ValueError(f"Connector not found: {connector_name}")
            
            if not connector.initialized:
                await connector.initialize()
            
            # Check for required parameters
            missing_params = [p for p in required_params if p not in kwargs]
            if missing_params:
                raise ValueError(f"Missing required parameters: {missing_params}")
            
            # Execute the tool
            result = await connector.execute_tool(tool_id, kwargs)
            return result
        
        # Set function metadata
        proxy_function.__name__ = full_tool_id
        proxy_function.__qualname__ = full_tool_id
        proxy_function.__doc__ = self._generate_proxy_doc(tool_info)
        
        # Store the proxy function
        self.proxies[full_tool_id] = proxy_function
        
        return proxy_function
    
    async def generate_all_proxies(self, connector_name: Optional[str] = None) -> Dict[str, Callable]:
        """
        Generate proxy functions for all tools from registered connectors.
        
        Args:
            connector_name: Optional name of a specific connector to generate proxies for.
            
        Returns:
            A dictionary mapping tool IDs to proxy functions.
        """
        # First, discover all tools
        all_tools = await self.discover_tools(connector_name)
        
        # Create proxies for each tool
        for tool_info in all_tools:
            await self.create_proxy(tool_info)
        
        return self.proxies
    
    def get_proxy(self, full_tool_id: str) -> Optional[Callable]:
        """
        Get a proxy function by its full tool ID.
        
        Args:
            full_tool_id: The full ID of the tool (connector_name_tool_id).
            
        Returns:
            The proxy function if found, None otherwise.
        """
        return self.proxies.get(full_tool_id)
    
    def register_proxies_in_namespace(self, namespace: Dict[str, Any]) -> int:
        """
        Register all proxy functions in a namespace (e.g., a session's globals).
        
        Args:
            namespace: The namespace to register the proxies in.
            
        Returns:
            The number of proxies registered.
        """
        count = 0
        for full_tool_id, proxy in self.proxies.items():
            namespace[full_tool_id] = proxy
            count += 1
        return count