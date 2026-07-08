"""Pydantic AI chat agent + Foundry model wiring (R10–R12). Public surface via explicit
re-exports."""

from src.services.agent.agent import ChatDeps as ChatDeps
from src.services.agent.agent import chat_agent as chat_agent
from src.services.agent.content import to_model_content as to_model_content
from src.services.agent.model import FoundryOnlyError as FoundryOnlyError
from src.services.agent.model import build_foundry_client as build_foundry_client
from src.services.agent.model import build_foundry_model as build_foundry_model
