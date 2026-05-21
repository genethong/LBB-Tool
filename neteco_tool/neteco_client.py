"""
NetEco WebService RESTful NBI Client
Handles authentication and all API calls to Huawei iMaster NetEco.
"""
from __future__ import annotations
import json
import logging
import time
import requests
import urllib3

# Suppress SSL warnings for self-signed certs on internal NetEco server
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("neteco_tool")

BASE_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json",
}

PAGE_SIZE = 2000  # Safe page size for all endpoints


class NetEcoAPIError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"NetEco API Error {code}: {message}")


class NetEcoClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.openid = None
        self._openid_time = None

    # ─────────────────────────────────────────────
    # Auth
    # ─────────────────────────────────────────────
    def login(self) -> tuple[bool, str]:
        """Login and obtain openid token. Returns (success, message).
        Tries plain password first, then Base64-encoded (some NetEco versions require it).
        """
        import base64
        attempts = [
            self.password,
            base64.b64encode(self.password.encode()).decode(),
        ]
        last_msg = ""
        for i, pwd in enumerate(attempts):
            enc_label = "plain" if i == 0 else "base64"
            try:
                log.info("LOGIN attempt (%s) → PUT %s/rest/openapi/sm/session user=%s",
                         enc_label, self.base_url, self.username)
                resp = requests.put(
                    f"{self.base_url}/rest/openapi/sm/session",
                    json={"userid": self.username, "value": pwd},
                    headers=BASE_HEADERS,
                    verify=False,
                    timeout=15,
                )
                data = resp.json()
                log.info("LOGIN response (%s): code=%s desc=%s",
                         enc_label, data.get("code"), data.get("description"))
                if data.get("code") == 0:
                    self.openid = data["data"]
                    self._openid_time = time.time()
                    return True, "Login successful"
                last_msg = data.get("description", f"Login failed (code {data.get('code')})")
            except requests.exceptions.ConnectionError:
                return False, "Cannot reach NetEco server — check URL and network"
            except Exception as e:
                return False, str(e)
        return False, last_msg

    def logout(self):
        if not self.openid:
            return
        try:
            requests.delete(
                f"{self.base_url}/rest/openapi/sm/session",
                headers={**BASE_HEADERS, "openid": self.openid},
                verify=False,
                timeout=10,
            )
        except Exception:
            pass
        self.openid = None

    def ensure_logged_in(self):
        """Auto-refresh if token is older than 25 min."""
        if not self.openid:
            ok, msg = self.login()
            if not ok:
                raise NetEcoAPIError(-1, msg)
        elif self._openid_time and (time.time() - self._openid_time) > 1500:
            self.login()

    def _get(self, path: str, params: dict) -> dict:
        """Generic paginated GET — params passed as custom HTTP header.
        Auto-retries once on 1204 (token expired/invalidated) by forcing re-login.
        """
        self.ensure_logged_in()
        url = f"{self.base_url}{path}"
        all_data = []
        page = 1
        _retried_1204 = False

        while True:
            headers = {**BASE_HEADERS, "openid": self.openid}
            p = {**params, "pageIndex": page, "pageSize": PAGE_SIZE}
            headers["params"] = json.dumps(p)
            log.info("API GET %s page=%d params=%s", path, page, json.dumps(params)[:200])
            resp = requests.get(url, headers=headers, verify=False, timeout=30)
            body = resp.json()
            code = body.get("code", -1)
            log.info("API response %s page=%d code=%s count=%s",
                     path, page, code,
                     len(body.get("data", [])) if isinstance(body.get("data"), list) else "n/a")
            if code == 1204 and not _retried_1204:
                # Token expired or invalidated by a newer login — re-login once and retry
                log.warning("1204 on %s page=%d — re-logging in and retrying…", path, page)
                self.openid = None
                self._openid_time = None
                self.ensure_logged_in()
                _retried_1204 = True
                continue   # retry same page with fresh token
            if code != 0:
                log.error("API error %s: code=%s desc=%s", path, code, body.get("description"))
                raise NetEcoAPIError(code, body.get("description", "Unknown error"))
            chunk = body.get("data") or []
            if isinstance(chunk, list):
                all_data.extend(chunk)
                if len(chunk) < PAGE_SIZE:
                    break
                page += 1
            else:
                return body
        return {"code": 0, "data": all_data}

    def _get_no_page(self, path: str, params: dict) -> dict:
        """Single GET without pagination — for statistic/alarm endpoints.
        Auto-retries once on 1204 (token expired/invalidated).
        """
        self.ensure_logged_in()
        url = f"{self.base_url}{path}"
        retried = False
        while True:
            headers = {**BASE_HEADERS, "openid": self.openid, "params": json.dumps(params)}
            log.info("API GET (no-page) %s params=%s", path, json.dumps(params)[:200])
            resp = requests.get(url, headers=headers, verify=False, timeout=60)
            body = resp.json()
            code = body.get("code", -1)
            log.info("API response (no-page) %s code=%s", path, code)
            if code == 1204 and not retried:
                log.warning("1204 on %s — re-logging in and retrying…", path)
                self.openid = None
                self._openid_time = None
                self.ensure_logged_in()
                retried = True
                continue
            if code != 0:
                log.error("API error %s: code=%s desc=%s", path, code, body.get("description"))
            return body

    # ─────────────────────────────────────────────
    # Configuration Management
    # ─────────────────────────────────────────────
    def get_mo_types(self) -> list:
        return self._get("/rest/openapi/neteco/nbi/v2/motype", {}).get("data", [])

    def get_mos(self, extra_params: dict = None) -> list:
        params = extra_params or {}
        return self._get("/rest/openapi/neteco/nbi/v2/mo", params).get("data", [])

    # ─────────────────────────────────────────────
    # Signal Management
    # ─────────────────────────────────────────────
    def get_sampling(self, dns: list, signal_ids: list = None) -> list:
        params = {"dns": dns}
        if signal_ids:
            params["signalIds"] = signal_ids
        result = []
        # Batch dns to avoid overly large headers
        for batch in _batch(dns, 200):
            p = {"dns": batch}
            if signal_ids:
                p["signalIds"] = signal_ids
            result.extend(self._get("/rest/openapi/neteco/nbi/v2/signal/sampling", p).get("data", []))
        return result

    def get_config_signals(self, dns: list, signal_ids: list = None) -> list:
        result = []
        for batch in _batch(dns, 200):
            p = {"dns": batch}
            if signal_ids:
                p["signalIds"] = signal_ids
            result.extend(self._get("/rest/openapi/neteco/nbi/v2/signal/config", p).get("data", []))
        return result

    def get_statistic(self, dns: list, signal_ids: list, start_ms: int, end_ms: int,
                      granularity: int = None) -> list:
        """Fetch statistical (historical) signal data.

        Manual constraints:
          - signalIds  : MANDATORY, max 50 per request
          - dns        : max 50 per request
          - time window: max 24 hours per request  → chunk larger ranges
          - pagination : follow hasNextPage
          - granularity: optional aggregation interval in seconds
                         (e.g. 3600=hourly, 86400=daily).
                         Without it, NetEco may return only a single
                         aggregate snapshot for sampling-type signals.
        """
        if not signal_ids:
            raise ValueError("signalIds is mandatory for statistical signal query — "
                             "select at least one signal before extracting statistical data.")

        ONE_DAY_MS = 24 * 3600 * 1000

        # Split the requested range into ≤24-hour windows
        time_windows: list[tuple[int, int]] = []
        t = int(start_ms)
        end = int(end_ms)
        while t < end:
            w_end = min(t + ONE_DAY_MS, end)
            time_windows.append((t, w_end))
            t = w_end

        result = []
        _logged_first_row = False   # log first raw row once per call for diagnosis
        for dn_batch in _batch(dns, 50):            # max 50 DNs
            for sig_batch in _batch(signal_ids, 50):  # max 50 signal IDs
                for t_start, t_end in time_windows:
                    page = 1
                    while True:
                        params = {
                            "dns":       dn_batch,
                            "signalIds": sig_batch,
                            "startTime": t_start,
                            "endTime":   t_end,
                            "pageIndex": page,
                            "pageSize":  4000,
                        }
                        if granularity is not None:
                            params["granularity"] = granularity
                        log.info("statistic: dns=%d sigs=%d window=%s→%s page=%d gran=%s",
                                 len(dn_batch), len(sig_batch), t_start, t_end, page, granularity)
                        resp = self._get_no_page(
                            "/rest/openapi/neteco/nbi/v2/signal/statistic", params)
                        code = resp.get("code", -1)
                        if code != 0:
                            log.error("statistic error: code=%s desc=%s",
                                      code, resp.get("description"))
                            break
                        data = resp.get("data") or []
                        if data and not _logged_first_row:
                            log.info("statistic: first raw row sample → %s", json.dumps(data[0]))
                            _logged_first_row = True
                        result.extend(data)
                        log.info("statistic: got %d rows (page %d), hasNextPage=%s",
                                 len(data), page, resp.get("hasNextPage"))
                        if not resp.get("hasNextPage", False):
                            break
                        page += 1
        return result

    def get_active_alarms(self, dns: list = None, severity: list = None,
                          alarm_name: str = None) -> list:
        params = {"pageIndex": 1, "pageSize": PAGE_SIZE}
        if dns:
            params["dns"] = dns
        if severity:
            params["severities"] = severity
        if alarm_name:
            params["alarmName"] = alarm_name
        result = []
        page = 1
        while True:
            params["pageIndex"] = page
            resp = self._get_no_page("/rest/openapi/neteco/nbi/v2/alarm/current", params)
            chunk = resp.get("data") or []
            result.extend(chunk)
            if len(chunk) < PAGE_SIZE:
                break
            page += 1
        return result

    def get_historical_alarms(self, start_ms: int, end_ms: int,
                               dns: list = None, alarm_name: str = None,
                               severity: list = None) -> list:
        params = {
            "startTime": start_ms,
            "endTime": end_ms,
            "pageIndex": 1,
            "pageSize": PAGE_SIZE,
        }
        if dns:
            params["dns"] = dns
        if alarm_name:
            params["alarmName"] = alarm_name
        if severity:
            params["severities"] = severity
        result = []
        page = 1
        while True:
            params["pageIndex"] = page
            resp = self._get_no_page("/rest/openapi/neteco/nbi/v2/alarm/history", params)
            chunk = resp.get("data") or []
            result.extend(chunk)
            if len(chunk) < PAGE_SIZE:
                break
            page += 1
        return result

    def get_inventory(self) -> list:
        return self._get("/rest/openapi/neteco/nbi/v2/inventory", {}).get("data", [])

    def get_signal_info(self) -> list:
        return self._get("/rest/openapi/neteco/nbi/v2/signal/info", {}).get("data", [])


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _batch(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]
