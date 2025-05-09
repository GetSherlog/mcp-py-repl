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
                
                # Force refresh if the connector supports it; fallback gracefully otherwise
                try:
                    # Many connectors accept a 'force_refresh' boolean parameter.
                    tools = await connector.list_tools(True)  # type: ignore[arg-type]
                except TypeError:
                    # Connector signature does not accept the extra argument (e.g., JiraConnector).
                    tools = await connector.list_tools()  # type: ignore[func-call]
                for tool in tools:
                    # Tools may come back either as dict-like objects or as class instances
                    if isinstance(tool, dict):
                        tool_id = tool.get('id') or tool.get('name')
                        tool_dict = tool
                    else:
                        # Fallback for pydantic/dataclass instances returned by some MCP servers
                        tool_id = getattr(tool, 'id', None) or getattr(tool, 'name', None)
                        try:
                            # Pydantic v2 has model_dump, v1 has dict
                            if hasattr(tool, 'model_dump'):
                                tool_dict = tool.model_dump(exclude_none=True)
                            elif hasattr(tool, 'dict'):
                                tool_dict = tool.dict(exclude_none=True)  # type: ignore[arg-type]
                            else:
                                tool_dict = tool.__dict__
                        except Exception:
                            tool_dict = {}
                    if not tool_id:
                        logger.warning(f"Tool from {name} missing ID/Name, skipping")
                        continue

                    full_tool_id = f"{name}_{tool_id}"
                    self.tool_info[full_tool_id] = {
                        'connector': name,
                        'tool_id': tool_id,
                        **tool_dict
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
            props_dict = parameters.get('properties', {}) if isinstance(parameters, dict) else {}
            required_list = parameters.get('required', []) if isinstance(parameters, dict) else []
            params_list = []
            for name, param in props_dict.items():
                param_type = param.get('type', 'any') if isinstance(param, dict) else 'any'
                param_desc = param.get('description', '') if isinstance(param, dict) else ''
                required = name in required_list
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
            
            # ── unwrap common mis-shaped payloads ────────────────────────────
            if "kwargs" in kwargs and (not required_params or any(p not in kwargs for p in required_params)):
                raw = kwargs.pop("kwargs")
                try:
                    if isinstance(raw, str):
                        import json  # noqa: WPS433 – local import OK
                        raw_json = None
                        try:
                            raw_json = json.loads(raw)
                        except Exception:
                            raw_json = None
                        if raw_json is not None:
                            raw = raw_json
                        else:
                            # Try URL-encoded fallback (owner=foo&repo=bar)
                            from urllib.parse import parse_qsl  # noqa: WPS433
                            raw = dict(parse_qsl(raw))
                except Exception:  # noqa: BLE001 – leave as-is if parsing fails
                    pass
                if isinstance(raw, dict):
                    # Only fill in names that are currently missing to let top-level keys win.
                    for k, v in raw.items():
                        kwargs.setdefault(k, v)

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

        # NEW: Register the proxy with the global FastMCP instance so it is
        # advertised via /v1/initialize and visible to front-ends.
        # Import is done here (lazy) to avoid circular-import problems during
        # module load. If anything goes wrong we only log a warning and
        # continue – the proxy will still be usable inside execute_python.
        try:
            from ..server import mcp  # local import to prevent circular dependency issues
            # The FastMCP @tool decorator registers the function when invoked.
            mcp.tool()(proxy_function)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not register proxy %s with FastMCP: %s", full_tool_id, exc)

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
    
    def register_proxies_in_namespace(self, namespace: Dict[str, Any], connector_name: Optional[str] = None) -> int:
        """
        Register proxy functions in a namespace (e.g., a session's globals).
        If connector_name is provided, only proxies from that connector are registered.
        
        Args:
            namespace: The namespace to register the proxies in.
            connector_name: Optional name of the connector to filter proxies by.
            
        Returns:
            The number of proxies registered.
        """
        count = 0
        domain_funcs: Dict[str, Any] = {}
        for full_tool_id, proxy in self.proxies.items():
            # Filter by connector when requested
            if connector_name is not None and not full_tool_id.startswith(f"{connector_name}_"):
                continue

            # Preserve original async proxy under a predictable alias so advanced
            # users can still "await" it explicitly if they want.
            async_name = f"{full_tool_id}_async"
            namespace[async_name] = proxy

            # Create a sync wrapper that hides ctx & await from the caller.
            # The wrapper will fetch `ctx` from the namespace (we inject it from
            # execute_python) and run the coroutine, benefiting from nest_asyncio.

            def _make_sync_wrapper(_proxy):  # closure over specific proxy
                def _sync_wrapper(*args, **kwargs):  # noqa: ANN001
                    # Late import to keep top-level clean
                    _ns = namespace  # capture current namespace reference

                    _ctx = kwargs.pop("ctx", None) or _ns.get("ctx") or _ns.get("_ctx")
                    if _ctx is None:
                        raise RuntimeError("MCP context 'ctx' is not available in the session namespace.")

                    # If caller supplied positional args, map them onto parameter names
                    if args:
                        # Attempt to infer parameter order from proxy signature (skip ctx)
                        sig = inspect.signature(_proxy)
                        param_names = [p.name for p in sig.parameters.values()][1:]  # exclude ctx
                        for idx, val in enumerate(args):
                            if idx < len(param_names):
                                # Don't override if same key already provided in kwargs
                                if param_names[idx] not in kwargs:
                                    kwargs[param_names[idx]] = val
                        args = ()

                    coro = _proxy(_ctx, **kwargs)

                    if inspect.iscoroutine(coro):
                        try:
                            loop = asyncio.get_running_loop()
                            try:
                                return loop.run_until_complete(coro)
                            except RuntimeError:
                                # If nest_asyncio not patched, this will raise; fall through
                                pass
                        except RuntimeError:
                            # No running loop; safe to asyncio.run
                            return asyncio.run(coro)

                        # Fallback when loop is running but run_until_complete failed
                        # Schedule the coroutine and wait synchronously using asyncio.tasks
                        fut = asyncio.run_coroutine_threadsafe(coro, loop)
                        value = fut.result()
                    else:
                        value = coro  # pragma: no cover – defensive

                    # Further unwrap common MCP envelope -> plain python structures
                    try:
                        from mcp import types as _mcp_types  # type: ignore

                        def _unwrap_mcp(val):  # noqa: WPS430
                            if isinstance(val, _mcp_types.CallToolResult):
                                # Prefer structured JSON in first TextContent if present
                                if val.content and len(val.content) > 0:
                                    first = val.content[0]
                                    if getattr(first, "type", "") == "text":
                                        _txt = first.text.strip()
                                        import json as _json  # noqa: WPS433
                                        try:
                                            if _txt.startswith("{") or _txt.startswith("["):
                                                return _json.loads(_txt)
                                        except Exception:
                                            pass
                                # Fallback to model_dump -> python dict
                                try:
                                    return val.model_dump(mode="python")
                                except Exception:  # noqa: BLE001
                                    return val
                            return val

                        value = _unwrap_mcp(value)
                    except Exception:  # noqa: BLE001
                        pass

                    return value

                # Copy metadata
                _sync_wrapper.__name__ = _proxy.__name__.replace("_async", "")
                _sync_wrapper.__qualname__ = _sync_wrapper.__name__
                _sync_wrapper.__doc__ = (
                    (getattr(_proxy, "__doc__", "") or "").strip()
                    + "\n\n(Synchronous wrapper – ctx & await handled automatically.)"
                )

                # Expose a clean signature (without ctx) so that `inspect.signature` works
                try:
                    _proxy_sig = inspect.signature(_proxy)
                    _params = [p for p in _proxy_sig.parameters.values()][1:]  # drop ctx
                    _sync_wrapper.__signature__ = inspect.Signature(parameters=_params)  # type: ignore[attr-defined]
                except Exception:
                    pass
                return _sync_wrapper

            sync_wrapper = _make_sync_wrapper(proxy)
            namespace[full_tool_id] = sync_wrapper
            # Store short name (without prefix) for aggregation object
            short_name = full_tool_id.split("_", 1)[1] if "_" in full_tool_id else full_tool_id
            domain_funcs[short_name] = sync_wrapper
            count += 1

        # Expose a simple namespace-style aggregator (e.g., namespace['github'])
        if connector_name is not None:
            import types as _t
            namespace[connector_name] = _t.SimpleNamespace(**domain_funcs)
        elif domain_funcs:
            # When registering all connectors, group by each connector name
            from collections import defaultdict  # noqa: WPS433
            grouped: Dict[str, Dict[str, Any]] = defaultdict(dict)
            for full_id, func in ((k, namespace[k]) for k in domain_funcs):
                dom, short = full_id.split("_", 1)
                grouped[dom][short] = func
            import types as _t
            for dom, funcs in grouped.items():
                namespace[dom] = _t.SimpleNamespace(**funcs)
        return count