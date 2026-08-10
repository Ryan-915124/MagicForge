"""MCP-backed research discovery adapters."""

from research.search.coordinator import ResearchSearchCoordinator
from research.search.exa import ExaAcademicSearch
from research.search.tavily import TavilyPractitionerSearch

__all__ = ["ExaAcademicSearch", "ResearchSearchCoordinator", "TavilyPractitionerSearch"]
from research.search.content import MCPContentFetcher
from research.search.coordinator import ResearchSearchCoordinator
from research.search.exa import ExaAcademicSearch
from research.search.tavily import TavilyPractitionerSearch

__all__ = [
    "ExaAcademicSearch",
    "MCPContentFetcher",
    "ResearchSearchCoordinator",
    "TavilyPractitionerSearch",
]
