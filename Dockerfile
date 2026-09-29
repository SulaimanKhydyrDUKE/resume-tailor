# resume-tailor: Python 3.12 + Playwright's Chromium, for the CLI, the MCP
# server and the test scripts. Personal data (~/.resume-tailor) and outputs
# are mounted, never baked in.
# The noble image (Ubuntu 24.04) carries Python 3.12; jammy's 3.10 is below
# the package's floor of 3.11. The image's browsers belong to one Playwright
# release, so the pip package is pinned to the same one (uv.lock's version).
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e . "playwright==1.63.0"

# The profile directory (career.yaml, answers.yaml, env) and the output
# directory are volumes: docker run -v ~/.resume-tailor:/root/.resume-tailor -v $PWD/output:/app/output …
VOLUME ["/root/.resume-tailor", "/app/output"]
COPY tests ./tests
COPY profile ./profile

CMD ["resume-tailor", "--help"]
