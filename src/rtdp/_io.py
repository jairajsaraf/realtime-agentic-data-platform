"""Cross-platform PyArrow FileIO for pyiceberg.

pyiceberg's ``parse_location`` returns a leading-slash path (``/C:/...``) for ``file://``
URIs, which pyarrow's ``LocalFileSystem`` rejects on Windows (WinError 123). This subclass
strips that leading slash before a drive letter for the ``file`` scheme on Windows only.
It is a no-op on POSIX and for non-``file`` schemes (e.g. ``s3``), so it is safe to use as
the FileIO for every backend. Wired in via the ``py-io-impl`` catalog property.
"""

from __future__ import annotations

import os
import re

from pyiceberg.io.pyarrow import PyArrowFileIO

_WIN_DRIVE = re.compile(r"^/[A-Za-z]:/")


class CrossPlatformPyArrowFileIO(PyArrowFileIO):
    @staticmethod
    def parse_location(location: str, properties=None) -> tuple[str, str, str]:
        scheme, netloc, path = PyArrowFileIO.parse_location(location, properties or {})
        if scheme == "file" and os.name == "nt" and _WIN_DRIVE.match(path):
            path = path[1:]  # "/C:/warehouse/..." -> "C:/warehouse/..."
        return scheme, netloc, path
