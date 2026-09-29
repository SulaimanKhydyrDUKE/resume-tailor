# resume-tailor: Python 3.11 + Playwright's Chromium, for the CLI, the MCP
# server and the test scripts. Personal data (~/.resume-tailor) and outputs
# are mounted, never baked in.
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .

# The profile directory (career.yaml, answers.yaml, env) and the output
# directory are volumes: docker run -v ~/.resume-tailor:/root/.resume-tailor -v $PWD/output:/app/output …
VOLUME ["/root/.resume-tailor", "/app/output"]
COPY tests ./tests
COPY profile ./profile

CMD ["resume-tailor", "--help"]
