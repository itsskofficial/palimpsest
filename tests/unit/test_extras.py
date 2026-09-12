

def test_the_dev_extra_installs_everything_the_tests_reach_for():
    """A skipped test on CI is worse than a failing one.

    `pytest.importorskip` is the right guard for an optional dependency, and it turns a
    missing one into a green run that quietly covered nothing — the PDF and spreadsheet
    adapters skipped on CI for exactly that reason while passing locally. So every
    optional package a test imports has to be in `[dev]`, and that is checked here rather
    than remembered.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    # The closing bracket is the one at the start of a line: `"uvicorn[standard]>=0.29"`
    # contains a `]` of its own, and slicing to the first one truncates the block three
    # entries early — which made this test's first run report a package that was there.
    dev = pyproject[pyproject.index("dev = ["):]
    dev = dev[:dev.index(chr(10) + "]")]

    wanted = set()
    for path in (root / "tests").rglob("test_*.py"):
        source = path.read_text(encoding="utf-8")
        wanted.update(re.findall(r'importorskip\(\s*"([a-z0-9_.]+)"', source))

    #: Import names that differ from their distribution name, plus the ones a test may
    #: legitimately guard on because they are genuinely not ours to require.
    aliases = {"playwright.sync_api": "playwright"}
    optional = {"playwright"}      # browser tests are opt-in and not part of `[dev]`

    missing = []
    for name in sorted(wanted):
        distribution = aliases.get(name, name)
        if distribution in optional:
            continue
        if not re.search(rf'"{re.escape(distribution)}[><=~ ]', dev):
            missing.append(distribution)

    assert not missing, (
        f"tests importorskip {missing}, which `[dev]` does not install — so those tests "
        "skip on CI and the run is green without having covered anything.")
