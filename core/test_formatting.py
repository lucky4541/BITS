"""
Test script to verify formatting detection on a PDF.
Run this from the command line: python test_formatting.py your_pdf.pdf
"""

import sys
import os
import fitz  # PyMuPDF

# Add the path to your project's core module if needed
# sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the formatting detector
from core.formatting_detector import (
    detect_formatting,
    classify_position,
    line_baseline_stats,
    line_shear_stats,
    span_shear_ratio,
    _char_shear_ratio,
    detect_bold_italic,
    FLAG_BOLD,
    FLAG_ITALIC,
    FLAG_SUPERSCRIPT,
)


def test_pdf_formatting(pdf_path, page_num=0):
    """
    Test formatting detection on a PDF page.
    
    Args:
        pdf_path: Path to the PDF file
        page_num: Page number to test (0-indexed)
    """
    if not os.path.exists(pdf_path):
        print(f"❌ File not found: {pdf_path}")
        return
    
    print(f"\n{'='*80}")
    print(f"📄 Testing PDF: {os.path.basename(pdf_path)}")
    print(f"📑 Page: {page_num + 1}")
    print(f"{'='*80}\n")
    
    # Open the PDF
    doc = fitz.open(pdf_path)
    page = doc[page_num]
    
    # Get raw text with formatting info
    raw = page.get_text("rawdict")
    
    # Statistics
    total_chars = 0
    chars_with_formatting = 0
    bold_count = 0
    italic_count = 0
    sup_count = 0
    sub_count = 0
    smallcaps_count = 0
    
    print("📊 SCANNING CHARACTERS...\n")
    print("-" * 80)
    print(f"{'Char':<6} {'Font':<20} {'Size':<8} {'Bold':<6} {'Italic':<8} {'Pos':<6} {'Shear':<8}")
    print("-" * 80)
    
    # Process each block
    for block in raw.get("blocks", []):
        if block.get("type") != 0:  # Skip non-text blocks
            continue
            
        for line in block.get("lines", []):
            # Get line stats for baseline detection
            dom_size, dom_baseline = line_baseline_stats(line)
            dom_shear = line_shear_stats(line)
            
            for span in line.get("spans", []):
                flags = span.get("flags", 0)
                font_name = span.get("font", "")
                size = span.get("size", 0)
                chars = span.get("chars", [])
                
                # Skip empty spans
                if not chars:
                    continue
                
                # Get span-level formatting
                span_shear = span_shear_ratio(chars)
                
                # Detect formatting for each character
                for ch in chars:
                    c = ch.get("c", "")
                    if not c or c.isspace():
                        continue
                    
                    total_chars += 1
                    
                    # Get character geometry
                    bbox = ch.get("bbox", [0, 0, 0, 0])
                    origin = ch.get("origin", [0, 0])
                    origin_y = origin[1] if len(origin) > 1 else 0
                    
                    # Character shear
                    char_shear = _char_shear_ratio(ch)
                    
                    # Detect bold/italic
                    bold, italic = detect_bold_italic(
                        flags=flags,
                        font_name=font_name,
                        shear_ratio=char_shear,
                        dominant_shear_ratio=dom_shear,
                    )
                    
                    # Detect position (sup/sub)
                    superscript_flag = bool(flags & FLAG_SUPERSCRIPT)
                    pos = classify_position(
                        size=size,
                        origin_y=origin_y,
                        dominant_size=dom_size,
                        dominant_baseline=dom_baseline,
                        superscript_flag=superscript_flag,
                        allow_size_only_fallback=True,
                    )
                    
                    # Detect small caps
                    smallcaps = False
                    if pos == "normal" and c.isalpha() and c.isupper():
                        if dom_size > 0 and size / dom_size <= 0.92:
                            smallcaps = True
                    
                    # Print character with formatting info
                    char_display = c if c.isprintable() else f"U+{ord(c):04X}"
                    
                    # Highlight formatted characters
                    has_format = bold or italic or pos != "normal" or smallcaps
                    if has_format:
                        chars_with_formatting += 1
                        if bold:
                            bold_count += 1
                        if italic:
                            italic_count += 1
                        if pos == "sup":
                            sup_count += 1
                        if pos == "sub":
                            sub_count += 1
                        if smallcaps:
                            smallcaps_count += 1
                    
                    shear_str = f"{char_shear:.3f}" if char_shear is not None else "None"
                    
                    # Print with formatting indicators
                    if has_format:
                        markers = []
                        if bold:
                            markers.append("B")
                        if italic:
                            markers.append("I")
                        if pos == "sup":
                            markers.append("↑")
                        elif pos == "sub":
                            markers.append("↓")
                        if smallcaps:
                            markers.append("SC")
                        marker_str = f"[{','.join(markers)}]"
                        print(f"{char_display:<6} {font_name[:18]:<20} {size:<8.2f} {str(bold):<6} {str(italic):<8} {pos:<6} {shear_str:<8} {marker_str}")
    
    print("-" * 80)
    print("\n📊 SUMMARY:\n")
    print(f"   Total characters scanned: {total_chars}")
    print(f"   Characters with formatting: {chars_with_formatting} ({chars_with_formatting/total_chars*100:.1f}%)")
    print(f"   Bold: {bold_count}")
    print(f"   Italic: {italic_count}")
    print(f"   Superscript: {sup_count}")
    print(f"   Subscript: {sub_count}")
    print(f"   Small Caps: {smallcaps_count}")
    
    # Show some examples with context
    print("\n📝 SAMPLE TEXT WITH DETECTED FORMATTING:\n")
    
    # Get formatted text with tags
    from core.text_extractor import extract_formatted_text
    bbox = [0, 0, page.rect.width, page.rect.height]
    formatted_text = extract_formatted_text(page, bbox)
    
    # Show first 500 characters
    preview = formatted_text[:500]
    if len(formatted_text) > 500:
        preview += "..."
    
    # Highlight tags for visibility
    preview = preview.replace("<bold>", "**").replace("</bold>", "**")
    preview = preview.replace("<italic>", "*").replace("</italic>", "*")
    preview = preview.replace("<sup>", "^").replace("</sup>", "^")
    preview = preview.replace("<sub>", "_").replace("</sub>", "_")
    
    print(preview)
    print("\n" + "="*80)
    
    doc.close()


