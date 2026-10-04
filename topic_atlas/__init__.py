"""Topic Atlas V1: advisory discovery, isolated from world/character authority."""
from .collector import POLICY_VERSION, collect_topics_once
from .projection import index_topics_once, project_topics_once
from .research_dossier import ProgressiveResearchService

__all__ = ["POLICY_VERSION", "collect_topics_once", "index_topics_once",
           "project_topics_once", "ProgressiveResearchService"]
