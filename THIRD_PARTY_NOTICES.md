# Third-Party Notices

Aegis Agent is built by extracting, simplifying, and modularizing the core
runtime behaviour of **Hermes** (`hermes-agent`).  This file records the
third-party code and licences that Aegis Agent incorporates or derives from.

---

## Hermes (hermes-agent)

- **Upstream:** Nous Research — `hermes-agent`
- **License:** MIT
- **Copyright:** © 2025 Nous Research

Portions of Aegis Agent are **adapted from** Hermes (marked in source files
with an attribution header) and other portions are **clean rewrites that
reference Hermes' observable behaviour**.  The mapping of which Aegis modules
derive from which Hermes sources is maintained in
[`docs/source-map.md`](docs/source-map.md).  Aegis Agent does **not** claim
adapted Hermes code as wholly original.

The full text of the Hermes MIT license is reproduced below:

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Runtime dependencies

Aegis Agent's direct runtime dependencies (see `pyproject.toml`) and their
licences:

| Package   | License  |
|-----------|----------|
| openai    | MIT      |
| typer     | MIT      |
| rich      | MIT      |
| pydantic  | MIT      |
| pyfiglet  | MIT      |
| pyyaml    | MIT      |
| mcp (optional) | MIT      |
| prompt_toolkit | BSD-3-Clause |
| wcwidth   | MIT      |

Development-only dependencies (pytest, ruff, mypy, pytest-asyncio) are not
distributed with the runtime and are listed under `[dependency-groups]`.