def test_single_character_detection(pdf_path, page_num=0, char_index=None):
    """
    Detailed test for a single character to debug detection issues.
    """
    if not os.path.exists(pdf_path):
        print(f"❌ File not found: {pdf_path}")
        return
    
    print(f"\n{'='*80}")
    print(f"🔍 DETAILED CHARACTER ANALYSIS")
    print(f"{'='*80}\n")
    
    doc = fitz.open(pdf_path)
    page = doc[page_num]
    raw = page.get_text("rawdict")
    
    char_count = 0
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
                    
                    char_count += 1
                    if char_index is not None and char_count != char_index:
                        continue
                    
                    print(f"CHARACTER #{char_count}: '{c}'")
                    print(f"  Font: {font_name}")
                    print(f"  Size: {size:.4f}")
                    print(f"  Flags: {flags}")
                    print(f"    Bold flag: {bool(flags & FLAG_BOLD)}")
                    print(f"    Italic flag: {bool(flags & FLAG_ITALIC)}")
                    print(f"    Superscript flag: {bool(flags & FLAG_SUPERSCRIPT)}")
                    
                    bbox = ch.get("bbox", [0, 0, 0, 0])
                    print(f"  BBox: {bbox}")
                    
                    origin = ch.get("origin", [0, 0])
                    print(f"  Origin: {origin}")
                    
                    char_shear = _char_shear_ratio(ch)
                    print(f"  Shear ratio: {char_shear}")
                    
                    print(f"\n  LINE STATS:")
                    print(f"    Dominant size: {dom_size:.4f}")
                    print(f"    Dominant baseline: {dom_baseline:.4f}")
                    print(f"    Dominant shear: {dom_shear}")
                    
                    print(f"\n  DETECTION RESULTS:")
                    bold, italic = detect_bold_italic(
                        flags=flags,
                        font_name=font_name,
                        shear_ratio=char_shear,
                        dominant_shear_ratio=dom_shear,
                    )
                    print(f"    Bold: {bold}")
                    print(f"    Italic: {italic}")
                    
                    ratio = size / dom_size if dom_size > 0 else 1.0
                    shift = origin[1] - dom_baseline if len(origin) > 1 else 0
                    baseline_threshold = max(1.0, dom_size * 0.30 * 2.0)
                    
                    print(f"\n  POSITION ANALYSIS:")
                    print(f"    Size ratio: {ratio:.4f}")
                    print(f"    Baseline shift: {shift:.4f}")
                    print(f"    Baseline threshold: {baseline_threshold:.4f}")
                    
                    pos = classify_position(
                        size=size,
                        origin_y=origin[1] if len(origin) > 1 else 0,
                        dominant_size=dom_size,
                        dominant_baseline=dom_baseline,
                        superscript_flag=bool(flags & FLAG_SUPERSCRIPT),
                        allow_size_only_fallback=True,
                    )
                    print(f"    Position: {pos}")
                    
                    smallcaps = False
                    if pos == "normal" and c.isalpha() and c.isupper():
                        if dom_size > 0 and size / dom_size <= 0.92:
                            smallcaps = True
                    print(f"    Small caps: {smallcaps}")
                    
                    print(f"\n  REASON FOR POSITION:")
                    if pos == "sup":
                        if ratio < 0.95 and shift < -baseline_threshold:
                            print(f"    → Size ratio {ratio:.4f} < 0.95 AND shift {shift:.4f} < -{baseline_threshold:.4f}")
                        elif ratio < 0.85:
                            print(f"    → Size-only fallback: ratio {ratio:.4f} < 0.85")
                        elif ratio < 0.98 and abs(shift) > 1.0:
                            print(f"    → Very aggressive: ratio {ratio:.4f} < 0.98 AND shift {abs(shift):.4f} > 1.0")
                    elif pos == "sub":
                        if ratio < 0.95 and shift > baseline_threshold:
                            print(f"    → Size ratio {ratio:.4f} < 0.95 AND shift {shift:.4f} > {baseline_threshold:.4f}")
                    else:
                        print(f"    → Normal text")
                    
                    print("\n" + "-"*40)
                    
                    if char_index is not None:
                        doc.close()
                        return
    
    doc.close()
    print(f"\nTotal characters scanned: {char_count}")


def run_comprehensive_test(pdf_path, page_num=0):
    """
    Run a comprehensive test showing all detection results.
    """
    print(f"\n{'='*80}")
    print(f"🧪 COMPREHENSIVE FORMATTING DETECTION TEST")
    print(f"{'='*80}\n")
    
    # Test 1: Full page detection
    test_pdf_formatting(pdf_path, page_num)
    
    # Test 2: Show first 10 characters with detailed analysis
    for i in range(1, 11):
        test_single_character_detection(pdf_path, page_num, i)
        print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_formatting.py <pdf_path> [page_number]")
        print("  pdf_path: Path to the PDF file")
        print("  page_number: Page number to test (default: 1)")
        print("\nExamples:")
        print("  python test_formatting.py my_document.pdf")
        print("  python test_formatting.py my_document.pdf 3")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    page_num = int(sys.argv[2]) - 1 if len(sys.argv) > 2 else 0
    
    # Run the comprehensive test
    run_comprehensive_test(pdf_path, page_num)