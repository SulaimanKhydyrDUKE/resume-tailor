from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("resume-tailor")
except PackageNotFoundError:  # running from a checkout without an install
    __version__ = "0.1.0"
