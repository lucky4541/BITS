"""The OCR result shape every OCREngine must produce (spec: "OCR must NOT
return plain text only" - page number, dimensions, blocks with bounding
boxes and confidence, engine identity, language, timing are all
mandatory). Never a plain string return anywhere in this package."""
from dataclasses import dataclass, field, asdict


@dataclass
class OCRBlock:
    """One recognized text region. bbox is ALWAYS in PDF point space
    (already mapped via coordinate_mapper.py before this is stored/used
    outside the engine that produced it) - [x0, y0, x1, y1], same
    coordinate system core/zone_manager.py's Zone.bbox already uses, so an
    OCRBlock can become a Zone with zero further conversion.

    kind is a CANDIDATE layout guess only (text/title/heading/paragraph/
    list/table/figure/caption/footnote/page_number/other) - spec 23:
    "OCR/layout detection is only a candidate layer. Do NOT automatically
    assume every OCR object is a final CUPEPUB tag." Nothing in this
    package ever assigns a CUP/XML tag directly from `kind` - that
    decision stays with the user (or, later, with a candidate-zone review
    step that maps `kind` to a SUGGESTED tag, never an applied one)."""
    text: str
    bbox: list  # [x0, y0, x1, y1] in PDF points
    confidence: float  # 0.0-1.0
    kind: str = "text"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OCRBlock":
        return cls(text=d["text"], bbox=list(d["bbox"]), confidence=float(d["confidence"]),
                    kind=d.get("kind", "text"))


@dataclass
class OCRResult:
    """One page's complete OCR result. `text` is the reading-order-naive
    concatenation of every block's text (for quick preview/search only -
    ZoneTool's own reading order, built from the resulting zones, is what
    actually governs document order per spec rule 15/16, never this
    field's own block order)."""
    page_number: int
    image_width: int
    image_height: int
    dpi: int
    text: str
    blocks: list  # list[OCRBlock]
    confidence: float  # page-level aggregate (mean of block confidences)
    engine: str
    engine_version: str
    language: str
    processing_time: float
    error: str = None  # set (with blocks=[]) if this engine failed on this page - never raises past here

    def to_dict(self) -> dict:
        d = asdict(self)
        d["blocks"] = [b if isinstance(b, dict) else b.to_dict() for b in self.blocks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "OCRResult":
        blocks = [OCRBlock.from_dict(b) for b in d.get("blocks", [])]
        return cls(
            page_number=d["page_number"], image_width=d["image_width"], image_height=d["image_height"],
            dpi=d["dpi"], text=d.get("text", ""), blocks=blocks, confidence=float(d.get("confidence", 0.0)),
            engine=d.get("engine", ""), engine_version=d.get("engine_version", ""),
            language=d.get("language", ""), processing_time=float(d.get("processing_time", 0.0)),
            error=d.get("error"),
        )

    @classmethod
    def empty(cls, page_number: int, image_width: int, image_height: int, dpi: int,
              engine: str, engine_version: str, language: str, error: str) -> "OCRResult":
        """A failed-but-non-crashing result (spec 41: OCR failure must
        never crash ZoneTool) - zero blocks, the failure reason recorded
        in `error`, everything else still a valid OCRResult the rest of
        the pipeline can handle uniformly (no None-checking needed at
        every call site)."""
        return cls(page_number=page_number, image_width=image_width, image_height=image_height, dpi=dpi,
                    text="", blocks=[], confidence=0.0, engine=engine, engine_version=engine_version,
                    language=language, processing_time=0.0, error=error)
