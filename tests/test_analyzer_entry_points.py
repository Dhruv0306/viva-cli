from __future__ import annotations

from viva.analyzer.entry_points import detect_entry_points
from viva.ingest.models import SampledFile


def _sf(path: str) -> SampledFile:
    return SampledFile(path=path, size_bytes=100, module=path.split("/")[0] if "/" in path else "")


def test_detects_common_entry_point_names():
    files = [_sf("src/app/main.py"), _sf("src/app/handlers.py"), _sf("README.md")]
    assert detect_entry_points(files, ["python"]) == ["src/app/main.py"]


def test_no_entry_points_returns_empty_list():
    files = [_sf("src/app/handlers.py"), _sf("src/app/db.py")]
    assert detect_entry_points(files, ["python"]) == []


def test_matches_are_case_insensitive_and_deduped_and_sorted():
    files = [_sf("b/INDEX.JS"), _sf("a/main.go"), _sf("a/main.go")]
    assert detect_entry_points(files, []) == ["a/main.go", "b/INDEX.JS"]


def test_multiple_entry_points_across_modules():
    files = [_sf("api/main.py"), _sf("worker/main.py"), _sf("api/utils.py")]
    assert detect_entry_points(files, []) == ["api/main.py", "worker/main.py"]


def test_spring_boot_project_named_application_class_detected():
    # Regression test: surfaced against a real Spring Boot repo
    # (URL-Shortener-Using-REST-API) whose entry point is
    # UrlShortenerApplication.java -- Spring Boot's actual naming
    # convention names the class after the project, not literally
    # "Application.java", so only a suffix match catches it.
    files = [
        _sf("src/main/java/com/urlshortener/urlshortener/UrlShortenerApplication.java"),
        _sf("src/main/java/com/urlshortener/urlshortener/controller/UrlShortenerController.java"),
    ]
    assert detect_entry_points(files, ["java"]) == [
        "src/main/java/com/urlshortener/urlshortener/UrlShortenerApplication.java"
    ]


def test_literal_application_java_still_matches():
    files = [_sf("src/Application.java")]
    assert detect_entry_points(files, ["java"]) == ["src/Application.java"]


def test_class_merely_containing_application_substring_not_at_suffix_not_matched():
    # "ApplicationConfig.java" doesn't *end* with "application.java", so
    # it must not match -- the suffix check should be precise, not a
    # loose substring search that could false-positive on any
    # Application-related class.
    files = [_sf("src/ApplicationConfig.java")]
    assert detect_entry_points(files, ["java"]) == []
