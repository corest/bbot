# Live Update: http_proxy_exclude (NO_PROXY support)

Patch for bbot **2.8.4** to add proxy exclusion support. Allows excluding specific hosts/CIDRs from `web.http_proxy`, so internal services (e.g. Elasticsearch output) can be reached directly while scanning traffic still goes through the proxy.

## What changed

| File | Change |
|------|--------|
| `bbot/defaults.yml` | Added `http_proxy_exclude: []` config option |
| `bbot/scanner/preset/args.py` | Added `--no-proxy` CLI argument |
| `bbot/scanner/scanner.py` | Exposed `http_proxy_exclude` property |
| `bbot/scanner/preset/environ.py` | Sets `NO_PROXY` env var for external tools |
| `bbot/core/helpers/web/engine.py` | Dual-client strategy: proxy-free client for excluded hosts |
| `bbot/core/helpers/web/proxy_utils.py` | **New file** — `should_bypass_proxy()` matching utility |

## Ansible deployment

### Directory structure

```
roles/bbot_noproxy_patch/
  files/
    no_proxy.patch
    proxy_utils.py
  tasks/
    main.yml
```

### tasks/main.yml

```yaml
---
- name: Get bbot package path
  command: "{{ bbot_python | default('python3') }} -c \"import bbot; import os; print(os.path.dirname(bbot.__path__[0]))\""
  register: bbot_site_packages
  changed_when: false

- name: Set bbot_path fact
  set_fact:
    bbot_path: "{{ bbot_site_packages.stdout }}/bbot"

- name: Copy proxy_utils.py
  copy:
    src: proxy_utils.py
    dest: "{{ bbot_path }}/core/helpers/web/proxy_utils.py"
    mode: "0644"

- name: Apply no_proxy patch
  ansible.posix.patch:
    src: no_proxy.patch
    basedir: "{{ bbot_path }}/.."
    strip: 1
    state: present

- name: Clear __pycache__ for patched modules
  command: find {{ bbot_path }} -name '__pycache__' -exec rm -rf {} +
  changed_when: true
```

### files/proxy_utils.py

Copy from `bbot/core/helpers/web/proxy_utils.py` in this repo.

### files/no_proxy.patch

Generate with:

```bash
git diff HEAD -- \
  bbot/core/helpers/web/engine.py \
  bbot/defaults.yml \
  bbot/scanner/preset/args.py \
  bbot/scanner/preset/environ.py \
  bbot/scanner/scanner.py \
  > roles/bbot_noproxy_patch/files/no_proxy.patch
```

Or save the following patch directly:

