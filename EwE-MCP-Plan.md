# EwE MCP Integration Plan

## Objective
Create a Model Context Protocol (MCP) server for the classic Windows version of Ecopath with Ecosim (EwE) using a Python-based FastMCP server connected to a custom C# plugin via a TCP socket.

## Key Components

1.  **EwE Plugin (C# / .NET Framework 4.8)**
    *   **Role**: Acts as a bridge inside the EwE application, executing commands within the EwE UI thread.
    *   **Implementation**: A custom plugin implementing EwE's `IUIContextPlugin` and/or `ICommandHandlerPlugin`.
    *   **Mechanism**: Starts a non-blocking TCP socket server upon initialization. It listens for JSON payloads, parses them, and maps them to EwE UI commands or direct Core API calls.
    *   **Execution**: Uses EwE's `cCommandHandler` to invoke registered UI commands (e.g., `LoadEcopathModel`, `RunEcosimScenario`) or modifies `cCore` data directly and triggers UI updates.

2.  **MCP Server (Python / FastMCP)**
    *   **Role**: Exposes EwE capabilities as MCP tools to LLM clients (like Claude).
    *   **Implementation**: A standalone Python script using the `fastmcp` library (similar to `qgis-mcp`).
    *   **Mechanism**: Connects to the EwE Plugin's TCP socket. Translates MCP tool calls into JSON commands and sends them to the plugin.
    *   **Features**: Manages caching for large datasets (e.g., extracting time series data or biomass results as MCP resources) and provides semantic error handling.

## Implementation Steps

### Phase 1: The C# TCP Bridge Plugin
1.  **Plugin Scaffolding**: Create a new Class Library (.NET Framework) project referencing `EwECore.dll`, `EwEUtils.dll`, and `ScientificInterfaceShared.dll`.
2.  **Plugin Interface**: Implement `IPlugin` and `IGUIPlugin` to integrate with the EwE plugin manager.
3.  **Socket Server**: Implement an asynchronous TCP listener (e.g., `System.Net.Sockets.TcpListener`) on a dedicated background thread to avoid blocking the EwE UI.
4.  **Command Router**: Implement a thread-safe message queue. The background socket thread parses incoming JSON and marshals the execution of EwE commands onto the main UI thread using `Control.Invoke` or `SynchronizationContext`.
5.  **Core Access**: Expose endpoints for:
    *   Invoking named `cCommand` objects via the `cCommandHandler`.
    *   Reading model parameters directly from `EwECore.cCore`.
    *   Modifying parameters and triggering UI refreshes.

### Phase 2: The Python FastMCP Server
1.  **Project Setup**: Initialize a Python project with `uv` and install `fastmcp` (similar to the QGIS MCP).
2.  **Socket Client**: Implement an asynchronous TCP client to communicate with the C# plugin using a length-prefixed JSON protocol.
3.  **Tool Definitions**: Define initial MCP tools:
    *   `load_model(path: str)`: Loads an `.eweaccdb` database.
    *   `run_ecosim()`: Triggers an Ecosim simulation.
    *   `get_groups()`: Retrieves Ecopath group names and IDs.
    *   `get_biomass(group_id: int)`: Retrieves current biomass for a group.
4.  **Error Handling**: Implement robust error mapping from C# exceptions to MCP error responses.

### Phase 3: Advanced Integration (Optional/Later)
1.  **Event Subscriptions**: Allow the Python server to subscribe to EwE events (e.g., `OnRunCompleted`) to notify the LLM when a long-running simulation finishes.
2.  **Visual Resources**: Create tools to export EwE plots and charts as images and serve them back to the LLM as MCP resources (e.g., `qgis://cache/{id}` style).

## Verification & Testing
*   **Unit Tests**: Mock the TCP socket in Python to test tool parsing and response handling.
*   **Integration Tests**: Run the EwE Plugin within a live instance of EwE and execute Python scripts to verify successful command execution and state changes.

## Migration & Rollback
*   The plugin is an optional DLL in the EwE `plugins` folder. Removing the DLL completely disables the MCP integration without affecting the core application.
*   The TCP server binds to a local port (e.g., 9876). If port conflicts occur, the plugin should allow configuration via EwE settings.