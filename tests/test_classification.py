from viva.classification import ClassificationProvider, NullClassificationProvider


def test_null_provider_always_returns_none():
    provider = NullClassificationProvider()
    assert provider.classify("q1", "some answer") is None
    assert provider.classify("q2", "") is None


def test_null_provider_is_a_classification_provider():
    assert isinstance(NullClassificationProvider(), ClassificationProvider)


def test_classification_provider_is_abstract():
    import pytest

    with pytest.raises(TypeError):
        ClassificationProvider()  # can't instantiate the ABC directly
