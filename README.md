# Sherlog-Canvas Unified REPL MCP Server

A unified REPL-based MCP server for Sherlog-Canvas that integrates multiple MCP services (GitHub, Filesystem, Jira) into a single persistent Python session.

This server provides a powerful environment for working with Claude and other LLMs by enabling a persistent REPL session with access to multiple MCP tools.

## Features

- **Persistent Python Session**: Maintain variables and state between calls
- **Unified Tool Access**: Access GitHub, Filesystem, and Jira tools from a single REPL
- **Dynamic Tool Discovery**: Automatically discover and register tools from MCP services
- **Session Management**: Support for multiple concurrent sessions with isolation
- **Tool Proxies**: Call MCP tools as native Python functions

## Installation

```bash
pip install sherlog-repl
```

## Usage

```bash
sherlog-repl
```

## Using with Docker

You can run the REPL server using Docker:

```bash
docker pull ghcr.io/evalstate/sherlog-repl
docker run -it \
  -e GITHUB_TOKEN=<your-token> \
  -e JIRA_URL=<your-jira-url> \
  -e JIRA_API_TOKEN=<your-jira-token> \
  -e JIRA_EMAIL=<your-jira-email> \
  -v $(pwd):/mnt/data \
  ghcr.io/evalstate/sherlog-repl
```

The container includes:
- Python 3.12
- UV package manager for fast dependency installation
- Pre-configured MCP environment
- GitHub, Filesystem, and Jira MCP connectors

## Available Tools

The server provides the following tools:

1. `execute_python`: Execute Python code with access to MCP tools
   - `code`: The Python code to execute
   - `reset`: Optional boolean to reset the session
   - `register_tools`: Optional list of tool domains to register

2. `list_available_tools`: List all available tools from MCP connectors
   - `domain`: Optional domain to filter tools by (e.g., "github", "filesystem", "jira")

3. `register_tools`: Register tools from specified domains in the current session
   - `domains`: List of domains to register tools from (e.g., ["github", "filesystem"])

4. `list_session_variables`: Show all variables in the current session

5. `reset_session`: Reset the current session, clearing all variables

## Examples

### Register and use GitHub tools

```python
# Register GitHub tools
await register_tools(domains=["github"])

# List GitHub repositories
repos = await github_list_repositories(owner="yourusername")
for repo in repos:
    print(repo['name'])
```

### Register and use Filesystem tools

```python
# Register Filesystem tools
await register_tools(domains=["filesystem"])

# List files in a directory
files = await filesystem_list_files(path="/mnt/data")
for file in files:
    print(file['name'])
```

### Persistent variables across calls

```python
# First call
data = await github_list_issues(owner="yourusername", repo="yourrepo")
issue_count = len(data)

# Second call (issue_count persists)
print(f"Found {issue_count} issues")
```

## Architecture

The Sherlog-Canvas Unified REPL MCP Server integrates:

1. **MCP Connectors**: Interfaces to GitHub, Filesystem, and Jira MCP services
2. **Dynamic Tool Proxy Generator**: Creates Python function proxies for MCP tools
3. **Session Manager**: Maintains isolated sessions for different users/notebooks
4. **Concurrency Manager**: Handles parallel execution of MCP operations

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

Before submitting a PR, please ensure:

1. Your code follows the existing style
2. You've updated documentation as needed
3. You've added tests for new functionality

For major changes, please open an issue first to discuss what you would like to change.