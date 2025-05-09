"""
GitHub MCP connector for the Sherlog REPL.
Provides tools for interacting with GitHub repositories.
"""

import asyncio
import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, AsyncIterator, AsyncContextManager

from mcp import ClientSession, StdioServerParameters, types as mcp_types
from mcp.client.stdio import stdio_client

from .base import MCPConnector

logger = logging.getLogger(__name__)


class GitHubConnector(MCPConnector):
    """
    Connector for GitHub MCP.
    Uses the MCP Python SDK client to communicate with the github-mcp-server.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the GitHub connector.
        
        Args:
            config: Configuration for the connector.
                - container_image: Docker image for github-mcp-server (default: ghcr.io/github/github-mcp-server)
                - github_token: GitHub API token (or set via GITHUB_TOKEN env var)
        """
        super().__init__("github", config)
        self.container_image = self.config.get("container_image", "ghcr.io/github/github-mcp-server")
        self.github_token = self.config.get("github_token") or os.environ.get("GITHUB_TOKEN")
        
        self._client_session: Optional[ClientSession] = None
        self._stdio_client_cm: Optional[AsyncContextManager] = None # To hold the context manager
        self._server_params: Optional[StdioServerParameters] = None

        # Cached tool information
        self.tools: List[Dict[str, Any]] = [] # Keep as dicts for now for sherlog compatibility

        # Internal flag to avoid recursive initialise -> list_tools -> initialise loops
        self._initializing: bool = False
    
    async def _get_client_session(self) -> ClientSession:
        """Initializes and returns the MCP ClientSession."""
        if self._client_session: # Check if session object exists
            # If it exists, assume it was initialized by a previous call within this connector's lifetime.
            # The mcp-sdk's ClientSession doesn't have a public 'is_initialized' attribute.
            # Its usability is determined by successful completion of 'await session.initialize()'.
            return self._client_session

        if not self.github_token:
            logger.warning("GitHub token not provided. GitHub tools will not be available.")
            raise RuntimeError("GitHub token not provided.")

        # Check if Docker is available
        try:
            docker_check_process = await asyncio.create_subprocess_exec(
                "docker", "version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr_bytes = await docker_check_process.communicate()
            if docker_check_process.returncode != 0:
                err_msg = f"Docker is not available or docker command failed: {stderr_bytes.decode(errors='ignore')}"
                logger.error(err_msg)
                raise RuntimeError(err_msg)
        except FileNotFoundError:
            logger.error("Docker command not found. GitHub MCP connector requires Docker.")
            raise RuntimeError("Docker command not found.")
        except Exception as e:
            logger.error(f"Error checking Docker status: {e}")
            raise RuntimeError(f"Error checking Docker status: {e}")

        self._server_params = StdioServerParameters(
            command="docker",
            args=[
                "run", "-i", "--rm",
                "-e", f"GITHUB_PERSONAL_ACCESS_TOKEN={self.github_token}",
                self.container_image
            ],
            # env={} # Not needed as token is passed via -e
        )
        
        logger.info(f"Starting GitHub MCP server with command: docker run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN=**** {self.container_image}")

        try:
            # Enter the stdio_client context manager
            # We store the context manager itself to be able to exit it later in shutdown
            self._stdio_client_cm = stdio_client(self._server_params)
            read_stream, write_stream = await self._stdio_client_cm.__aenter__()

            self._client_session = ClientSession(read_stream, write_stream)
            await self._client_session.__aenter__() # Enter the ClientSession CM

            logger.info("Attempting to initialize ClientSession with GitHub MCP server...")
            await self._client_session.initialize()
            logger.info("ClientSession with GitHub MCP server initialized successfully.")
            
            # Small delay to ensure server is fully ready after initialize
            await asyncio.sleep(1)

            return self._client_session
        except Exception as e:
            logger.error(f"Failed to establish ClientSession with GitHub MCP server: {e}")
            logger.debug(traceback.format_exc())
            # Ensure cleanup if session partially started
            if self._client_session: # if session was created
                await self._client_session.__aexit__(None, None, None)
                self._client_session = None
            if self._stdio_client_cm:
                await self._stdio_client_cm.__aexit__(None, None, None)
                self._stdio_client_cm = None
            raise RuntimeError(f"Failed to initialize GitHub MCP client session: {e}")

    async def initialize(self) -> bool:
        """
        Initialize the GitHub MCP connector by establishing a client session.
        Returns: True if initialization was successful, False otherwise.
        """
        if self.initialized:
            return True
        
        if self._initializing:
            return False

        self._initializing = True

        if not self.github_token:  # Early exit if no token
            logger.warning("GitHub token not provided. GitHub connector cannot initialize.")
            self._initializing = False  # Reset flag before returning
            return False

        try:
            # _get_client_session will raise an error if it fails to init the session.
            session = await self._get_client_session() 
            # If _get_client_session completed, session.initialize() within it was successful.
            
            raw_tools = await self.list_tools(force_refresh=True)
            if not raw_tools:
                logger.warning("GitHub MCP connector initialized, but no tools were found.")
            
            self.initialized = True
            logger.info(f"GitHub MCP connector initialized. Found {len(raw_tools)} tools.")
            return True
        except Exception as e:
            logger.error(f"Error initializing GitHub MCP connector: {e}")
            logger.debug(traceback.format_exc())
            return False
        finally:
            self._initializing = False

    async def list_tools(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        List all available tools from the GitHub MCP server.
        
        Args:
            force_refresh: If True, bypass the cache and fetch from the server.

        Returns:
            A list of tool definitions as dictionaries.
        """
        if not force_refresh and self.tools:
            return self.tools
        
        if (not self.initialized) and (not self._initializing):
             if not await self.initialize():
                  logger.warning("Cannot list tools: GitHub connector not initialized.")
                  return []

        try:
            session = await self._get_client_session()
            logger.info("Fetching tools from GitHub MCP server via ClientSession...")
            # session.list_tools() returns a ListToolsResult object
            list_tools_result: mcp_types.ListToolsResult = await session.list_tools()
            
            logger.info(f"Raw ListToolsResult received from GitHub MCP server: {list_tools_result}")

            self.tools = []
            # The actual tool definitions are in the .tools attribute of ListToolsResult
            actual_tool_definitions = list_tools_result.tools

            if isinstance(actual_tool_definitions, list):
                for tool_def_item in actual_tool_definitions:
                    if isinstance(tool_def_item, mcp_types.Tool):
                        # Some MCP servers may not include an explicit `id` field on the Tool object.
                        tool_id_val = getattr(tool_def_item, "id", None) or getattr(tool_def_item, "name", None)
                        if not tool_id_val:
                            logger.warning(f"Tool definition missing both 'id' and 'name': {tool_def_item}. Skipping.")
                            continue

                        # Convert mcp_types.Tool to dict for sherlog compatibility
                        self.tools.append({
                            "id": tool_id_val,
                            "tool_id": tool_id_val, 
                            "name": tool_def_item.name, 
                            "description": tool_def_item.description,
                            # MCP v2024 Tool objects expose `inputSchema`; older SDKs expose `parameters`/`returns`.
                            "parameters": (
                                tool_def_item.inputSchema  # type: ignore[attr-defined]
                                if hasattr(tool_def_item, "inputSchema") else (
                                    tool_def_item.parameters.model_dump(mode="json")  # type: ignore[attr-defined]
                                    if hasattr(tool_def_item, "parameters") and tool_def_item.parameters is not None
                                    else {}
                                )
                            ),
                            "returns": (
                                tool_def_item.returns.model_dump(mode="json")  # type: ignore[attr-defined]
                                if hasattr(tool_def_item, "returns") and tool_def_item.returns is not None
                                else {}
                            ),
                            "connector": self.name
                        })
                    else:
                        logger.warning(f"Skipping unexpected item type in tool list from GitHub: {type(tool_def_item)}, value: {tool_def_item}")
            elif actual_tool_definitions is None:
                logger.info("GitHub MCP server returned no tools (tools attribute is None).")
            else:
                logger.error(f"Unexpected type for actual_tool_definitions from GitHub: {type(actual_tool_definitions)}, value: {actual_tool_definitions}")

            logger.info(f"Successfully processed {len(self.tools)} tools from GitHub MCP server.")
            return self.tools
        except Exception as e:
            logger.error(f"Error listing GitHub MCP tools via ClientSession: {e}")
            logger.debug(traceback.format_exc())
            # Invalidate session on error?
            # await self.shutdown() # Or a more targeted reset
            return []
    
    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Any:
        """
        Execute a GitHub MCP tool using the ClientSession.
        
        Args:
            tool_id: The ID of the tool to execute.
            params: Parameters to pass to the tool.
            
        Returns:
            The result of the tool execution.
        """
        if not self.initialized and not await self.initialize():
            raise RuntimeError("GitHub MCP connector not initialized, cannot execute tool.")

        try:
            session = await self._get_client_session()
            logger.info(f"Executing tool '{tool_id}' with params: {params} via GitHub ClientSession")
            
            # The MCP ClientSession.call_tool expects the tool's 'name' (which is tool_def.id from list_tools)
            result = await session.call_tool(name=tool_id, arguments=params)

            logger.info(f"Tool '{tool_id}' executed successfully via GitHub ClientSession.")
            return result
            
        except Exception as e:
            logger.error(f"Error executing GitHub MCP tool {tool_id} via ClientSession: {e}")
            logger.debug(traceback.format_exc())
            # Consider invalidating session on some errors
            # await self.shutdown()
            raise RuntimeError(f"Error executing GitHub tool {tool_id}: {e}")
    
    async def shutdown(self) -> None:
        """
        Shutdown the GitHub MCP connector and close the ClientSession.
        """
        logger.info("Shutting down GitHub MCP connector...")
        if self._client_session:
            try:
                logger.info("Closing ClientSession with GitHub MCP server...")
                await self._client_session.__aexit__(None, None, None)
                logger.info("ClientSession closed.")
            except Exception as e:
                logger.error(f"Error closing ClientSession: {e}")
            finally:
                self._client_session = None
        
        if self._stdio_client_cm:
            try:
                logger.info("Exiting stdio_client context for GitHub MCP server...")
                await self._stdio_client_cm.__aexit__(None, None, None) # Properly exit the stdio_client CM
                logger.info("stdio_client context exited.")
            except RuntimeError as e:
                if "Attempted to exit cancel scope in a different task" in str(e):
                    logger.warning(f"Known anyio issue during stdio_client exit for GitHub: {e}. Process might already be terminating.")
                else:
                    logger.error(f"RuntimeError exiting stdio_client context for GitHub: {e}")
            except Exception as e:
                logger.error(f"Error exiting stdio_client context for GitHub: {e}")
            finally:
                self._stdio_client_cm = None
        
        self.initialized = False
        self.tools = [] # Clear cached tools
        logger.info("GitHub MCP connector shutdown complete.")

# Need to import traceback for logging
import traceback