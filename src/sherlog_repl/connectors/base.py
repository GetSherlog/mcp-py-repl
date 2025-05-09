"""
Base connector module for MCP services.
Defines the interface that all MCP connectors should implement.
"""

import abc
from typing import Any, Dict, List, Optional


class MCPConnector(abc.ABC):
    """
    Abstract base class for MCP connectors.
    Each connector is responsible for interacting with a specific MCP service.
    """

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the MCP connector.
        
        Args:
            name: The name of the connector (e.g., "github", "filesystem", "jira").
            config: Optional configuration dictionary for the connector.
        """
        self.name = name
        self.config = config or {}
        self.initialized = False
        
    @abc.abstractmethod
    async def initialize(self) -> bool:
        """
        Initialize the connection to the MCP service.
        Should perform any necessary setup, like establishing connections,
        authentication, and validating configuration.
        
        Returns:
            True if initialization was successful, False otherwise.
        """
        pass
        
    @abc.abstractmethod
    async def list_tools(self) -> List[Dict[str, Any]]:
        """
        List all available tools from this MCP service.
        
        Returns:
            A list of tool definitions, where each tool is a dictionary with
            keys like 'id', 'name', 'description', 'parameters', etc.
        """
        pass
        
    @abc.abstractmethod
    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Any:
        """
        Execute a tool with the given parameters.
        
        Args:
            tool_id: The identifier of the tool to execute.
            params: A dictionary of parameters to pass to the tool.
            
        Returns:
            The result of the tool execution.
        """
        pass
        
    @abc.abstractmethod
    async def shutdown(self) -> None:
        """
        Clean up resources and shutdown the connector.
        """
        pass
        
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name}, initialized={self.initialized})"