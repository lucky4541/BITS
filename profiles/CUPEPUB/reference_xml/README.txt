Reference XML corpus for the CUPEPUB Auto Tag engine (core/tag_knowledge).

Place approved reference XML or final XHTML files of the project here (any
*.xml / *.xhtml / *.html). They are analysed automatically when the Tag
Knowledge Model is built: element frequencies, reading-order succession,
parent/child patterns, attributes and text patterns. Final XHTML is mapped
back to zone tags through Mapping.xml (e.g. <p class="poemline"> -> poemline).

A project DTD (*.dtd) placed in profiles/CUPEPUB/ is parsed the same way and
its content models / required attributes constrain Auto Tag decisions.
Additional folders and DTDs can also be loaded from the app:
File > Auto Zone / Auto Tag (CUPEPUB) > Load Reference XML Corpus / Load Project DTD.
