
from tool import run

def test_run_returns_headlines():
    result = run({"url": "https://news.example.com"})
    assert "headlines" in result
    assert len(result["headlines"]) == 3

def test_run_returns_expected_format():
    result = run({"url": "https://news.example.com"})
    assert isinstance(result["headlines"], list)

def test_run_with_different_url():
    result = run({"url": "https://other.news.example.com"})
    assert "headlines" in result
