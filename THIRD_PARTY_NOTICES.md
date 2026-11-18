# Third-party notices

This repository is released under the [MIT License](LICENSE). It also redistributes two Grafana dashboards from
other projects. They are fetched at pinned revisions by `monitoring/grafana/vendor-dashboards.py`, and they keep
their own licenses.

Container images named in the Compose files are pulled from their publishers when you deploy. They are not part of
this repository and come under their own licenses.

## DGX Spark vLLM dashboard

- File: `monitoring/grafana/dashboards/dgx-spark-vllm.json`
- Source: <https://github.com/RodriMora/dgx-spark-grafana-dashboard> at commit
  `0ea065652f861d4e7a7156bae2ee084ab491a2e8`
- License: MIT
- Changes made here: a `model` template variable added and injected into every `vllm:*` query; uid, title and tags
  set; numeric id removed.

```text
MIT License

Copyright (c) 2025 RodriMora

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

## Node Exporter Full

- File: `monitoring/grafana/dashboards/node-exporter-full.json`
- Source: Grafana.com dashboard [1860](https://grafana.com/grafana/dashboards/1860), revision 45, by rfmoz,
  <https://github.com/rfmoz/grafana-dashboards>
- License: Apache License 2.0; the full text is in [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt). The source
  project ships no NOTICE file.
- Changes made here: numeric id removed.
