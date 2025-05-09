"""
Filesystem MCP connector for the Sherlog REPL.
Provides tools for interacting with the filesystem.
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


class FilesystemConnector(MCPConnector):
    """
    Connector for Filesystem MCP.
    Uses the MCP Python SDK client to communicate with the filesystem MCP server.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the Filesystem connector.
        
        Args:
            config: Configuration for the connector.
                - container_image: Not used directly, npx package is specified (default: @modelcontextprotocol/server-filesystem)
                - root_dirs: List of directories to expose (default: current working directory)
                - npx_package: The npx package for the filesystem server (default: @modelcontextprotocol/server-filesystem)
        """
        super().__init__("filesystem", config)
        self.npx_package = self.config.get(
            "npx_package", 
            "@modelcontextprotocol/server-filesystem"
        )
        self.root_dirs = self.config.get("root_dirs", [os.getcwd()])
        
        self._client_session: Optional[ClientSession] = None
        self._stdio_client_cm: Optional[AsyncContextManager] = None # To hold the context manager
        self._server_params: Optional[StdioServerParameters] = None

        # Cached tool information
        self.tools: List[Dict[str, Any]] = [] # Keep as dicts for sherlog compatibility

        # Internal flag to avoid recursive initialise -> list_tools -> initialise loops
        self._initializing: bool = False
    
    async def _get_client_session(self) -> ClientSession:
        """Initializes and returns the MCP ClientSession for the filesystem server."""
        if self._client_session: # Check if session object exists
            # If it exists, assume it was initialized by a previous call.
            # ClientSession's usability is determined by successful 'await session.initialize()'.
            return self._client_session

        # Ensure root directories exist and are absolute for npx command
        abs_root_dirs = []
        for dir_path in self.root_dirs:
            abs_path = os.path.abspath(dir_path)
            if not os.path.isdir(abs_path):
                logger.warning(f"Root directory does not exist or is not a directory: {abs_path}. Skipping for Filesystem MCP.")
                # Optionally raise an error if no valid root dirs are found
            else:
                abs_root_dirs.append(abs_path)
        
        if not abs_root_dirs:
            err_msg = "No valid root directories configured for Filesystem MCP."
            logger.error(err_msg)
            raise RuntimeError(err_msg)

        # Check if npx is available
        try:
            npx_check_process = await asyncio.create_subprocess_exec(
                "npx", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr_bytes = await npx_check_process.communicate()
            if npx_check_process.returncode != 0:
                err_msg = f"npx is not available or npx command failed: {stderr_bytes.decode(errors='ignore')}"
                logger.error(err_msg)
                raise RuntimeError(err_msg)
        except FileNotFoundError:
            logger.error("npx command not found. Filesystem MCP connector requires npx (Node.js).")
            raise RuntimeError("npx command not found.")
        except Exception as e:
            logger.error(f"Error checking npx status: {e}")
            raise RuntimeError(f"Error checking npx status: {e}")

        self._server_params = StdioServerParameters(
            command="npx",
            args=["-y", self.npx_package, *abs_root_dirs],
        )
        
        logger.info(f"Starting Filesystem MCP server with command: npx -y {self.npx_package} {' '.join(abs_root_dirs)}")

        try:
            self._stdio_client_cm = stdio_client(self._server_params)
            read_stream, write_stream = await self._stdio_client_cm.__aenter__()

            self._client_session = ClientSession(read_stream, write_stream)
            await self._client_session.__aenter__()

            logger.info("Attempting to initialize ClientSession with Filesystem MCP server...")
            await self._client_session.initialize()
            logger.info("ClientSession with Filesystem MCP server initialized successfully.")
            
            await asyncio.sleep(1) # Small delay post-initialize

            return self._client_session
        except Exception as e:
            logger.error(f"Failed to establish ClientSession with Filesystem MCP server: {e}")
            logger.debug(traceback.format_exc())
            if self._client_session:
                await self._client_session.__aexit__(None, None, None)
                self._client_session = None
            if self._stdio_client_cm:
                await self._stdio_client_cm.__aexit__(None, None, None)
                self._stdio_client_cm = None
            raise RuntimeError(f"Failed to initialize Filesystem MCP client session: {e}")

    async def initialize(self) -> bool:
        """
        Initialize the Filesystem MCP connector by establishing a client session.
        Returns: True if initialization was successful, False otherwise.
        """
        if self.initialized:
            return True
        
        # Prevent recursive initialise/list_tools loops
        if self._initializing:
            return False

        self._initializing = True
        
        try:
            # _get_client_session will raise an error if it fails to init the session.
            session = await self._get_client_session()
            # If _get_client_session completed, session.initialize() within it was successful.

            raw_tools = await self.list_tools(force_refresh=True)
            if not raw_tools:
                logger.warning("Filesystem MCP connector initialized, but no tools were found.")
            
            self.initialized = True
            logger.info(f"Filesystem MCP connector initialized. Found {len(raw_tools)} tools.")
            return True
        except Exception as e:
            logger.error(f"Error initializing Filesystem MCP connector: {e}")
            logger.debug(traceback.format_exc())
            return False
        finally:
            self._initializing = False

    async def list_tools(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        List all available tools from the Filesystem MCP server.
        Args:
            force_refresh: If True, bypass cache and fetch from server.
        Returns:
            A list of tool definitions as dictionaries.
        """
        if not force_refresh and self.tools:
            return self.tools
        
        if (not self.initialized) and (not self._initializing):
            if not await self.initialize():
                logger.warning("Cannot list tools: Filesystem connector not initialized.")
                return []

        try:
            session = await self._get_client_session()
            logger.info("Fetching tools from Filesystem MCP server via ClientSession...")
            list_tools_result: mcp_types.ListToolsResult = await session.list_tools()

            logger.info(f"Raw ListToolsResult received from Filesystem MCP server: {list_tools_result}")
            
            self.tools = []
            actual_tool_definitions = list_tools_result.tools

            if isinstance(actual_tool_definitions, list):
                for tool_def_item in actual_tool_definitions:
                    if isinstance(tool_def_item, mcp_types.Tool):
                        # Some MCP servers return tools without an explicit `id` field. Fallback to `name`.
                        tool_id_val = getattr(tool_def_item, "id", None) or getattr(tool_def_item, "name", None)
                        if not tool_id_val:
                            logger.warning(f"Tool definition missing both 'id' and 'name': {tool_def_item}. Skipping.")
                            continue

                        self.tools.append({
                            "id": tool_id_val,
                            "tool_id": tool_id_val,
                            "name": tool_def_item.name,
                            "description": tool_def_item.description,
                            # MCP v2024 Tool objects expose `inputSchema`; older versions expose `parameters`/`returns`.
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
                        logger.warning(f"Skipping unexpected item type in tool list from Filesystem: {type(tool_def_item)}, value: {tool_def_item}")
            elif actual_tool_definitions is None:
                logger.info("Filesystem MCP server returned no tools (tools attribute is None).")
            else:
                logger.error(f"Unexpected type for actual_tool_definitions from Filesystem: {type(actual_tool_definitions)}, value: {actual_tool_definitions}")

            logger.info(f"Successfully processed {len(self.tools)} tools from Filesystem MCP server.")
            return self.tools
        except Exception as e:
            logger.error(f"Error listing Filesystem MCP tools via ClientSession: {e}")
            logger.debug(traceback.format_exc())
            return []
    
    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Any:
        """
        Execute a Filesystem MCP tool using the ClientSession.
        Args:
            tool_id: The ID of the tool to execute.
            params: Parameters to pass to the tool.
        Returns:
            The result of the tool execution.
        """
        if not self.initialized and not await self.initialize():
            raise RuntimeError("Filesystem MCP connector not initialized, cannot execute tool.")

        try:
            session = await self._get_client_session()
            logger.info(f"Executing tool '{tool_id}' with params: {params} via Filesystem ClientSession")
            
            result = await session.call_tool(name=tool_id, arguments=params)
            # Process result if needed (e.g. mcp_types.Content handling)
            logger.info(f"Tool '{tool_id}' executed successfully via Filesystem ClientSession.")
            return result
            
        except Exception as e:
            logger.error(f"Error executing Filesystem MCP tool {tool_id} via ClientSession: {e}")
            logger.debug(traceback.format_exc())
            raise RuntimeError(f"Error executing Filesystem tool {tool_id}: {e}")
    
    async def shutdown(self) -> None:
        """
        Shutdown the Filesystem MCP connector and close the ClientSession.
        """
        logger.info("Shutting down Filesystem MCP connector...")
        if self._client_session:
            try:
                logger.info("Closing ClientSession with Filesystem MCP server...")
                await self._client_session.__aexit__(None, None, None)
                logger.info("ClientSession closed.")
            except Exception as e:
                logger.error(f"Error closing ClientSession: {e}")
            finally:
                self._client_session = None
        
        if self._stdio_client_cm:
            try:
                logger.info("Exiting stdio_client context for Filesystem MCP server...")
                await self._stdio_client_cm.__aexit__(None, None, None)
                logger.info("stdio_client context exited.")
            except RuntimeError as e:
                if "Attempted to exit cancel scope in a different task" in str(e):
                    logger.warning(f"Known anyio issue during stdio_client exit for Filesystem: {e}. Process might already be terminating.")
                else:
                    logger.error(f"RuntimeError exiting stdio_client context for Filesystem: {e}")
            except Exception as e:
                logger.error(f"Error exiting stdio_client context for Filesystem: {e}")
            finally:
                self._stdio_client_cm = None
        
        self.initialized = False
        self.tools = [] # Clear cached tools
        logger.info("Filesystem MCP connector shutdown complete.")

# Need to import traceback for logging
import traceback