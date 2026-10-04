from dataclasses import dataclass, field


@dataclass
class RepairActionResult:
    applied: bool
    description: str = ""
    files: list = field(default_factory=list)