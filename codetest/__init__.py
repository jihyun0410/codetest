"""Code Test AI Agent.

A CLI-based agent that detects changed code in a Spring Boot project
(Git Diff + AST), reasons about the intent of the change, generates
@SpringBootTest test code via an LLM abstraction layer, executes it with
JaCoCo, and reports the result in a Terminal UI.
"""

__version__ = "0.1.0"
