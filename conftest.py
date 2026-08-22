"""pytest_plugins is only honoured in the rootdir conftest, and the integration
tests need pytester to run a real pytest in a subprocess."""

pytest_plugins = ["pytester"]
