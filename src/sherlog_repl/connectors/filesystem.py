"""
Filesystem MCP connector for the Sherlog REPL.
Provides tools for interacting with the filesystem.
"""

import asyncio
import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

from .base import MCPConnector

logger = logging.getLogger(__name__)


class FilesystemConnector(MCPConnector):
    """
    Connector for Filesystem MCP.
    Uses subprocess to communicate with the filesystem MCP server.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the Filesystem connector.
        
        Args:
            config: Configuration for the connector.
                - container_image: Docker image for filesystem MCP server 
                  (default: modelcontextprotocol/filesystem-mcp-server:latest)
                - root_dirs: List of directories to expose (default: current working directory)
        """
        super().__init__("filesystem", config)
        self.process = None
        self.container_image = self.config.get(
            "container_image", 
            "modelcontextprotocol/filesystem-mcp-server:latest"
        )
        self.root_dirs = self.config.get("root_dirs", [os.getcwd()])
        
        # Cached tool information
        self.tools: List[Dict[str, Any]] = []
    
    async def initialize(self) -> bool:
        """
        Initialize the Filesystem MCP connector by starting the container.
        
        Returns:
            True if initialization was successful, False otherwise.
        """
        if self.initialized:
            return True
        
        try:
            # Ensure the root directories exist
            for dir_path in self.root_dirs:
                if not os.path.isdir(dir_path):
                    logger.warning(f"Root directory does not exist: {dir_path}")
            
            # Docker volume mounts for root directories
            volume_mounts = []
            env_vars = []
            
            for i, dir_path in enumerate(self.root_dirs):
                abs_path = os.path.abspath(dir_path)
                container_path = f"/mnt/data{i}"
                volume_mounts.extend(["-v", f"{abs_path}:{container_path}"])
                env_vars.extend(["-e", f"ROOT_DIR_{i}={container_path}"])
            
            # Start the filesystem-mcp-server container
            cmd = [
                "docker", "run", "-i", "--rm",
                *volume_mounts,
                *env_vars,
                self.container_image
            ]
            
            # Start the process
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Check if the process started successfully
            if self.process.returncode is not None:
                stderr = await self.process.stderr.read()
                logger.error(f"Failed to start Filesystem MCP server: {stderr.decode()}")
                return False
            
            # Send a list_tools request to check if the server is working
            tools = await self.list_tools()
            if not tools:
                logger.error("Failed to get tools from Filesystem MCP server")
                return False
            
            self.initialized = True
            logger.info(f"Filesystem MCP connector initialized with {len(tools)} tools")
            return True
            
        except Exception as e:
            logger.error(f"Error initializing Filesystem MCP connector: {e}")
            return False
    
    async def list_tools(self) -> List[Dict[str, Any]]:
        """
        List all available tools from the Filesystem MCP server.
        
        Returns:
            A list of tool definitions.
        """
        if not self.process or self.process.returncode is not None:
            if not await self.initialize():
                return []
        
        if self.tools:
            return self.tools
        
        try:
            # Prepare list_tools request
            request = {
                "jsonrpc": "2.0",
                "method": "list_tools",
                "params": {},
                "id": 1
            }
            
            # Send request
            request_bytes = (json.dumps(request) + "\n").encode()
            self.process.stdin.write(request_bytes)
            await self.process.stdin.drain()
            
            # Read response
            response_line = await self.process.stdout.readline()
            response = json.loads(response_line)
            
            if "error" in response:
                logger.error(f"Error from Filesystem MCP: {response['error']}")
                return []
            
            if "result" in response and "tools" in response["result"]:
                self.tools = response["result"]["tools"]
                return self.tools
            
            return []
            
        except Exception as e:
            logger.error(f"Error listing Filesystem MCP tools: {e}")
            return []
    
    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Any:
        """
        Execute a Filesystem MCP tool.
        
        Args:
            tool_id: The ID of the tool to execute.
            params: Parameters to pass to the tool.
            
        Returns:
            The result of the tool execution.
        """
        if not self.process or self.process.returncode is not None:
            if not await self.initialize():
                raise RuntimeError("Filesystem MCP connector not initialized")
        
        try:
            # Prepare tool request
            request = {
                "jsonrpc": "2.0",
                "method": "execute_tool",
                "params": {
                    "tool_id": tool_id,
                    "parameters": params
                },
                "id": 2
            }
            
            # Send request
            request_bytes = (json.dumps(request) + "\n").encode()
            self.process.stdin.write(request_bytes)
            await self.process.stdin.drain()
            
            # Read response
            response_line = await self.process.stdout.readline()
            response = json.loads(response_line)
            
            if "error" in response:
                error_message = response["error"].get("message", "Unknown error")
                error_code = response["error"].get("code", -1)
                raise RuntimeError(f"Filesystem MCP error {error_code}: {error_message}")
            
            if "result" in response:
                return response["result"]
            
            return None
            
        except Exception as e:
            logger.error(f"Error executing Filesystem MCP tool {tool_id}: {e}")
            raise
    
    async def shutdown(self) -> None:
        """
        Shutdown the Filesystem MCP connector.
        """
        if self.process and self.process.returncode is None:
            try:
                # Send a terminate signal
                self.process.terminate()
                # Wait for process to terminate
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # If it doesn't terminate in 5 seconds, kill it
                self.process.kill()
            except Exception as e:
                logger.error(f"Error shutting down Filesystem MCP connector: {e}")
            finally:
                self.process = None
                self.initialized = False
                self.tools = []