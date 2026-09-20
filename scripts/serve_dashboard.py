#!/usr/bin/env python3
"""Sentinel dashboard static HTTP server (default :8765).

Serves the repository root so /output/sentinel_dashboard.html is reachable.
Virtual mount: /assets/7000/* -> ASSETS_7000_DIR env var (Docker-portable).
Virtual mount: /assets/1-10/* and /assets/video/* -> ASSETS_1_10_DIR (Q-Flex REAL VIDEO).
Virtual mount: /assets/arctic/* -> ASSETS_ARCTIC_DIR (Arc7 flight video + video-derived frames).

Modes:
  default  — serve output/ (+ assets virtual mounts)
  --dev    — on each /output/js|css request, sync from web/ (or serve web source live)
             Cache-Control: no-store for JS/CSS to prevent stale browser cache

Asset resolution order for /assets/7000:
  1. ASSETS_7000_DIR env var
  2. ./assets/7000 relative to repo root
  3. ~/OneDrive/Desktop/7000 or ~/Desktop/7000 (local fallback)

Asset resolution order for /assets/1-10 (and /assets/video alias):
  1. ASSETS_1_10_DIR env var
  2. C:\\111\\1001\\1-10 (Windows host SoT)
  3. ./assets/1-10 relative to repo root

Asset resolution order for /assets/arctic:
  1. ASSETS_ARCTIC_DIR env var
  2. ./assets/arctic relative to repo root
  3. ./output/assets/arctic relative to repo root
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.web_assets_sync import ensure_file_synced, sync_web_assets  # noqa: E402
from services.ais_health import build_health_document  # noqa: E402


def _resolve_assets_dir() -> Path:
    env_path = os.environ.get("ASSETS_7000_DIR", "").strip()
    if env_path and (env_path[1:3] == ":\\" or env_path[1:3] == ":/" or env_path.startswith("\\\\")):
        if Path("/.dockerenv").exists() or os.environ.get("APP_HOME"):
            env_path = ""
    if env_path:
        return Path(env_path)
    repo_relative = ROOT / "assets" / "7000"
    if repo_relative.exists() or os.environ.get("APP_HOME"):
        return repo_relative
    home = Path.home()
    for cand in (
        home / "OneDrive" / "Desktop" / "7000",
        home / "Desktop" / "7000",
    ):
        if cand.exists():
            return cand
    return repo_relative


DESKTOP_7000 = _resolve_assets_dir()


def _resolve_qflex_1_10_dir() -> Path:
    env_path = os.environ.get("ASSETS_1_10_DIR", "").strip()
    in_docker = bool(Path("/.dockerenv").exists() or os.environ.get("APP_HOME"))
    if env_path:
        if in_docker and (env_path[1:3] in (":\\", ":/") or env_path.startswith("\\\\")):
            env_path = ""
        else:
            return Path(env_path)
    if in_docker:
        docker_path = Path("/app/assets/1-10")
        if docker_path.is_dir():
            return docker_path
    host = Path(r"C:\111\1001\1-10")
    if host.is_dir():
        return host
    return ROOT / "assets" / "1-10"


QFLEX_1_10 = _resolve_qflex_1_10_dir()


def _resolve_arctic_dir() -> Path:
    env_path = os.environ.get("ASSETS_ARCTIC_DIR", "").strip()
    in_docker = bool(Path("/.dockerenv").exists() or os.environ.get("APP_HOME"))
    if env_path:
        if in_docker and (env_path[1:3] in (":\\", ":/") or env_path.startswith("\\\\")):
            env_path = ""
        else:
            return Path(env_path)
    if in_docker:
        docker_path = Path("/app/assets/arctic")
        if docker_path.is_dir():
            return docker_path
    repo = ROOT / "assets" / "arctic"
    if repo.is_dir():
        return repo
    return ROOT / "output" / "assets" / "arctic"


ARCTIC_ASSETS = _resolve_arctic_dir()
DEFAULT_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0").strip() or "0.0.0.0"
try:
    from services.utils.canonical_port import assert_canonical_dashboard_port, resolve_dashboard_port

    DEFAULT_PORT = assert_canonical_dashboard_port(
        resolve_dashboard_port(), context="serve_dashboard.env"
    )
except SystemExit:
    raise
except Exception:
    DEFAULT_PORT = 8765
ASSETS_7000_PREFIX = "/assets/7000/"
ASSETS_1_10_PREFIXES = ("/assets/1-10/", "/assets/video/")
ASSETS_ARCTIC_PREFIX = "/assets/arctic/"


def _sheet_url(port: int, sheet: str) -> str:
    return f"http://127.0.0.1:{port}/output/sentinel_dashboard.html?sheet={sheet}"


# Module-level toggle set from CLI (--dev)
DEV_MODE = False


def _content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".js":
        return "application/javascript; charset=utf-8"
    if suffix == ".mjs":
        return "application/javascript; charset=utf-8"
    if suffix == ".css":
        return "text/css; charset=utf-8"
    if suffix == ".json":
        return "application/json; charset=utf-8"
    if suffix == ".html":
        return "text/html; charset=utf-8"
    if suffix == ".glb":
        return "model/gltf-binary"
    if suffix == ".gltf":
        return "model/gltf+json"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".webm":
        return "video/webm"
    guessed = mimetypes.guess_type(str(path))[0]
    return guessed or "application/octet-stream"


class _ReuseHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        # Dynamic CORS for dashboard + module scripts
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, X-Sentinel-Admin-Token")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _send_file(self, path: Path, *, no_store: bool = False) -> None:
        if not path.is_file():
            self.send_error(404, f"File not found: {path.name}")
            return
        ctype = _content_type_for(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            self.send_error(500, str(exc))
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if no_store or path.suffix.lower() in {".js", ".mjs", ".css", ".glb", ".gltf", ".html", ".json"} or (
            DEV_MODE and path.suffix.lower() in {".js", ".mjs", ".css"}
        ):
            self.send_header(
                "Cache-Control",
                "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0",
            )
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        else:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_desktop_7000(self) -> bool:
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "")
        if not path.startswith(ASSETS_7000_PREFIX):
            return False
        rel = path[len(ASSETS_7000_PREFIX) :].lstrip("/").replace("\\", "/")
        if not rel or ".." in rel.split("/"):
            self.send_error(400, "Invalid asset path")
            return True
        target = (DESKTOP_7000 / rel).resolve()
        try:
            target.relative_to(DESKTOP_7000.resolve())
        except ValueError:
            self.send_error(403, "Forbidden")
            return True
        self._send_file(target)
        return True

    def _serve_qflex_1_10(self) -> bool:
        """Serve Q-Flex REAL VIDEO MP4s from C:\\111\\1001\\1-10 (or ASSETS_1_10_DIR)."""
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "")
        prefix = next((p for p in ASSETS_1_10_PREFIXES if path.startswith(p)), None)
        if not prefix:
            return False
        rel = path[len(prefix) :].lstrip("/").replace("\\", "/")
        if not rel or ".." in rel.split("/"):
            self.send_error(400, "Invalid video path")
            return True
        base = QFLEX_1_10
        if not base.exists():
            self.send_error(404, f"Q-Flex video root missing: {base}")
            return True
        target = (base / rel).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            self.send_error(403, "Forbidden")
            return True
        self._send_file(target)
        return True

    def _serve_arctic_assets(self) -> bool:
        """Serve Arc7 ARCTIC flight videos + video-derived frames."""
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "")
        if not path.startswith(ASSETS_ARCTIC_PREFIX):
            return False
        rel = path[len(ASSETS_ARCTIC_PREFIX) :].lstrip("/").replace("\\", "/")
        if not rel or ".." in rel.split("/"):
            self.send_error(400, "Invalid arctic asset path")
            return True
        bases = [ARCTIC_ASSETS, ROOT / "assets" / "arctic", ROOT / "output" / "assets" / "arctic"]
        for base in bases:
            if not base.exists():
                continue
            target = (base / rel).resolve()
            try:
                target.relative_to(base.resolve())
            except ValueError:
                continue
            if target.is_file():
                self._send_file(target)
                return True
        self.send_error(404, f"Arctic asset missing: {rel}")
        return True

    def _dev_serve_or_sync(self) -> bool:
        """In --dev: sync-check output/js|css from web/, prefer live web source bytes."""
        if not DEV_MODE:
            return False
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "").lstrip("/")
        if not (path.startswith("output/js/") or path.startswith("output/css/")):
            return False
        if ".." in path.split("/"):
            self.send_error(400, "Invalid path")
            return True
        live = ensure_file_synced(ROOT, path)
        serve_path = live if live is not None else (ROOT / path)
        self._send_file(serve_path, no_store=True)
        return True

    def _serve_live_health(self) -> bool:
        """Dynamic Truth Contract health — always HTTP 200 on dashboard port."""
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "")
        if path not in (
            "/output/api/v1/health",
            "/output/api/v1/health/",
            "/output/api/v1/health.json",
            "/api/v1/health",
            "/api/v1/health/",
        ):
            return False
        try:
            doc = build_health_document()
            raw = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raw = json.dumps(
                {"service": "sentinel_dashboard", "status": "error", "error": str(exc), "http": "200"},
                indent=2,
            ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header(
            "Cache-Control",
            "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0",
        )
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
        return True

    def _client_is_local(self) -> bool:
        host = (self.client_address[0] if self.client_address else "") or ""
        return host in {"127.0.0.1", "::1", "localhost"}

    def _json_response(self, code: int, payload: dict, *, contract_header: bool = False) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header(
            "Cache-Control",
            "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0",
        )
        if contract_header:
            try:
                from services.compressor_stations import CONTRACT_VERSION

                self.send_header("X-Contract-Version", CONTRACT_VERSION)
            except Exception:  # noqa: BLE001
                self.send_header("X-Contract-Version", "1.8.0-ops-gis-sot")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _serve_key_status(self) -> bool:
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "")
        if path not in (
            "/api/config/key-status",
            "/api/config/key-status/",
            "/output/api/config/key-status",
        ):
            return False
        try:
            from services.key_manager import key_status_public

            self._json_response(200, key_status_public())
        except Exception as exc:  # noqa: BLE001
            self._json_response(500, {"ok": False, "error": str(exc)})
        return True

    def _serve_quant_risk(self) -> bool:
        """Serve /api/v1/quant/risk (and /output/api/v1/quant/risk).

        Reverse-proxies to internal api_server on port 8766 (if available).
        Falls back to in-process deterministic quant computation.
        Guarantees 200 OK and eliminates 404 on the canonical edge port 8765.
        """
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "").rstrip("/")
        if path not in (
            "/api/v1/quant/risk",
            "/output/api/v1/quant/risk",
        ):
            return False

        from urllib.parse import parse_qs
        qs = parse_qs(parsed.query or "")
        horizon = 7
        try:
            if "horizon" in qs:
                horizon = int(qs["horizon"][0])
        except (ValueError, TypeError, IndexError):
            horizon = 7

        # 1. Attempt reverse proxy to internal api_server (port 8766)
        internal_port = int(os.environ.get("INTERNAL_API_PORT", "8766"))
        internal_url = f"http://127.0.0.1:{internal_port}{self.path}"
        try:
            import urllib.request
            req = urllib.request.Request(internal_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                raw = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw)
                return True
        except Exception:
            pass

        # 2. Resilient in-process deterministic fallback
        try:
            from services.quant_risk_service import compute_quant_risk_payload
            payload = compute_quant_risk_payload(horizon=horizon)
            raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raw = json.dumps(
                {
                    "error": str(exc),
                    "is_synthetic": True,
                    "synthetic_components": ["returns_sharpe_cvar"],
                    "production_actionable": False,
                },
                indent=2,
            ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
        return True

    def _normalize_api_path(self) -> str:
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "").rstrip("/") or "/"
        return path

    def _send_json(self, data: dict, status: int = 200, *, contract_header: bool = False) -> None:
        """Alias used by admin POST handlers."""
        self._json_response(status, data, contract_header=contract_header)

    def _serve_gis_compressor_stations(self) -> bool:
        path = self._normalize_api_path()
        if path not in (
            "/api/v1/gis/compressor-stations",
            "/output/api/v1/gis/compressor-stations",
        ):
            return False
        try:
            from services.compressor_stations import build_gis_stations_payload

            self._send_json(build_gis_stations_payload(), contract_header=True)
        except Exception as exc:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": str(exc)[:400], "proximity_compressors": []},
                status=500,
                contract_header=True,
            )
        return True

    def _serve_intel_get_apis(self) -> bool:
        """News / FIRMS / Market GET routes (Contract 1.8.0 Sections II–III)."""
        path = self._normalize_api_path()
        aliases = {
            "/api/v1/news/latest": "news",
            "/output/api/v1/news/latest": "news",
            "/api/v1/gis/firms/anomalies": "firms",
            "/output/api/v1/gis/firms/anomalies": "firms",
            "/api/v1/market/summary": "market",
            "/output/api/v1/market/summary": "market",
        }
        kind = aliases.get(path)
        if not kind:
            return False
        try:
            from urllib.parse import parse_qs

            qs = parse_qs(urlparse(self.path).query or "")
            if kind == "news":
                from services.news_service import fetch_latest_news

                limit = int((qs.get("limit") or ["25"])[0])
                self._send_json(fetch_latest_news(limit=limit), contract_header=True)
            elif kind == "firms":
                from services.firms_service import fetch_firms_anomalies

                days = int((qs.get("days") or ["1"])[0])
                buf = float((qs.get("max_distance_nm") or ["50"])[0])
                self._send_json(
                    fetch_firms_anomalies(days=days, max_distance_nm=buf),
                    contract_header=True,
                )
            else:
                from services.market_data_service import fetch_market_summary

                self._send_json(fetch_market_summary(), contract_header=True)
        except Exception as exc:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": str(exc)[:400]},
                status=500,
                contract_header=True,
            )
        return True

    def _handle_alerts_dispatch_post(self) -> bool:
        path = self._normalize_api_path()
        if path not in ("/api/v1/alerts/dispatch", "/output/api/v1/alerts/dispatch"):
            return False
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        try:
            from services.notify_service import dispatch_alert

            self._send_json(dispatch_alert(body if isinstance(body, dict) else {}), contract_header=True)
        except Exception as exc:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": str(exc)[:400], "delivered": False},
                status=500,
                contract_header=True,
            )
        return True

    def _serve_gis_ais(self) -> bool:
        """GIS AIS tracker — /api/v1/gis/ais/* (cache-first, Contract 1.8.0)."""
        path = self._normalize_api_path()
        if "/api/v1/gis/ais" not in path:
            return False
        # Strip /output prefix for parser
        norm = path
        if norm.startswith("/output"):
            norm = norm[len("/output") :]
        try:
            from services.ais_tracker import handle_ais_request

            status, body, headers = handle_ais_request(norm)
        except Exception as exc:  # noqa: BLE001
            self._json_response(
                500,
                {"ok": False, "error": str(exc)[:200], "contract_version": "1.8.0-ops-gis-sot"},
                contract_header=True,
            )
            return True
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for hk, hv in headers.items():
            self.send_header(hk, hv)
        if "X-Contract-Version" not in headers:
            self.send_header("X-Contract-Version", "1.8.0-ops-gis-sot")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
        return True

    def _handle_route_analytics_post(self) -> bool:
        path = self._normalize_api_path()
        if path not in (
            "/api/v1/route/analytics",
            "/output/api/v1/route/analytics",
        ):
            return False
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            from services.compressor_stations import analyze_route_payload

            self._send_json(analyze_route_payload(body), contract_header=True)
        except Exception as exc:  # noqa: BLE001
            self._send_json(
                {
                    "contract_version": "1.8.0-ops-gis-sot",
                    "proximity_compressors": [],
                    "error": str(exc)[:400],
                },
                status=500,
                contract_header=True,
            )
        return True

    def _handle_update_key(self) -> bool:
        """Admin live key injection — localhost only (optional admin token)."""
        path = self._normalize_api_path()
        if path not in (
            "/api/config/update-key",
            "/output/api/config/update-key",
        ):
            return False
        if not self._client_is_local():
            self._send_json({"ok": False, "error": "localhost_only"}, status=403)
            return True
        expected = (os.environ.get("SENTINEL_ADMIN_TOKEN") or "").strip()
        if expected:
            got = (self.headers.get("X-Sentinel-Admin-Token") or "").strip()
            if got != expected:
                self._send_json({"ok": False, "error": "admin_token_required"}, status=401)
                return True
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 8192:
            self._send_json({"ok": False, "error": "invalid_body"}, status=400)
            return True
        try:
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"json:{exc}"}, status=400)
            return True
        new_key = str(
            data.get("userkey") or data.get("api_key") or data.get("key") or ""
        ).strip()
        if not new_key:
            self._send_json({"ok": False, "error": "Empty key provided"}, status=400)
            return True
        run_sync = bool(data.get("run_sync", True))
        try:
            from services.key_manager import mask_key, update_userkey

            success, message = update_userkey(new_key, run_sync=run_sync)
            try:
                from services.archive_service import status_report

                status_report()
            except Exception:
                pass
            if success:
                self._send_json(
                    {
                        "ok": True,
                        "message": message,
                        "key_masked": mask_key(new_key),
                        "ingest_mode": "commercial_rest",
                    }
                )
            else:
                # Invalid commercial key — still 200-ish UX for UI; report as ok:false
                self._send_json(
                    {
                        "ok": False,
                        "error": message,
                        "key_masked": mask_key(new_key),
                        "ingest_mode": "hybrid_local",
                    },
                    status=400,
                )
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)[:400]}, status=500)
        return True

    def _serve_gis_tiles(self) -> bool:
        """Contract 1.8.0 GIS tile proxy — MapTiler/Mapbox/Esri/OSM + disk cache + hard-stop."""
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "")
        try:
            from services.tile_proxy import (
                CONTRACT_VERSION,
                fetch_gis_tile,
                parse_gis_tile_path,
                public_status,
            )
        except Exception as exc:  # noqa: BLE001
            if "/api/v1/gis/tiles/" in path:
                self._json_response(
                    500,
                    {"ok": False, "error": "tile_proxy_unavailable", "detail": str(exc)[:120]},
                    contract_header=True,
                )
                return True
            return False

        if path in (
            "/api/v1/gis/tiles/status",
            "/api/v1/gis/tiles/status/",
            "/output/api/v1/gis/tiles/status",
        ):
            self._json_response(200, {"ok": True, **public_status()}, contract_header=True)
            return True

        # Also accept /output prefix
        norm = path
        if norm.startswith("/output/api/v1/gis/tiles/"):
            norm = norm[len("/output") :]
        spec = parse_gis_tile_path(norm)
        if not spec:
            return False

        result = fetch_gis_tile(
            provider=spec["provider"], z=spec["z"], x=spec["x"], y=spec["y"]
        )
        if result.status != 200 or not result.body:
            self._json_response(
                int(result.status or 502),
                {
                    "ok": False,
                    "error": result.error_code or "tile_unavailable",
                    "contract_version": CONTRACT_VERSION,
                },
                contract_header=True,
            )
            return True

        self.send_response(200)
        self.send_header("Content-Type", result.content_type)
        self.send_header("Content-Length", str(len(result.body)))
        for hk, hv in result.headers().items():
            self.send_header(hk, hv)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(result.body)
        return True

    def _serve_map_tiles(self) -> bool:
        """Paid tile proxy — key stays server-side; Esri fallback (Never-Black maps)."""
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "")
        try:
            from services.maptiles_proxy import (
                fetch_tile,
                origin_allowed,
                parse_tile_path,
                public_status,
                rate_limit_ok,
            )
        except Exception as exc:  # noqa: BLE001
            if path.startswith("/api/tiles/"):
                self._json_response(
                    500,
                    {"ok": False, "error": "maptiles_proxy_unavailable", "detail": str(exc)[:120]},
                )
                return True
            return False

        if path in (
            "/api/v1/maptiles/status",
            "/api/v1/maptiles/status/",
            "/output/api/v1/maptiles/status",
        ):
            self._json_response(200, {"ok": True, **public_status()})
            return True

        spec = parse_tile_path(path)
        if not spec:
            return False

        client_ip = (self.client_address[0] if self.client_address else "") or ""
        origin = self.headers.get("Origin") or ""
        referer = self.headers.get("Referer") or ""
        if not origin_allowed(origin=origin, referer=referer, client_ip=client_ip):
            self._json_response(403, {"ok": False, "error": "origin_forbidden"})
            return True
        if not rate_limit_ok(client_ip):
            self._json_response(429, {"ok": False, "error": "rate_limited"})
            return True

        result = fetch_tile(
            spec["provider"], spec["z"], spec["x"], spec["y"], prefer_ext=spec["ext"]
        )
        if result.status != 200 or not result.body:
            self._json_response(
                int(result.status or 502),
                {"ok": False, "error": result.error_code or "tile_unavailable"},
            )
            return True

        self.send_response(200)
        self.send_header("Content-Type", result.content_type)
        self.send_header("Content-Length", str(len(result.body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Sentinel-Tile-Source", result.source)
        self.send_header("X-Sentinel-Tile-Cache", "HIT" if result.cache_hit else "MISS")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(result.body)
        return True

    def do_GET(self):  # noqa: N802
        if self._serve_gis_tiles():
            return
        if self._serve_gis_ais():
            return
        if self._serve_intel_get_apis():
            return
        if self._serve_map_tiles():
            return
        if self._serve_qflex_1_10():
            return
        if self._serve_arctic_assets():
            return
        if self._serve_desktop_7000():
            return
        if self._serve_live_health():
            return
        if self._serve_quant_risk():
            return
        if self._serve_gis_compressor_stations():
            return
        if self._serve_key_status():
            return
        if self._dev_serve_or_sync():
            return
        parsed = urlparse(self.path)
        rel = unquote(parsed.path or "").lstrip("/")
        if rel.endswith(
            (".js", ".mjs", ".css", ".glb", ".gltf", ".html", ".mp4", ".webm", ".json")
        ):
            target = ROOT / rel
            if target.is_file():
                self._send_file(target, no_store=True)
                return
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        """Always handle POST — never fall through to BaseHTTPRequestHandler 501."""
        try:
            if self._handle_alerts_dispatch_post():
                return
            if self._handle_route_analytics_post():
                return
            if self._handle_update_key():
                return
            self._send_json({"ok": False, "error": "Endpoint Not Found"}, status=404)
        except Exception as exc:  # noqa: BLE001
            try:
                self._send_json({"ok": False, "error": str(exc)[:400]}, status=500)
            except Exception:
                self.send_error(500, str(exc))

    def do_HEAD(self):  # noqa: N802
        if self._serve_gis_tiles():
            return
        if self._serve_gis_ais():
            return
        if self._serve_intel_get_apis():
            return
        if self._serve_map_tiles():
            return
        if self._serve_qflex_1_10():
            return
        if self._serve_arctic_assets():
            return
        if self._serve_desktop_7000():
            return
        if self._serve_live_health():
            return
        if self._serve_quant_risk():
            return
        if self._serve_gis_compressor_stations():
            return
        if self._serve_key_status():
            return
        if self._dev_serve_or_sync():
            return
        parsed = urlparse(self.path)
        rel = unquote(parsed.path or "").lstrip("/")
        if rel.endswith(
            (".js", ".mjs", ".css", ".glb", ".gltf", ".html", ".mp4", ".webm", ".json")
        ):
            target = ROOT / rel
            if target.is_file():
                self._send_file(target, no_store=True)
                return
        return super().do_HEAD()

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))


def port_open(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def main() -> int:
    global DEV_MODE

    parser = argparse.ArgumentParser(description="Serve Oracle-1001 / Sentinel dashboard")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Hot-reload: sync web->output on each JS/CSS request; Cache-Control: no-store",
    )
    parser.add_argument(
        "--sync-on-start",
        action="store_true",
        help="Run full web->output asset sync once at server start",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Exit 0 if port already listening; else exit 1",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bind even if something already listens (may fail); prefer kill+restart externally",
    )
    args = parser.parse_args()
    DEV_MODE = bool(args.dev)

    from services.utils.canonical_port import assert_canonical_dashboard_port

    args.port = assert_canonical_dashboard_port(int(args.port), context="serve_dashboard")

    if args.check_only:
        ok = port_open(args.host, args.port)
        print(f"{'OK' if ok else 'DOWN'} {args.host}:{args.port}")
        return 0 if ok else 1

    if args.sync_on_start or DEV_MODE:
        print("[SYNC] startup web->output asset sync", flush=True)
        sync_web_assets(ROOT, force=False, log=True)

    port = int(args.port)
    ttf_url = _sheet_url(port, "ttf")
    top10_url = _sheet_url(port, "top10")
    route_url = _sheet_url(port, "route")
    balance_url = _sheet_url(port, "balance")

    if port_open(args.host, port) and not args.force:
        print(f"OK already listening on {args.host}:{port}")
        print(f"Sentinel Dashboard live at {ttf_url}")
        print(f"TOP10 Flagships sheet at {top10_url}")
        print(f"ROUTE Analytics sheet at {route_url}")
        print(f"BALANCE sheet at {balance_url}")
        print(f"Desktop assets: http://{args.host}:{port}/assets/7000/ -> {DESKTOP_7000}")
        print(f"Q-Flex REAL VIDEO: http://{args.host}:{port}/assets/1-10/ -> {QFLEX_1_10}")
        return 0

    try:
        server = _ReuseHTTPServer((args.host, port), DashboardHandler)
    except OSError as exc:
        print(f"ERROR: cannot bind {args.host}:{port}: {exc}", file=sys.stderr)
        if port_open(args.host, port):
            print("NOTE: port became active concurrently — treating as OK")
            print(f"Sentinel Dashboard live at {ttf_url}")
            return 0
        return 1

    mode = "DEV(hot-reload)" if DEV_MODE else "prod"
    print(f"Serving {ROOT} mode={mode} port={port}")
    print(
        f"Virtual /assets/7000/ -> {DESKTOP_7000} "
        f"(exists={DESKTOP_7000.exists()}, env={os.environ.get('ASSETS_7000_DIR', '<not set>')})\n"
        f"Virtual /assets/1-10/ -> {QFLEX_1_10} "
        f"(exists={QFLEX_1_10.exists()}, env={os.environ.get('ASSETS_1_10_DIR', '<not set>')})"
    )
    print(f"Sentinel Dashboard live at {ttf_url}")
    print(f"TOP10 Flagships sheet at {top10_url}")
    print(f"ROUTE Analytics sheet at {route_url}")
    print(f"BALANCE sheet at {balance_url}")
    print("Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        try:
            server.server_close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
