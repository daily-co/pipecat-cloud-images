# Third-party software in `dailyco/pipecat-base`

This notice describes `dailyco/pipecat-base` as published (the `IMAGE_VERSION`
environment variable gives the version). An image built from it may add
software this notice does not describe.

The image is built on `ghcr.io/astral-sh/uv:python<version>-trixie-slim`,
which is Debian 13 ("trixie") with Python and uv installed. The third-party
software it contains is unmodified and remains under its own licenses:

- **Debian packages.** Each package's copyright and license terms are in
  `/usr/share/doc/<package>/copyright`. `dpkg-query -W` lists the installed
  packages and versions, and Debian publishes the source for every version at
  https://snapshot.debian.org and https://sources.debian.org.
- **Python.** Python Software Foundation License, in
  `/usr/local/lib/python<version>/LICENSE.txt`.
- **uv.** Apache-2.0 or MIT; both license texts are in
  `/usr/share/doc/pipecat-base/`. Source at https://github.com/astral-sh/uv.
- **Python packages.** Installed in `/app/.venv`. Each package's license is in
  its `*.dist-info` directory under
  `/app/.venv/lib/python<version>/site-packages`.
- **NLTK data.** The `punkt_tab` tokenizer data is in
  `/usr/local/share/nltk_data`. The NLTK project distributes it separately
  from the `nltk` package, under the terms listed for it at
  https://www.nltk.org/nltk_data/.
