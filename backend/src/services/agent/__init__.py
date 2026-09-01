"""Pydantic AI chat agent + Foundry model wiring (R10–R12). Public surface via explicit
re-exports."""

from src.services.agent.agent import ChatDeps as ChatDeps
from src.services.agent.agent import chat_agent as chat_agent
from src.services.agent.mode_prompts import PromptContext as PromptContext
from src.services.agent.mode_prompts import compose_kind_prompt as compose_kind_prompt
from src.services.agent.model import FoundryOnlyError as FoundryOnlyError
from src.services.agent.model import build_foundry_client as build_foundry_client
from src.services.agent.model import build_foundry_model as build_foundry_model
from src.services.agent.read_tools import EmptyProjectWorkspace as EmptyProjectWorkspace
from src.services.agent.read_tools import ExtractedSnapshotWorkspace as ExtractedSnapshotWorkspace
from src.services.agent.read_tools import ReadOnlyWorkspace as ReadOnlyWorkspace
from src.services.agent.read_tools import check_the_guest_list as check_the_guest_list
from src.services.agent.read_tools import read_only_toolset as read_only_toolset
from src.services.agent.toolsets import ReadDeps as ReadDeps
from src.services.agent.toolsets import toolsets_for_kind as toolsets_for_kind
