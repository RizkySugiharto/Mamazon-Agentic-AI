"""Agent package: LangChain/LangGraph chat agent over a local zvec knowledge base.

Layers:
    embeddings.py  local ONNX embeddings (no API key, no network at query time)
    vectorstore.py LangChain VectorStore backed by zvec (embedded, local disk)
    knowledge.py   corpus builder: rules, allowed commands, allowed information
    providers.py   LangChain chat model for the 9Router gateway
    graph.py       LangGraph StateGraph (guard -> retrieve -> tools -> respond)
"""

from __future__ import annotations

__all__ = ["embeddings", "vectorstore", "knowledge", "providers", "graph"]
