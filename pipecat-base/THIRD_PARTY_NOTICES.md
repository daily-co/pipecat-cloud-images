# Third-party software in `dailyco/pipecat-base`

This image is built on `ghcr.io/astral-sh/uv:python<version>-trixie-slim`,
which is Debian 13 ("trixie") with Python and uv installed. The third-party
software it contains is unmodified and remains under its own licenses:

- **Debian packages.** Each package's copyright and license terms are in
  `/usr/share/doc/<package>/copyright`. `dpkg-query -W` lists the installed
  packages and versions, and Debian publishes the source for every version at
  https://snapshot.debian.org and https://sources.debian.org.
- **Python.** Python Software Foundation License, in
  `/usr/local/lib/python<version>/LICENSE.txt`.
- **uv.** Apache-2.0 or MIT, at https://github.com/astral-sh/uv.
- **Python packages.** Installed in `/app/.venv`. Each package's license is in
  its `*.dist-info` directory under
  `/app/.venv/lib/python<version>/site-packages`.
