# Fixed public pilot: 25 decisions

These are the 25 public ANON decisions collected and verified on 27 October
2026, with 58 original text passages. Input SHA-256:
`580fa3b6b831e7328b50a9e435d940fe94b3902856240b85f2260b11638d2635`.

Source: Tražilica odluka sudova Republike Hrvatske,
https://odluke.sudovi.hr/. Each record includes its original source URL.
Independent, unofficial project. Terms: https://odluke.sudovi.hr/Home/Privacy.

This fixed input is used for an explicitly requested vectorization test.
The workflow does not crawl ANON and does not read Drive or the live database.
The SQLite checkpoint, runtime paths, private matter files and mappings are
not part of the input. This sample is not representative of the whole corpus.

The ONNX workflow splits each source passage into overlapping token windows
of at most 128 tokens, rather than silently discarding its ending. It uses
the same pinned CPU encoder for indexing and local queries. The downloadable
artifact includes the tokenizer, quantized model, FAISS index, normalized
vectors, source-linked segments, integrity manifest and three query probes.
Artifacts expire after seven days; downloaded local copies remain available.
