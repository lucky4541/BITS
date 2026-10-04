"""
Quick test to verify superscript detection on abbreviations page.
"""
import fitz
import sys

from core.formatting_detector import (
    classify_position,
    line_baseline_stats,
    line_shear_stats,
    _char_shear_ratio,
    detect_bold_italic,
)

def test_abbreviations_page(pdf_path):
    doc = fitz.open(pdf_path)
    
    # Page 3 is the abbreviations page (0-indexed = 2)
    page = doc[2]
    raw = page.get_text("rawdict")
    
    print("\n" + "="*80)
    print("ABBREVIATIONS PAGE - FORMATTING DETECTION")
    print("="*80 + "\n")
    
    total = 0
    formatted = 0
    
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            dom_size, dom_baseline = line_baseline_stats(line)
            dom_shear = line_shear_stats(line)
            
            for span in line.get("spans", []):
                flags = span.get("flags", 0)
                font_name = span.get("font", "")
                size = span.get("size", 0)
                
                for ch in span.get("chars", []):
                    c = ch.get("c", "")
                    if not c or c.isspace():
                        continue
                    total += 1
                    
                    origin_y = ch.get("origin", [0, 0])[1]
                    char_shear = _char_shear_ratio(ch)
                    
                    bold, italic = detect_bold_italic(
                        flags=flags,
                        font_name=font_name,
                        shear_ratio=char_shear,
                        dominant_shear_ratio=dom_shear,
                    )
                    
                    pos = classify_position(
                        size=size,
                        origin_y=origin_y,
                        dominant_size=dom_size,
                        dominant_baseline=dom_baseline,
                        superscript_flag=bool(flags & 1),
                        allow_size_only_fallback=True,
                    )
                    
                    if pos != "normal" or bold or italic:
                        formatted += 1
                        print(f"  '{c}' | Size: {size:.2f} | Dom: {dom_size:.2f} | Ratio: {size/dom_size:.3f} | Pos: {pos} | Bold: {bold} | Italic: {italic}")
    
    print(f"\nTotal characters: {total}")
    print(f"Formatted characters: {formatted} ({formatted/total*100:.1f}%)")
    
    # Show the actual text
    text = page.get_text("text")
    print("\n" + "="*80)
    print("ACTUAL TEXT CONTENT:")
    print("="*80)
    print(text)
    
    doc.close()

if __name__ == "__main__":
    test_abbreviations_page("08_191AR_fm7.pdf")