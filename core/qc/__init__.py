"""PDF <-> XHTML production QC engine.

    package_manager  - THE gateway for every structural EPUB change (splits,
                       manifest, spine, nav, NCX, links, IDs, undo/redo)
    pdf_model        - PDF pages: words, paragraphs, printed page numbers,
                       images, captions (cached per page)
    xhtml_model      - XHTML splits in spine order: blocks, words, page
                       markers, images, captions, ids, links (cached per split)
    mapping          - ONE unified PDF <-> XHTML mapping + difference model
    corrections      - suggested / automatic corrections and user decisions
    reports          - JSON / HTML / CSV / PDF reports
    final_validation - pre-export validation and final EPUB build
"""
