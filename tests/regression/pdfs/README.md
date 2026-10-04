# Supplied regression PDFs

Put real PDFs/pages here (for example the page whose underlines the old tool
missed) together with a sidecar `<name>.expected.json`:

```json
{
  "underlines": [
    {"page": 1, "line": "exact text of the visual line", "ranges": [[start, end]]}
  ],
  "strikes": []
}
```

`ranges` are half-open character offsets into `line`. The test
`tests/test_auto_zone_regression.py::test_supplied_pdfs` locates the line and
checks that the engine finds exactly these ranges on its own — no words or
coordinates are given to the engine.
