def test_document_pipeline_imports_lib_metadata() -> None:
    import inspect

    import yokaze.pipeline.document as doc

    source = inspect.getsource(doc)
    assert "yokaze.lib.metadata" in source
    assert "yokaze.lab.crop.eval.metadata" not in source
