"""Code Test AI Agent.

A CLI-based agent that detects changed code in a Spring Boot project, prunes it
to the minimum context through MCP servers, makes a **single** LLM call that
returns both the intent/importance analysis and the @SpringBootTest code,
executes it with JaCoCo, and reports the result in a Terminal UI.

Layers (each imports only downward):

    cli  →  agent  →  mcp  →  storage
                  ↘  models ↙        (shared contract, imports nothing)
"""

__version__ = "0.1.0"
