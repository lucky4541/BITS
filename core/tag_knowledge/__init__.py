"""Tag Knowledge Model: derives the project's tagging rules from its own DTD,
Mapping.xml, zoning configuration, reference XML corpus and reference
projects (see knowledge_model.py)."""
from core.tag_knowledge.knowledge_model import TagKnowledgeModel, TagOption, RoleMatch, StructureVerdict  # noqa: F401
from core.tag_knowledge.dtd_model import DTDModel  # noqa: F401
from core.tag_knowledge.corpus_model import CorpusModel  # noqa: F401
from core.tag_knowledge.mapping_model import MappingModel  # noqa: F401
