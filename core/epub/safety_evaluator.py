"""SafetyEvaluator (spec: "EPUBForge - PHASE 3 - Universal Auto-Fix
Engine" section 1). Sits between core.epub.error_analyzer's own
RepairPlan objects and core.epub.repair_strategies' StrategyRegistry: it
decides WHICH root-cause categories are safe to actually act on this
pass, never how to fix anything itself (spec 3: "Do not independently
repair cascading errors") and never anything beyond LEVEL 1 (spec 3:
"Automatically repair ONLY deterministic issues")."""
from core.epub.error_analyzer import Repairability


def safe_categories(analyses) -> set:
    """analyses: a list of core.epub.error_analyzer.ErrorAnalysis (as
    produced by analyze_errors). Returns the set of RootCause.category
    values that are BOTH classified SAFE_AUTO_FIX and marked deterministic
    - cascading findings are excluded by construction, since
    error_analyzer never marks a cascading finding's own category as one
    of the deterministic SAFE_AUTO_FIX categories (it uses OPF_CASCADE/
    REVIEW_REQUIRED instead) - so this function does not need its own
    separate cascade check."""
    return {
        a.root_cause.category for a in analyses
        if a.repair_plan.repairability == Repairability.SAFE_AUTO_FIX and a.repair_plan.deterministic
    }