```diff
diff --git a/bbot/core/helpers/web/engine.py b/bbot/core/helpers/web/engine.py
index 1c3ecc0f5..fc06ee03d 100644
--- a/bbot/core/helpers/web/engine.py
+++ b/bbot/core/helpers/web/engine.py
@@ -36,6 +36,15 @@ class HTTPEngine(EngineServer):
         self.web_clients = {}
         self.web_client = self.AsyncClient(persist_cookies=False)

+        # proxy exclusion support
+        self.proxy_exclusions = self.web_config.get("http_proxy_exclude", [])
+        self.has_proxy = bool(self.web_config.get("http_proxy", ""))
+        self.noproxy_web_clients = {}
+        if self.has_proxy and self.proxy_exclusions:
+            self.noproxy_web_client = self._AsyncClient_noproxy(persist_cookies=False)
+        else:
+            self.noproxy_web_client = None
+
     def AsyncClient(self, *args, **kwargs):
         # cache by retries to prevent unwanted accumulation of clients
         # (they are not garbage-collected)
@@ -49,12 +58,43 @@ class HTTPEngine(EngineServer):
             self.web_clients[client.retries] = client
             return client

+    def _AsyncClient_noproxy(self, *args, **kwargs):
+        """Create/cache a BBOTAsyncClient with proxy disabled, for excluded hosts."""
+        retries = kwargs.get("retries", 1)
+        try:
+            return self.noproxy_web_clients[retries]
+        except KeyError:
+            from .client import BBOTAsyncClient
+
+            noproxy_config = dict(self.config)
+            noproxy_web = dict(noproxy_config.get("web", {}))
+            noproxy_web["http_proxy"] = ""
+            noproxy_config["web"] = noproxy_web
+            client = BBOTAsyncClient.from_config(noproxy_config, self.target, *args, **kwargs)
+            self.noproxy_web_clients[client.retries] = client
+            return client
+
+    def _get_client_for_url(self, url, client=None):
+        """Return the appropriate client based on proxy exclusion rules.
+
+        If no explicit client is provided and the URL matches an exclusion pattern,
+        returns the no-proxy client. Otherwise returns the given client or default.
+        """
+        if client is not None:
+            return client
+        if self.noproxy_web_client is not None and url:
+            from .proxy_utils import should_bypass_proxy
+
+            if should_bypass_proxy(url, self.proxy_exclusions):
+                return self.noproxy_web_client
+        return self.web_client
+
     async def request(self, *args, **kwargs):
         raise_error = kwargs.pop("raise_error", False)
         # TODO: use this
         cache_for = kwargs.pop("cache_for", None)  # noqa

-        client = kwargs.get("client", self.web_client)
+        explicit_client = kwargs.pop("client", None)

         # allow vs follow, httpx why??
         allow_redirects = kwargs.pop("allow_redirects", None)
@@ -79,6 +119,8 @@ class HTTPEngine(EngineServer):

         if client_kwargs:
             client = self.AsyncClient(**client_kwargs)
+        else:
+            client = self._get_client_for_url(url, explicit_client)

         try:
             async with self._acatch(url, raise_error):
@@ -144,7 +186,8 @@ class HTTPEngine(EngineServer):
             chunk_size = 8192
             chunks = []

-            async with self._acatch(url, raise_error=True), self.web_client.stream(url=url, **kwargs) as response:
+            stream_client = self._get_client_for_url(url)
+            async with self._acatch(url, raise_error=True), stream_client.stream(url=url, **kwargs) as response:
                 agen = response.aiter_bytes(chunk_size=chunk_size)
                 async for chunk in agen:
                     _chunk_size = len(chunk)
diff --git a/bbot/defaults.yml b/bbot/defaults.yml
index 64614d08e..8688b0405 100644
--- a/bbot/defaults.yml
+++ b/bbot/defaults.yml
@@ -75,6 +75,9 @@ dns:
 web:
   # HTTP proxy
   http_proxy:
+  # Hosts/CIDRs to exclude from HTTP proxy (NO_PROXY equivalent)
+  # Examples: ["localhost", "*.internal.corp", "10.0.0.0/8", "elastic.mycompany.com"]
+  http_proxy_exclude: []
   # Web user-agent
   user_agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.2151.97
   # Set the maximum number of HTTP links that can be followed in a row (0 == no spidering allowed)
diff --git a/bbot/scanner/preset/args.py b/bbot/scanner/preset/args.py
index b018fe395..4342674e3 100644
--- a/bbot/scanner/preset/args.py
+++ b/bbot/scanner/preset/args.py
@@ -174,6 +174,9 @@ class BBOTArgs:
         if self.parsed.proxy:
             args_preset.core.merge_custom({"web": {"http_proxy": self.parsed.proxy}})

+        if self.parsed.no_proxy:
+            args_preset.core.merge_custom({"web": {"http_proxy_exclude": self.parsed.no_proxy}})
+
         if self.parsed.custom_headers:
             args_preset.core.merge_custom({"web": {"http_headers": self.parsed.custom_headers}})

@@ -380,6 +383,13 @@ class BBOTArgs:
         misc = p.add_argument_group(title="Misc")
         misc.add_argument("--version", action="store_true", help="show BBOT version and exit")
         misc.add_argument("--proxy", help="Use this proxy for all HTTP requests", metavar="HTTP_PROXY")
+        misc.add_argument(
+            "--no-proxy",
+            nargs="+",
+            default=[],
+            help="Exclude these hosts from proxy (e.g. localhost *.internal.corp 10.0.0.0/8)",
+            metavar="HOST",
+        )
         misc.add_argument(
             "-H",
             "--custom-headers",
diff --git a/bbot/scanner/preset/environ.py b/bbot/scanner/preset/environ.py
index a222dd1bb..2a833d009 100644
--- a/bbot/scanner/preset/environ.py
+++ b/bbot/scanner/preset/environ.py
@@ -125,6 +125,13 @@ class BBOTEnviron:
             environ.pop("HTTP_PROXY", None)
             environ.pop("HTTPS_PROXY", None)

+        # handle proxy exclusions (NO_PROXY)
+        http_proxy_exclude = self.preset.config.get("web", {}).get("http_proxy_exclude", [])
+        if http_proxy_exclude:
+            environ["NO_PROXY"] = ",".join(str(x) for x in http_proxy_exclude)
+        else:
+            environ.pop("NO_PROXY", None)
+
         # ssl verification
         import urllib3

diff --git a/bbot/scanner/scanner.py b/bbot/scanner/scanner.py
index 67c09ea38..fd3f78189 100644
--- a/bbot/scanner/scanner.py
+++ b/bbot/scanner/scanner.py
@@ -211,6 +211,7 @@ class Scanner:
         max_redirects = self.web_config.get("http_max_redirects", 5)
         self.web_max_redirects = max(max_redirects, self.web_spider_distance)
         self.http_proxy = self.web_config.get("http_proxy", "")
+        self.http_proxy_exclude = self.web_config.get("http_proxy_exclude", [])
         self.http_timeout = self.web_config.get("http_timeout", 10)
         self.httpx_timeout = self.web_config.get("httpx_timeout", 5)
         self.http_retries = self.web_config.get("http_retries", 1)
```

## Usage after patching

### Config file (`~/.config/bbot/bbot.yml`)

```yaml
web:
  http_proxy: http://your-proxy:8080
  http_proxy_exclude:
    - elastic.internal
    - "10.0.0.0/8"
    - "*.mycompany.com"
    - localhost
```

### CLI

```bash
bbot -t example.com \
  --proxy http://your-proxy:8080 \
  --no-proxy elastic.internal 10.0.0.0/8 "*.mycompany.com"
```

### Preset YAML

```yaml
config:
  web:
    http_proxy: http://your-proxy:8080
    http_proxy_exclude:
      - elastic.internal
      - "10.0.0.0/8"
```

## Supported exclusion patterns

| Pattern | Matches |
|---------|---------|
| `elastic.internal` | Exact hostname + subdomains (`sub.elastic.internal`) |
| `.internal.corp` | Any subdomain of `internal.corp` |
| `*.internal.corp` | Wildcard — same as above |
| `10.0.0.0/8` | Any IP in CIDR range (URL must use IP, not hostname) |
| `192.168.1.100` | Exact IP match |
| `*` | Bypass proxy for all requests |

## Notes

- Patch targets bbot **2.8.4**. Re-apply or verify after upgrading.
- No new pip dependencies — uses only Python stdlib (`ipaddress`, `fnmatch`, `urllib.parse`).
- Clearing `__pycache__` after patching is required so Python loads the updated `.py` files.
- External tools (nuclei, ffuf, httpx, gowitness, wpscan) get `NO_PROXY` via environment variable automatically.
- External tools invoked with explicit `--proxy` flags (e.g. nuclei) will still proxy all their requests. This is correct behavior since those tools scan targets, not internal infrastructure.
