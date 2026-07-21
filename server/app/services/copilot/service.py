"""OpenAI Codex OAuth service and MCP-backed SQL agent loop."""

import asyncio
import base64
import json
import logging
import os
import re
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.mcp_server import get_mcp_server
from app.mcp_server.tools.sql_normalizer import normalize_readonly_sql

logger = logging.getLogger(__name__)

# C9: cap how many rows we ship over SSE/JSON to the UI. The Angular grid
# never renders more than this, and the full result set is always available
# by re-executing the SQL via /api/copilot/sql/execute.
_UI_ROW_LIMIT = 500

# C8: short conversational user messages ("hi", "thanks", "ok cool") should
# never trigger an auto-continue prompt — there is nothing to continue and
# nudging the model only wastes tokens / produces hallucinated work.
_CONVERSATIONAL_PATTERN = re.compile(
    r"^(?:hi|hey|hello|yo|sup|thanks|thank\s*you|thx|ty|"
    r"ok(?:ay)?|cool|nice|great|good|got\s*it|"
    r"bye|goodbye|cya|see\s*ya)[\s!?.,]*$",
    re.IGNORECASE,
)


def _looks_conversational(msg: str) -> bool:
    """Return True if ``msg`` is a short greeting / acknowledgement."""
    if not msg:
        return True
    stripped = msg.strip()
    if len(stripped) <= 4:
        return True
    if len(stripped) <= 30 and _CONVERSATIONAL_PATTERN.match(stripped):
        return True
    return False

# ── SSL Context ────────────────────────────────────────────────────
# Try proper certificate verification first (what VS Code does).
# Falls back to unverified ONLY if corporate proxy has custom CA
# that isn't in the system trust store.
def _build_ssl_context() -> ssl.SSLContext:
    """
    Build SSL context with proper cert verification.
    Priority: 1) Custom CA bundle (REQUESTS_CA_BUNDLE / SSL_CERT_FILE env)
              2) System default trust store (certifi)
              3) Unverified (last resort for corporate proxies)
    """
    # Check for custom CA bundle (set by IT for corporate proxies)
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca_bundle and os.path.isfile(ca_bundle):
        ctx = ssl.create_default_context(cafile=ca_bundle)
        logger.info(f"SSL: Using custom CA bundle from {ca_bundle}")
        return ctx

    # Try system default (certifi included with httpx/requests)
    try:
        ctx = ssl.create_default_context()
        # Verify default certs are loaded (77+ root certs expected)
        stats = ctx.cert_store_stats()
        if stats.get("x509_ca", 0) > 0:
            logger.info(f"SSL: Using system trust store ({stats['x509_ca']} CA certs)")
            return ctx
    except Exception:
        pass

    # Try certifi explicitly
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        logger.info("SSL: Using certifi CA bundle")
        return ctx
    except Exception:
        pass

    # Last resort: unverified (log a warning so it's visible)
    logger.warning(
        "SSL: ⚠️ Certificate verification DISABLED — no valid CA bundle found. "
        "Set REQUESTS_CA_BUNDLE or SSL_CERT_FILE env var to your corporate CA bundle path."
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

_SSL_CTX = _build_ssl_context()

# ── Endpoints ──────────────────────────────────────────────────────
CODEX_AUTH_BASE_URL = "https://auth.openai.com"
CODEX_AUTH_ACCOUNTS_API_URL = f"{CODEX_AUTH_BASE_URL}/api/accounts"
CODEX_VERIFICATION_URL = f"{CODEX_AUTH_BASE_URL}/codex/device"
CODEX_OAUTH_REDIRECT_URI = f"{CODEX_AUTH_BASE_URL}/deviceauth/callback"
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_CLIENT_VERSION = os.environ.get("CODEX_CLIENT_VERSION", "0.145.0")
PROVIDER_OPENAI_CODEX = "openai_codex"


@dataclass
class _ProviderResponse:
    status_code: int
    text: str
    data: Dict[str, Any] = field(default_factory=dict)

    def json(self) -> Dict[str, Any]:
        return self.data

# ── System prompt ─────────────────────────────────────────────────
# The generic SQL-assistant guidance and agent behavioural rules live in
# `app.mcp_server.system_prompt` so the Codex web UI and stdio MCP path
# share a single source of truth. The
# response-format block is gated by `COPILOT_STRUCTURED_RESPONSE` so VSCode-
# style free-form answers are the default.
from app.mcp_server.system_prompt import (
    GENERIC_SQL_PROMPT,
    COPILOT_BEHAVIORAL_RULES,
    COPILOT_STRUCTURED_RESPONSE_FORMAT,
)


def _compose_system_prompt_base() -> str:
    """Compose the static portion of the system prompt at call time so the
    `COPILOT_STRUCTURED_RESPONSE` env flag can be flipped without restart."""
    parts = [GENERIC_SQL_PROMPT, COPILOT_BEHAVIORAL_RULES]
    try:
        from app.config.settings import settings as _settings
        if getattr(_settings, "copilot_structured_response", False):
            parts.append(COPILOT_STRUCTURED_RESPONSE_FORMAT)
    except Exception:
        # Settings unavailable — fall back to free-form.
        pass
    return "".join(parts)


# Back-compat alias: legacy code paths read `SYSTEM_PROMPT_BASE` directly.
# `_build_system_prompt` calls `_compose_system_prompt_base()` instead so the
# structured-response flag is re-evaluated on every request.
SYSTEM_PROMPT_BASE = _compose_system_prompt_base()


# ---------------------------------------------------------------------------
# Cross-pod / SSH cluster tooling was removed in the hackathon cleanup.
# The agent is now strictly scoped to the database the user is currently
# connected to. The helpers below are kept as no-ops so call sites that
# still reference them (e.g. ``_build_system_prompt``) keep compiling.
# ---------------------------------------------------------------------------
CROSSPOD_HINT_ENABLED = ""
CROSSPOD_HINT_DISABLED = ""
CROSSPOD_HINT_SSH_CREDS_PROVIDED = ""

_SSH_CRED_TOOLS: set[str] = set()
_SSH_CRED_FIELDS: tuple[str, ...] = ()
_SSH_SECRET_FIELDS: set[str] = set()


def _compact_tool_result_for_sse(result: Any) -> Any:
    """Keep live progress useful without streaming large database results twice."""
    if not isinstance(result, dict):
        return result
    compact = dict(result)
    compact.pop("csv_data", None)
    records = compact.get("records")
    if isinstance(records, list) and len(records) > 5:
        compact["records"] = records[:5]
        compact["row_count"] = compact.get("row_count", len(records))
        compact["truncated_for_stream"] = True
    values = compact.get("values")
    if isinstance(values, list) and len(values) > 20:
        compact["values"] = values[:20]
        compact["value_count"] = compact.get("value_count", len(values))
        compact["truncated_for_stream"] = True
    tables = compact.get("tables")
    if isinstance(tables, list) and len(tables) > 10:
        compact["tables"] = tables[:10]
        compact["table_count"] = compact.get("table_count", len(tables))
        compact["truncated_for_stream"] = True
    columns = compact.get("columns")
    if isinstance(columns, list) and len(columns) > 25:
        compact["columns"] = columns[:25]
        compact["column_count"] = compact.get("column_count", len(columns))
        compact["truncated_for_stream"] = True
    return compact


def _inject_ssh_credentials(
    tool_name: str,
    tool_args: Dict[str, Any],
    ssh_credentials: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """No-op -- SSH-based cluster tools were removed in the cleanup."""

    return tool_args


def _redact_ssh_args_for_log(tool_args: Dict[str, Any]) -> Dict[str, Any]:
    """No-op -- SSH-based cluster tools were removed in the cleanup."""

    return tool_args


@dataclass
class CopilotToolCall:
    """A tool call executed during the agent loop."""
    tool_name: str
    arguments: Dict[str, Any]
    result: Any = None
    success: bool = True
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    reasoning: Optional[str] = None
    database: Optional[str] = None


@dataclass
class CopilotResponse:
    """Response from the Copilot agent loop."""
    success: bool
    message: str = ""
    sql: Optional[str] = None
    records: List[Dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    columns: List[str] = field(default_factory=list)
    tool_calls: List[CopilotToolCall] = field(default_factory=list)
    total_time_ms: float = 0.0
    model: str = ""
    error: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    active_database: str = ""
    # ── Verifiable Trust Layer (Phase 1) ──────────────────────────────
    # Earned trust signals computed from the tool trace (see _compute_trust).
    trust_score: int = 0
    trust_label: str = ""  # "verified" | "caution" | "unverified" | "" (n/a)
    trust_checks: List[Dict[str, Any]] = field(default_factory=list)
    verification: Optional[Dict[str, Any]] = None
    grounded_sources: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Verifiable Trust Layer (Phase 1)
#
# Every text-to-SQL agent can produce a clean-looking result that is silently
# wrong (a fan-out JOIN that double-counts, the wrong filter, a hallucinated
# column, or an ungoverned definition). These helpers turn the agent's own
# tool trace into four *earned* trust signals so the answer can be trusted:
#   1. schema_validated -- SQL was checked against the live schema
#   2. cross_checked    -- a 2nd independent query confirms the headline metric
#   3. result_sane      -- the headline query returned non-empty, non-null data
# Nothing here calls the model or the database; it only inspects results that
# were already produced this turn.
# ---------------------------------------------------------------------------
def _trust_to_number(value: Any) -> Optional[float]:
    """Best-effort numeric coercion for cross-checks (int/float/Decimal/str)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from decimal import Decimal
        if isinstance(value, Decimal):
            return float(value)
    except Exception:
        pass
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _trust_scalar_from_execute(result: Any) -> Optional[float]:
    """Extract a single headline scalar from an execute_sql result.

    Only single-cell results (1 row x 1 column -- e.g. COUNT/SUM/AVG) are
    treated as verifiable headline metrics. This deliberately ignores
    multi-row results so two unrelated queries that both hit a ``LIMIT 100``
    cannot be mistaken for an "agreement".
    """
    if not isinstance(result, dict):
        return None
    records = result.get("records") or []
    if len(records) == 1 and isinstance(records[0], dict) and len(records[0]) == 1:
        return _trust_to_number(next(iter(records[0].values())))
    return None


def _trust_numeric_pair_from_execute(result: Any) -> Optional[Dict[str, Any]]:
    """Extract an in-query dual-method check from a single-row result.

    Some generated SQL verifies a metric in one statement, returning columns
    such as ``method_a`` and ``method_b`` in the same row. Treat exactly two
    numeric cells in a one-row result as a legitimate cross-check candidate.
    """
    if not isinstance(result, dict):
        return None
    records = result.get("records") or []
    if len(records) != 1 or not isinstance(records[0], dict):
        return None

    numeric_cells = []
    for key, value in records[0].items():
        number = _trust_to_number(value)
        if number is not None:
            numeric_cells.append((str(key), number))

    if len(numeric_cells) != 2:
        return None

    (primary_label, primary_value), (check_label, check_value) = numeric_cells
    return {
        "primary_label": primary_label.replace("_", " "),
        "primary_value": primary_value,
        "check_label": check_label.replace("_", " "),
        "check_value": check_value,
    }


def _trust_method_label(sql: Optional[str]) -> str:
    """Human-readable method label inferred from SQL so the cross-check trace
    is legible to a reviewer (e.g. "Haversine" vs "PostGIS geocoded")."""
    s = (sql or "").lower()
    if "st_dwithin" in s or "st_distance" in s or "::geography" in s or "st_within" in s:
        return "PostGIS geocoded"
    if "acos(" in s and "radians(" in s:
        return "Haversine formula"
    if "ilike" in s or "lower(city)" in s or "city =" in s or "city in" in s:
        return "city-label filter"
    if "join" in s and "count(" in s:
        return "JOIN-based count"
    if "count(" in s:
        return "count query"
    return "SQL query"


def _compute_trust(
    tool_calls: List[CopilotToolCall],
    sql: Optional[str] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    row_count: int = 0,
    columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Derive the Verifiable Trust Layer signals from the agent's tool trace.

    Returns a dict with: trust_score (0-100), trust_label, trust_checks[],
    verification (or None), grounded_sources[].
    """
    checks: List[Dict[str, Any]] = []

    # ── 1. Schema validation ──────────────────────────────────────────
    schema_tools = {
        "validate_sql", "search_tables", "search_columns",
        "introspect_schema", "discover_join_paths", "preview_data",
        "check_relationships",
    }
    schema_validated = False
    schema_detail = "Schema verification was not run; we should verify that."
    for tc in tool_calls:
        if not tc.success:
            continue
        if tc.tool_name == "validate_sql" and isinstance(tc.result, dict) and tc.result.get("valid"):
            schema_validated = True
            schema_detail = "Passed static + structural validation (validate_sql)"
            break
        if tc.tool_name in schema_tools:
            schema_validated = True
            schema_detail = f"Schema introspected via {tc.tool_name} before generating SQL"
    if not schema_validated:
        for tc in tool_calls:
            if tc.tool_name == "execute_sql" and tc.success:
                schema_validated = True
                schema_detail = "Executed successfully against the live database (runtime schema check)"
                break
    checks.append({"name": "Schema validated", "passed": schema_validated, "detail": schema_detail})

    # ── 2. Dual-path cross-check ──────────────────────────────────────
    scalar_runs: List[tuple] = []  # (scalar_value, sql_text)
    in_query_pair: Optional[Dict[str, Any]] = None
    for tc in tool_calls:
        if tc.tool_name == "execute_sql" and tc.success and isinstance(tc.result, dict):
            scalar = _trust_scalar_from_execute(tc.result)
            if scalar is not None:
                scalar_runs.append((scalar, tc.result.get("sql")))
            if in_query_pair is None:
                in_query_pair = _trust_numeric_pair_from_execute(tc.result)
    verification: Optional[Dict[str, Any]] = None
    cross_checked = False
    if len(scalar_runs) >= 2:
        primary_value, primary_sql = scalar_runs[-2]
        check_value, check_sql = scalar_runs[-1]
        delta = abs(primary_value - check_value)
        denom = max(abs(primary_value), abs(check_value), 1.0)
        agreed = (delta / denom) <= 0.05
        cross_checked = agreed
        method_primary = _trust_method_label(primary_sql)
        method_check = _trust_method_label(check_sql)
        verification = {
            "primary_value": primary_value,
            "check_value": check_value,
            "delta": round(delta, 4),
            "agreed": agreed,
            "method_primary": method_primary,
            "method_check": method_check,
        }
        if agreed:
            cross_detail = (
                f"Independently confirmed: {method_primary} vs {method_check} "
                f"agree (\u0394 {round(delta, 2):g})"
            )
        else:
            cross_detail = (
                f"Discrepancy flagged: {method_primary}={primary_value:g} vs "
                f"{method_check}={check_value:g} (\u0394 {round(delta, 2):g})"
            )
    elif in_query_pair is not None:
        primary_value = in_query_pair["primary_value"]
        check_value = in_query_pair["check_value"]
        delta = abs(primary_value - check_value)
        denom = max(abs(primary_value), abs(check_value), 1.0)
        agreed = (delta / denom) <= 0.05
        cross_checked = agreed
        method_primary = in_query_pair["primary_label"]
        method_check = in_query_pair["check_label"]
        verification = {
            "primary_value": primary_value,
            "check_value": check_value,
            "delta": round(delta, 4),
            "agreed": agreed,
            "method_primary": method_primary,
            "method_check": method_check,
        }
        if agreed:
            cross_detail = (
                f"Confirmed inside one verification query: {method_primary} vs "
                f"{method_check} agree (\u0394 {round(delta, 2):g})"
            )
        else:
            cross_detail = (
                f"Discrepancy flagged: {method_primary}={primary_value:g} vs "
                f"{method_check}={check_value:g} (\u0394 {round(delta, 2):g})"
            )
    else:
        cross_detail = "Independent cross-check was not run; we should verify that."
    checks.append({"name": "Cross-checked", "passed": cross_checked, "detail": cross_detail})

    # ── 3. Result sanity ──────────────────────────────────────────────
    result_sane = False
    if row_count and row_count > 0:
        result_sane = True
        sane_detail = f"Query returned {row_count} row(s)"
        if records and columns:
            first_col = columns[0]
            non_null = sum(
                1 for r in records
                if isinstance(r, dict) and r.get(first_col) is not None
            )
            if non_null == 0:
                result_sane = False
                sane_detail = f"Key column '{first_col}' is null in every returned row"
    else:
        sane_detail = "Query returned no rows"
    checks.append({"name": "Result sanity", "passed": result_sane, "detail": sane_detail})

    # ── Score & label ─────────────────────────────────────────────────
    weights = {
        "Schema validated": 35,
        "Cross-checked": 40,
        "Result sanity": 25,
    }
    score = sum(weights.get(c["name"], 0) for c in checks if c["passed"])
    if verification is not None and not verification["agreed"]:
        # An independent check ran and disagreed -- flag for review regardless
        # of the other signals. This is the live "self-catch" state.
        label = "caution"
    elif score >= 75:
        label = "verified"
    elif score >= 40:
        label = "caution"
    else:
        label = "unverified"

    return {
        "trust_score": int(score),
        "trust_label": label,
        "trust_checks": checks,
        "verification": verification,
        "grounded_sources": [],
    }


class CopilotService:
    """
    OpenAI Codex OAuth client with an MCP-backed SQL agent loop.
    """

    # Persist token to survive server restarts
    _TOKEN_FILE = Path(__file__).resolve().parent.parent.parent / "data" / ".copilot_token.json"
    def __init__(self):
        self._provider: str = PROVIDER_OPENAI_CODEX
        self._codex_access_token: Optional[str] = None
        self._codex_refresh_token: Optional[str] = None
        self._codex_id_token: Optional[str] = None
        self._codex_account_id: str = ""
        self._codex_email: str = ""
        self._codex_display_name: str = ""
        self._codex_cached_models: List[Dict[str, Any]] = []
        self._default_model: str = "gpt-5.6-sol"
        # OpenAI OAuth device-flow state.
        self._codex_device_auth_id: Optional[str] = None
        self._codex_user_code: Optional[str] = None
        self._codex_device_expires: int = 0
        self._codex_poll_interval: int = 5
        # Per-session conversation history
        self._sessions: Dict[str, List[Dict[str, Any]]] = {}
        # Last error from list_models (surface in UI)
        self._last_models_fetch_error: Optional[str] = None
        # Concurrency guards: one asyncio.Lock per chat session_id so concurrent
        #   requests with the same session_id do not corrupt history.
        # - _session_locks_guard: protects _session_locks dict creation.
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._session_locks_guard: asyncio.Lock = asyncio.Lock()
        # Try to restore saved token
        self._load_token()

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Return (creating if needed) the asyncio.Lock for this chat session."""
        async with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
                # Soft cap: evict oldest if we ever accumulate too many sessions
                if len(self._session_locks) > 1000:
                    # Drop the first inserted key (insertion order preserved)
                    oldest = next(iter(self._session_locks))
                    if oldest != session_id:
                        self._session_locks.pop(oldest, None)
            return lock

    # ── DPAPI encryption (Windows) for token-at-rest protection ──────
    @staticmethod
    def _dpapi_encrypt(data: bytes) -> bytes:
        """Encrypt bytes using Windows DPAPI (current-user scope)."""
        import ctypes, ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        blob_in = DATA_BLOB(len(data), ctypes.create_string_buffer(data, len(data)))
        blob_out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raise OSError("CryptProtectData failed")
        encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return encrypted

    @staticmethod
    def _dpapi_decrypt(data: bytes) -> bytes:
        """Decrypt DPAPI-encrypted bytes."""
        import ctypes, ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        blob_in = DATA_BLOB(len(data), ctypes.create_string_buffer(data, len(data)))
        blob_out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raise OSError("CryptUnprotectData failed")
        decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return decrypted

    @staticmethod
    def _is_windows() -> bool:
        return os.name == "nt"

    # ── AES-GCM at-rest encryption for non-Windows hosts (Phase 3) ───
    # Legacy versions wrote OAuth tokens in plaintext to
    # `data/.copilot_token.json` whenever DPAPI wasn't available (i.e. any
    # Linux/macOS deployment). That token has the same blast radius as the
    # user's OpenAI session — if the file leaks (backup, container snapshot,
    # accidental commit), the attacker can call Copilot on their behalf. We
    # now require an explicit 32-byte AES-256 key in the env var
    # `COPILOT_TOKEN_ENC_KEY` (base64-encoded). When the key is missing the
    # token is held in memory only and the user must re-authenticate after
    # every restart — a deliberate trade so we never silently write
    # bearer tokens to disk in the clear.
    _ENC_HEADER = b"COPILOTv1"  # marks AES-GCM-encrypted token files

    @staticmethod
    def _load_token_enc_key() -> Optional[bytes]:
        try:
            from app.config.settings import settings as _settings
            raw = (getattr(_settings, "copilot_token_enc_key", "") or "").strip()
        except Exception:
            raw = ""
        if not raw:
            return None
        try:
            import base64
            key = base64.b64decode(raw, validate=True)
        except Exception:
            logger.warning(
                "COPILOT_TOKEN_ENC_KEY is set but is not valid base64 — "
                "token will be held in memory only and not persisted."
            )
            return None
        if len(key) != 32:
            logger.warning(
                "COPILOT_TOKEN_ENC_KEY must decode to exactly 32 bytes (got %d) — "
                "token will be held in memory only and not persisted.",
                len(key),
            )
            return None
        return key

    @classmethod
    def _aesgcm_encrypt(cls, payload: bytes) -> Optional[bytes]:
        key = cls._load_token_enc_key()
        if key is None:
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            aes = AESGCM(key)
            nonce = os.urandom(12)
            ct = aes.encrypt(nonce, payload, associated_data=cls._ENC_HEADER)
            return cls._ENC_HEADER + nonce + ct
        except Exception as e:
            logger.warning(f"AES-GCM encryption failed: {e}")
            return None

    @classmethod
    def _aesgcm_decrypt(cls, blob: bytes) -> Optional[bytes]:
        if not blob.startswith(cls._ENC_HEADER):
            return None
        key = cls._load_token_enc_key()
        if key is None:
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            aes = AESGCM(key)
            nonce = blob[len(cls._ENC_HEADER):len(cls._ENC_HEADER) + 12]
            ct = blob[len(cls._ENC_HEADER) + 12:]
            return aes.decrypt(nonce, ct, associated_data=cls._ENC_HEADER)
        except Exception as e:
            logger.warning(f"AES-GCM decryption failed: {e}")
            return None

    def _load_token(self):
        """Load a persisted OpenAI OAuth token."""
        try:
            if not self._TOKEN_FILE.exists():
                return
            raw = self._TOKEN_FILE.read_bytes()

            # Try DPAPI decryption first (binary file)
            if self._is_windows():
                try:
                    decrypted = self._dpapi_decrypt(raw)
                    data = json.loads(decrypted.decode("utf-8"))
                    self._restore_codex_fields(data)
                    model = data.get("default_model", "gpt-5.6-sol")
                    if self._codex_access_token:
                        self._default_model = model
                        logger.info("Restored DPAPI-encrypted OpenAI OAuth token")
                        return
                except Exception:
                    pass  # Fall through to AES-GCM / plaintext migration paths

            # AES-GCM-encrypted (non-Windows preferred at-rest format).
            decrypted = self._aesgcm_decrypt(raw)
            if decrypted is not None:
                try:
                    data = json.loads(decrypted.decode("utf-8"))
                    self._restore_codex_fields(data)
                    model = data.get("default_model", "gpt-5.6-sol")
                    if self._codex_access_token:
                        self._default_model = model
                        logger.info("Restored AES-GCM-encrypted OpenAI OAuth token")
                        return
                except Exception as e:
                    logger.warning(f"AES-GCM token blob present but JSON parse failed: {e}")
                    return

            # Legacy plaintext file (pre-Phase 3). Read once, then — if we
            # have a way to encrypt it — migrate; otherwise wipe the
            # plaintext file so it doesn't sit on disk forever.
            try:
                data = json.loads(raw.decode("utf-8"))
                self._restore_codex_fields(data)
            except Exception:
                logger.warning("Token file is not DPAPI, AES-GCM, or plaintext JSON — ignoring.")
                return
            model = data.get("default_model", "gpt-5.6-sol")
            if not self._codex_access_token:
                return
            self._default_model = model
            if self._is_windows():
                logger.info("Restored OpenAI OAuth token from disk (plaintext) — migrating to DPAPI")
                self._save_token()
            elif self._load_token_enc_key() is not None:
                logger.info("Restored OpenAI OAuth token from disk (plaintext) — migrating to AES-GCM")
                self._save_token()
            else:
                logger.warning(
                    "Found legacy plaintext token at %s. COPILOT_TOKEN_ENC_KEY is not set; "
                    "deleting the plaintext file. Re-authenticate via the UI to persist a new "
                    "AES-GCM-encrypted token, or set the key to keep the token across restarts.",
                    self._TOKEN_FILE,
                )
                try:
                    self._TOKEN_FILE.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Could not load saved token: {e}")

    def _save_token(self):
        """Persist OpenAI OAuth tokens (DPAPI on Windows, AES-GCM elsewhere).
        Refuses to write plaintext: if no encryption is available the token
        stays in memory only and the user must re-authenticate after restart."""
        try:
            self._TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "codex_access_token": self._codex_access_token or "",
                "codex_refresh_token": self._codex_refresh_token or "",
                "codex_id_token": self._codex_id_token or "",
                "codex_account_id": self._codex_account_id,
                "codex_email": self._codex_email,
                "codex_display_name": self._codex_display_name,
                "default_model": self._default_model,
            }).encode("utf-8")

            if self._is_windows():
                encrypted = self._dpapi_encrypt(payload)
                self._TOKEN_FILE.write_bytes(encrypted)
                logger.info("Saved DPAPI-encrypted OpenAI OAuth token to disk")
                return

            enc = self._aesgcm_encrypt(payload)
            if enc is None:
                logger.warning(
                    "COPILOT_TOKEN_ENC_KEY is not set — OpenAI OAuth token will NOT be "
                    "written to disk. Re-authenticate after every restart, or generate "
                    "a key with: python -c \"import base64,os; print(base64.b64encode(os.urandom(32)).decode())\" "
                    "and put it in .env as COPILOT_TOKEN_ENC_KEY."
                )
                # If a legacy plaintext file is lingering, remove it now —
                # don't leave a stale bearer token sitting on disk.
                try:
                    if self._TOKEN_FILE.exists():
                        self._TOKEN_FILE.unlink()
                except Exception:
                    pass
                return

            self._TOKEN_FILE.write_bytes(enc)
            try:
                os.chmod(self._TOKEN_FILE, 0o600)
            except Exception:
                pass
            logger.info("Saved AES-GCM-encrypted OpenAI OAuth token to disk (mode 600)")
        except Exception as e:
            logger.warning(f"Could not save token: {e}")

    @property
    def is_configured(self) -> bool:
        return bool(self._codex_access_token)

    @staticmethod
    def _decode_jwt_payload(token: Optional[str]) -> Dict[str, Any]:
        if not token:
            return {}
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}

    def _restore_codex_fields(self, data: Dict[str, Any]) -> None:
        self._codex_access_token = data.get("codex_access_token") or None
        self._codex_refresh_token = data.get("codex_refresh_token") or None
        self._codex_id_token = data.get("codex_id_token") or None
        claims = self._decode_jwt_payload(self._codex_id_token)
        auth = claims.get("https://api.openai.com/auth") or {}
        profile = claims.get("https://api.openai.com/profile") or {}
        self._codex_account_id = str(data.get("codex_account_id") or auth.get("chatgpt_account_id") or "")
        self._codex_email = str(data.get("codex_email") or claims.get("email") or profile.get("email") or "")
        self._codex_display_name = str(data.get("codex_display_name") or claims.get("name") or profile.get("name") or "")

    def disconnect(self):
        """Clear OpenAI Codex OAuth credentials."""
        self._codex_access_token = None
        self._codex_refresh_token = None
        self._codex_id_token = None
        self._codex_account_id = ""
        self._codex_email = ""
        self._codex_display_name = ""
        self._codex_cached_models = []
        try:
            if self._TOKEN_FILE.exists():
                self._TOKEN_FILE.unlink()
                logger.info("Deleted persisted OpenAI OAuth token file")
        except Exception as e:
            logger.warning(f"Could not delete token file: {e}")

    def get_config(self) -> Dict[str, Any]:
        return {
            "provider": self._provider,
            "configured": self.is_configured,
            "default_model": self._default_model,
            "has_token": self.is_configured,
            "has_codex_token": bool(self._codex_access_token),
            "codex_email": self._codex_email,
            "codex_display_name": self._codex_display_name,
            "codex_account_id": self._codex_account_id,
        }

    # ── Cross-database connections ─────────────────────────────────

    _saved_connections: List[Dict[str, str]] = []

    def register_connections(self, connections: List[Dict[str, str]]) -> None:
        """Register saved database connections so the agent can switch between them."""
        self._saved_connections = connections
        # Also push to the switch_database tool
        from app.mcp_server.tools.switch_database import set_available_connections
        set_available_connections(connections)
        logger.info(f"Registered {len(connections)} database connections for cross-DB lookup")

    # ── OAuth Device Flow ──────────────────────────────────────────

    async def start_codex_device_flow(self) -> Dict[str, Any]:
        """Start the Codex device authorization flow."""
        now = int(time.time())
        if self._codex_device_auth_id and self._codex_user_code and now < self._codex_device_expires:
            return {
                "provider": PROVIDER_OPENAI_CODEX,
                "user_code": self._codex_user_code,
                "verification_uri": CODEX_VERIFICATION_URL,
                "expires_in": self._codex_device_expires - now,
                "interval": self._codex_poll_interval,
            }
        async with httpx.AsyncClient(timeout=15.0, verify=_SSL_CTX) as client:
            resp = await client.post(
                f"{CODEX_AUTH_ACCOUNTS_API_URL}/deviceauth/usercode",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json={"client_id": CODEX_CLIENT_ID},
            )
        if resp.status_code != 200:
            if resp.status_code == 404:
                raise Exception("Device code login is not enabled. Turn it on in ChatGPT security settings.")
            raise Exception(f"Codex device code request failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        user_code = data.get("user_code") or data.get("usercode") or ""
        device_auth_id = data.get("device_auth_id") or data.get("deviceAuthId") or ""
        if not user_code or not device_auth_id:
            raise Exception("Codex device code response did not include a user code.")
        expires_in = int(data.get("expires_in") or data.get("expires") or 900)
        interval = int(data.get("interval") or 5)
        self._codex_device_auth_id = str(device_auth_id)
        self._codex_user_code = str(user_code)
        self._codex_device_expires = now + expires_in
        self._codex_poll_interval = interval
        return {
            "provider": PROVIDER_OPENAI_CODEX,
            "user_code": self._codex_user_code,
            "verification_uri": CODEX_VERIFICATION_URL,
            "expires_in": expires_in,
            "interval": interval,
        }

    async def poll_codex_device_flow(self) -> Dict[str, Any]:
        """Poll Codex device authorization once."""
        if not self._codex_device_auth_id or not self._codex_user_code:
            raise Exception("No Codex device flow in progress. Start sign-in first.")
        if int(time.time()) > self._codex_device_expires:
            self._clear_codex_device_flow()
            return {"status": "expired"}
        async with httpx.AsyncClient(timeout=15.0, verify=_SSL_CTX) as client:
            try:
                resp = await client.post(
                    f"{CODEX_AUTH_ACCOUNTS_API_URL}/deviceauth/token",
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    json={"device_auth_id": self._codex_device_auth_id, "user_code": self._codex_user_code},
                )
            except (httpx.TimeoutException, httpx.ConnectError):
                return {"status": "pending", "interval": self._codex_poll_interval}
        if resp.status_code in (403, 404):
            return {"status": "pending", "interval": self._codex_poll_interval}
        if resp.status_code != 200:
            raise Exception(f"Codex device auth polling failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        authorization_code = data.get("authorization_code") or ""
        code_verifier = data.get("code_verifier") or ""
        if not authorization_code or not code_verifier:
            return {"status": "pending", "interval": self._codex_poll_interval}
        tokens = await self._exchange_codex_code_for_tokens(authorization_code, code_verifier)
        self._store_codex_tokens(tokens)
        self._clear_codex_device_flow()
        self._provider = PROVIDER_OPENAI_CODEX
        models = await self._list_codex_models()
        if models:
            self._default_model = models[0]["id"]
        self._save_token()
        return {"status": "complete", **self.get_config()}

    def _clear_codex_device_flow(self) -> None:
        self._codex_device_auth_id = None
        self._codex_user_code = None
        self._codex_device_expires = 0

    async def _exchange_codex_code_for_tokens(self, authorization_code: str, code_verifier: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0, verify=_SSL_CTX) as client:
            resp = await client.post(
                f"{CODEX_AUTH_BASE_URL}/oauth/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "authorization_code",
                    "client_id": CODEX_CLIENT_ID,
                    "code": authorization_code,
                    "code_verifier": code_verifier,
                    "redirect_uri": CODEX_OAUTH_REDIRECT_URI,
                },
            )
        if resp.status_code != 200:
            raise Exception(f"Codex token exchange failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def _store_codex_tokens(self, tokens: Dict[str, Any]) -> None:
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            raise Exception("Codex token exchange did not return an access token.")
        self._codex_access_token = access_token
        self._codex_refresh_token = str(tokens.get("refresh_token") or "")
        self._codex_id_token = str(tokens.get("id_token") or "")
        self._restore_codex_fields({
            "codex_access_token": self._codex_access_token,
            "codex_refresh_token": self._codex_refresh_token,
            "codex_id_token": self._codex_id_token,
        })

    # ── Model listing ──────────────────────────────────────────────

    @staticmethod
    def _normalize_codex_models(raw_models: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw_models, list):
            return []
        # Keep the complete account catalog. Codex marks older/preview models
        # as `visibility=hide`; that flag controls its default UI, not whether
        # QueryBench should omit them from an explicit "all models" picker.
        visible = [model for model in raw_models if isinstance(model, dict) and model.get("slug")]
        visible.sort(key=lambda model: int(model.get("priority") or 999_999))
        return [{
            "id": str(model.get("slug")),
            "name": str(model.get("display_name") or model.get("slug")),
            "vendor": "OpenAI",
            "context_window": int(model.get("context_window") or model.get("context_window_tokens") or 0),
            "default_reasoning_level": model.get("default_reasoning_level"),
            # codex-auto-review is an internal review preset, not an interactive
            # Responses API model. Keep it visible in the complete catalog but
            # prevent selecting it for database chat.
            "supported_in_api": bool(model.get("supported_in_api", True))
                and str(model.get("slug")) != "codex-auto-review",
            "visibility": str(model.get("visibility") or "list"),
        } for model in visible]

    @staticmethod
    def _codex_client_version() -> str:
        """Use an explicit override, otherwise mirror the installed Codex catalog version."""
        explicit = (os.environ.get("CODEX_CLIENT_VERSION") or "").strip()
        if explicit:
            return explicit
        cache_path = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "models_cache.json"
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            version = str(cached.get("client_version") or "").strip() if isinstance(cached, dict) else ""
            if version:
                return version
        except Exception:
            pass
        return CODEX_CLIENT_VERSION

    @staticmethod
    def _load_codex_fallback_models() -> List[Dict[str, Any]]:
        cache_path = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "models_cache.json"
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            return CopilotService._normalize_codex_models(cache.get("models") if isinstance(cache, dict) else [])
        except Exception:
            return []

    async def _list_codex_models(self) -> List[Dict[str, Any]]:
        if not self._codex_access_token:
            return []
        try:
            async with httpx.AsyncClient(timeout=30.0, verify=_SSL_CTX) as client:
                resp = await client.get(
                    CODEX_MODELS_URL,
                    params={"client_version": self._codex_client_version()},
                    headers={"Authorization": f"Bearer {self._codex_access_token}", "Accept": "application/json"},
                )
            if resp.status_code != 200:
                raise Exception(f"Codex models returned HTTP {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
            models = self._normalize_codex_models(payload.get("models") if isinstance(payload, dict) else [])
            if not models:
                raise Exception("No Codex models are available for this account.")
            self._codex_cached_models = models
            usable_models = [model for model in models if model.get("supported_in_api")]
            usable_ids = {model["id"] for model in usable_models}
            if usable_models and self._default_model not in usable_ids:
                self._default_model = usable_models[0]["id"]
                self._save_token()
            return models
        except Exception as exc:
            self._last_models_fetch_error = str(exc)
            logger.warning("Failed to fetch Codex models: %s", exc)
            return self._codex_cached_models or self._load_codex_fallback_models()

    async def list_models(self) -> List[Dict[str, Any]]:
        """Return every model exposed by the signed-in Codex account."""
        if self._codex_access_token:
            return await self._list_codex_models()
        return self._load_codex_fallback_models()

    # ── MCP tools -> OpenAI function definitions ───────────────────

    # Tools the model should NOT see (we handle connection and conversation
    # context internally — those are stateful infrastructure, not LLM-callable
    # actions). `check_db_integrity` USED to be excluded as a long-running
    # admin probe, but the schema-driven gating in the tool itself plus the
    # bumped agent wall-clock budget make it safe to expose.
    _EXCLUDED_TOOLS = {
        "connect_database",
        "get_conversation_context",
    }

    def _get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Convert MCP tools into OpenAI function-calling format."""
        mcp = get_mcp_server()
        tools = mcp.list_tools()
        definitions = []
        for t in tools:
            # Skip tools the model shouldn't call directly
            if t["name"] in self._EXCLUDED_TOOLS:
                continue
            schema = t.get("inputSchema", {})
            props = schema.get("properties", {})
            required = [r for r in schema.get("required", []) if r != "session_id"]
            cleaned_props = {}
            for k, v in props.items():
                # Hide session_id — we inject it automatically
                if k == "session_id":
                    continue
                cleaned_props[k] = {
                    "type": v.get("type", "string"),
                    "description": v.get("description", ""),
                }
                if "enum" in v:
                    cleaned_props[k]["enum"] = v["enum"]
                if "default" in v:
                    cleaned_props[k]["default"] = v["default"]
                # OpenAI requires 'items' for array types
                if v.get("type") == "array":
                    cleaned_props[k]["items"] = v.get("items", {"type": "string"})
            definitions.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": {
                        "type": "object",
                        "properties": cleaned_props,
                        "required": required,
                    },
                },
            })
        return definitions

    def _build_system_prompt(
        self,
        current_db_session_id: str = "",
        cross_pod_enabled: bool = False,
        ssh_credentials_present: bool = False,
    ) -> str:
        """Build system prompt with available database connections + cross-pod policy."""
        prompt = _compose_system_prompt_base()
        try:
            from app.services.database_context_service import get_database_context_store
            prompt += get_database_context_store().build_prompt(current_db_session_id)
        except Exception as exc:
            logger.warning("Could not load user database context: %s", exc)
        if self._saved_connections:
            db_lines = []
            for c in self._saved_connections:
                db_lines.append(
                    f"  - **{c.get('name', '')}**: {c.get('database', '')} "
                    f"on {c.get('hostname', '')}:{c.get('port', '')}"
                )
            prompt += (
                "\n\n## Available database connections:\n"
                "You can switch between these databases using `switch_database` tool.\n"
                + "\n".join(db_lines)
                + "\n\nIf a table is not found, try switching to a different database."
            )
        if not current_db_session_id:
            prompt += (
                "\n\n## ⚠ NO DATABASE CONNECTED\n"
                "There is no active database session. You MUST call `list_available_databases` "
                "first, then `switch_database` to connect before running any queries. "
                "Do NOT tell the user to go to Settings — connect automatically."
            )
        # Cross-pod federation policy (opt-in, see CROSSPOD_HINT_* docs).
        prompt += CROSSPOD_HINT_ENABLED if cross_pod_enabled else CROSSPOD_HINT_DISABLED
        if cross_pod_enabled and ssh_credentials_present:
            prompt += CROSSPOD_HINT_SSH_CREDS_PROVIDED
        return prompt

    def _build_codex_payload(
        self,
        model_id: str,
        history: List[Dict[str, Any]],
        tool_defs: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        instructions: List[str] = []
        items: List[Dict[str, Any]] = []
        for message in history:
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if role == "system":
                if content:
                    instructions.append(content)
                continue
            if role == "tool":
                call_id = str(message.get("tool_call_id") or "")
                if call_id:
                    items.append({"type": "function_call_output", "call_id": call_id, "output": content})
                continue
            if role in {"user", "assistant"} and content:
                items.append({
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text" if role == "user" else "output_text", "text": content}],
                })
            for tool_call in message.get("tool_calls") or []:
                fn = tool_call.get("function") or {}
                if fn.get("name"):
                    arguments = fn.get("arguments", "{}")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, default=str)
                    items.append({
                        "type": "function_call",
                        "call_id": str(tool_call.get("id") or ""),
                        "name": str(fn["name"]),
                        "arguments": arguments,
                    })
        tools = []
        for definition in tool_defs:
            fn = definition.get("function") or {}
            if fn.get("name"):
                tools.append({
                    "type": "function",
                    "name": fn["name"],
                    "description": fn.get("description") or "",
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                    "strict": False,
                })
        payload: Dict[str, Any] = {
            "model": model_id,
            "store": False,
            "stream": True,
            "input": items,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "reasoning": {"effort": "low", "summary": "auto"},
        }
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        return payload

    @staticmethod
    def _normalize_codex_usage(usage: Any) -> Dict[str, Any]:
        if not isinstance(usage, dict):
            return {}
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        return {**usage, "prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": usage.get("total_tokens") or prompt + completion}

    def _parse_codex_sse(self, body: str) -> Dict[str, Any]:
        text_parts: List[str] = []
        final_text = ""
        usage: Dict[str, Any] = {}
        calls: Dict[str, Dict[str, Any]] = {}
        argument_buffers: Dict[str, str] = {}
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                event = json.loads(raw)
            except Exception:
                continue
            if isinstance(event.get("error"), dict) and event["error"].get("message"):
                raise Exception(str(event["error"]["message"]))
            event_type = event.get("type")
            if event_type == "response.output_text.delta":
                text_parts.append(str(event.get("delta") or ""))
            elif event_type == "response.output_text.done":
                final_text = str(event.get("text") or "")
            item = event.get("item") or event.get("output_item")
            key = str(event.get("item_id") or (item.get("id") if isinstance(item, dict) else "") or f"idx:{event.get('output_index', '')}")
            if event_type == "response.function_call_arguments.delta":
                argument_buffers[key] = argument_buffers.get(key, "") + str(event.get("delta") or "")
            if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name"):
                calls[key] = {
                    "id": str(item.get("call_id") or item.get("id") or key),
                    "type": "function",
                    "function": {"name": str(item["name"]), "arguments": str(item.get("arguments") or argument_buffers.get(key, "{}"))},
                }
            response = event.get("response")
            if isinstance(response, dict):
                if isinstance(response.get("usage"), dict):
                    usage = self._normalize_codex_usage(response["usage"])
                for output in response.get("output") or []:
                    if not isinstance(output, dict):
                        continue
                    if output.get("type") == "function_call" and output.get("name"):
                        output_key = str(output.get("id") or output.get("call_id") or len(calls))
                        calls[output_key] = {
                            "id": str(output.get("call_id") or output.get("id") or output_key),
                            "type": "function",
                            "function": {"name": str(output["name"]), "arguments": str(output.get("arguments") or "{}")},
                        }
                    elif output.get("type") == "message":
                        chunks = [str(part.get("text") or "") for part in output.get("content") or [] if isinstance(part, dict)]
                        if chunks:
                            final_text = "".join(chunks)
        for key, arguments in argument_buffers.items():
            if key in calls and arguments:
                calls[key]["function"]["arguments"] = arguments
        tool_calls = list(calls.values())
        message: Dict[str, Any] = {"role": "assistant", "content": final_text or "".join(text_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}], "usage": usage}

    async def _post_codex_request(
        self, client: httpx.AsyncClient, url: str, headers: Dict[str, str], payload: Dict[str, Any]
    ) -> _ProviderResponse:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            return _ProviderResponse(response.status_code, response.text, {})
        data = self._parse_codex_sse(response.text)
        return _ProviderResponse(response.status_code, response.text, data)

    # ── Agent loop ─────────────────────────────────────────────────

    async def chat(
        self,
        session_id: str,
        message: str,
        db_session_id: str = "",
        model: Optional[str] = None,
        cross_pod_enabled: bool = False,
        ssh_credentials: Optional[Dict[str, Any]] = None,
    ) -> CopilotResponse:
        """Public entry point. Serializes calls per chat session_id (Phase A3)
        so concurrent requests on the same session can't corrupt history.

        ``cross_pod_enabled`` opts the agent into fan-out queries via
        ``query_across_databases`` (see CROSSPOD_HINT_*).

        ``ssh_credentials`` (optional) is a dict with keys ssh_host,
        ssh_port, ssh_username, ssh_password, sudo_password, kubeconfig_path,
        use_sudo. When supplied, the agent can call the *_via_ssh MCP tools
        without ever seeing the password — these fields are auto-injected at
        dispatch time. Never persisted; lives only for this chat() call.
        """
        if not self.is_configured:
            return CopilotResponse(success=False, error="OpenAI Codex is not connected.")

        session_lock = await self._get_session_lock(session_id)
        if session_lock.locked():
            return CopilotResponse(
                success=False,
                error="Another request is already running for this chat session. Please wait for it to finish.",
            )
        async with session_lock:
            return await self._chat_impl(
                session_id=session_id,
                message=message,
                db_session_id=db_session_id,
                model=model,
                cross_pod_enabled=cross_pod_enabled,
                ssh_credentials=ssh_credentials,
            )

    async def _chat_impl(
        self,
        session_id: str,
        message: str,
        db_session_id: str = "",
        model: Optional[str] = None,
        cross_pod_enabled: bool = False,
        ssh_credentials: Optional[Dict[str, Any]] = None,
    ) -> CopilotResponse:
        """Run the OpenAI Codex agent loop with MCP tools."""
        if not self.is_configured:
            return CopilotResponse(success=False, error="OpenAI Codex is not connected.")

        model_id = model or self._default_model or "gpt-5.6-sol"
        start = time.perf_counter()
        tool_calls_made: List[CopilotToolCall] = []
        total_usage: Dict[str, int] = {}
        active_db_name: str = ""  # Track which database is active (e.g. "analytics@db-host")

        print(f"\n{'='*80}")
        print(f"[COPILOT] Starting Copilot Agent Loop")
        print(f"[COPILOT] Model: {model_id}")
        print(f"[COPILOT] Session: {session_id}")
        print(f"[COPILOT] DB Session: {db_session_id or 'None'}")
        print(f"[COPILOT] User: '{message[:120]}'")
        print(f"{'='*80}")

        copilot_token = self._codex_access_token
        if not copilot_token:
            return CopilotResponse(success=False, error="OpenAI Codex is not connected.")

        # Get or create session history. Always refresh the system prompt so
        # any per-request policy changes take effect on the next user turn
        # (the rest of the system prompt is identical and reuses prior cache).
        ssh_creds_present = bool(ssh_credentials and ssh_credentials.get("ssh_host"))
        if session_id not in self._sessions:
            self._sessions[session_id] = [{
                "role": "system",
                "content": self._build_system_prompt(db_session_id, cross_pod_enabled, ssh_creds_present),
            }]
        else:
            self._sessions[session_id][0] = {
                "role": "system",
                "content": self._build_system_prompt(db_session_id, cross_pod_enabled, ssh_creds_present),
            }
        history = self._sessions[session_id]
        history.append({"role": "user", "content": message})

        tool_defs = self._get_tool_definitions()
        mcp = get_mcp_server()
        # Bound the agent loop. Configurable via
        # COPILOT_AGENT_MAX_ITERATIONS / COPILOT_AGENT_WALL_CLOCK_SECONDS.
        # Generous defaults suit multi-step schema exploration.
        from app.config.settings import settings as _agent_settings
        max_iterations = max(1, int(_agent_settings.copilot_agent_max_iterations))
        _budget_seconds = float(_agent_settings.copilot_agent_wall_clock_seconds)
        wall_clock_deadline = time.monotonic() + _budget_seconds
        print(f"[COPILOT] Available tools: {len(tool_defs)}")
        for td in tool_defs:
            print(f"[COPILOT]   - {td['function']['name']}")

        headers = {
            "Authorization": f"Bearer {copilot_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        chat_url = CODEX_RESPONSES_URL

        logger.info(f"Copilot chat: model={model_id}")

        try:
            async with httpx.AsyncClient(timeout=180.0, verify=_SSL_CTX) as client:
                for iteration in range(max_iterations):
                    # Hard wall-clock budget
                    if time.monotonic() > wall_clock_deadline:
                        print(f"[COPILOT] ✖ Wall-clock budget ({_budget_seconds:.0f}s) exceeded at iteration {iteration}")
                        return CopilotResponse(
                            success=False,
                            error=(
                                f"Agent loop exceeded {_budget_seconds:.0f}s wall-clock budget. "
                                "Increase COPILOT_AGENT_WALL_CLOCK_SECONDS or simplify the request."
                            ),
                            model=model_id,
                            total_time_ms=round((time.perf_counter() - start) * 1000, 1),
                        )
                    payload: Dict[str, Any] = self._build_codex_payload(model_id, history, tool_defs)

                    print(f"\n[COPILOT] ── Iteration {iteration + 1}/{max_iterations} ──")
                    print(f"[COPILOT] → Sending {len(history)} messages to LLM...")
                    llm_start = time.perf_counter()

                    resp = await self._post_codex_request(client, chat_url, headers, payload)
                    llm_elapsed = (time.perf_counter() - llm_start) * 1000

                    if resp.status_code != 200:
                        error_text = resp.text
                        print(f"[COPILOT] ✖ API error {resp.status_code} ({llm_elapsed:.0f}ms)")
                        print(f"[COPILOT]   {error_text[:300]}")
                        logger.error(f"Copilot API error {resp.status_code}: {error_text}")
                        return CopilotResponse(
                            success=False,
                            error=f"API error {resp.status_code}: {error_text[:500]}",
                            model=model_id,
                            tool_calls=tool_calls_made,
                        )

                    data = resp.json()
                    print(f"[COPILOT] ← LLM responded ({llm_elapsed:.0f}ms)")

                    if "usage" in data:
                        for k, v in data["usage"].items():
                            if isinstance(v, (int, float)):
                                total_usage[k] = total_usage.get(k, 0) + v
                            else:
                                total_usage[k] = v
                        print(f"[COPILOT]   Tokens: prompt={total_usage.get('prompt_tokens', '?')}, completion={total_usage.get('completion_tokens', '?')}")

                    choice = data["choices"][0]
                    assistant_msg = choice["message"]
                    finish_reason = choice.get("finish_reason", "unknown")
                    print(f"[COPILOT]   Finish reason: {finish_reason}")

                    # Capture reasoning text from the assistant message
                    reasoning_text = (assistant_msg.get("content") or "").strip() or None
                    if reasoning_text:
                        print(f"[COPILOT]   Reasoning: {reasoning_text[:150]}{'...' if len(reasoning_text or '') > 150 else ''}")

                    if assistant_msg.get("tool_calls"):
                        tc_list = assistant_msg["tool_calls"]
                        print(f"[COPILOT]   Tool calls requested: {len(tc_list)}")
                        for _i, _tc in enumerate(tc_list):
                            _fn = _tc.get("function", {})
                            print(f"[COPILOT]     [{_i+1}] {_fn.get('name', '?')}({str(_fn.get('arguments', ''))[:100]})")
                        history.append(assistant_msg)

                        # Attach reasoning only to the first tool call in this batch
                        first_in_batch = True
                        for tc in assistant_msg["tool_calls"]:
                            fn = tc["function"]
                            tool_name = fn["name"]
                            try:
                                tool_args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
                            except json.JSONDecodeError:
                                tool_args = {}

                            # Auto-inject session_id for DB-dependent tools.
                            # generate_sql and fix_sql are pure SQL builders / LLM helpers
                            # and do NOT accept a session_id kwarg.
                            _SESSION_TOOLS = {
                                "execute_sql",
                                "preview_data", "sample_column_values",
                                "introspect_schema", "discover_join_paths",
                                "get_connection_profile",
                                "analyze_connection_performance",
                                "validate_server_compatibility",
                                "detect_extensions", "semantic_data_search",
                                "search_tables", "search_columns",
                            }
                            if tool_name in _SESSION_TOOLS:
                                if "session_id" not in tool_args and db_session_id:
                                    tool_args["session_id"] = db_session_id
                            if tool_name in {"execute_sql", "validate_sql"} and isinstance(tool_args.get("sql"), str):
                                tool_args["sql"] = normalize_readonly_sql(tool_args["sql"])
                            if tool_name == "validate_server_compatibility" and isinstance(tool_args.get("sql_query"), str):
                                tool_args["sql_query"] = normalize_readonly_sql(tool_args["sql_query"])

                            # Auto-inject SSH credentials for *_via_ssh tools so
                            # the LLM never sees the password.
                            tool_args = _inject_ssh_credentials(tool_name, tool_args, ssh_credentials)

                            tool_start = time.perf_counter()
                            print(f"[COPILOT]   ▶ Executing tool: {tool_name}")
                            # Show key args (hide session_id for brevity, redact SSH secrets)
                            _safe_args = _redact_ssh_args_for_log(tool_args)
                            _display_args = {k: (str(v)[:80] + '...' if len(str(v)) > 80 else v) for k, v in _safe_args.items() if k != 'session_id'}
                            if _display_args:
                                print(f"[COPILOT]     Args: {json.dumps(_display_args, default=str)[:200]}")
                            try:
                                # MCP tools run sync (psycopg2 + SQLAlchemy). Off-load to a
                                # worker thread so long-running tools do NOT block the event
                                # loop and starve other concurrent requests (UI streams, etc).
                                result = await asyncio.to_thread(mcp.call_tool, tool_name, tool_args)
                                tool_elapsed = (time.perf_counter() - tool_start) * 1000
                                # Log result summary
                                if result.success and result.result:
                                    _r = result.result
                                    _summary = ""
                                    if isinstance(_r, dict):
                                        if "row_count" in _r:
                                            _summary = f"rows={_r['row_count']}"
                                        elif "tables" in _r and isinstance(_r["tables"], list):
                                            _summary = f"tables={len(_r['tables'])}"
                                        elif "sql" in _r:
                                            _summary = f"sql={str(_r['sql'])[:80]}..."
                                        elif "valid" in _r:
                                            _summary = f"valid={_r['valid']}"
                                        elif "columns" in _r and isinstance(_r["columns"], list):
                                            _summary = f"columns={len(_r['columns'])}"
                                        elif "relationships" in _r:
                                            _rels = _r["relationships"]
                                            _summary = f"relationships={len(_rels) if isinstance(_rels, list) else _rels}"
                                        elif "explanation" in _r:
                                            _summary = f"explanation={str(_r['explanation'])[:80]}..."
                                        elif "values" in _r:
                                            _vals = _r["values"]
                                            _summary = f"values={len(_vals) if isinstance(_vals, list) else _vals}"
                                    print(f"[COPILOT]   ✔ {tool_name} -> OK ({tool_elapsed:.0f}ms) {_summary}")
                                elif not result.success:
                                    print(f"[COPILOT]   ✖ {tool_name} -> FAIL ({tool_elapsed:.0f}ms) error={result.error}\")")
                                else:
                                    print(f"[COPILOT]   ✔ {tool_name} -> OK ({tool_elapsed:.0f}ms)")
                                tool_call = CopilotToolCall(
                                    tool_name=tool_name,
                                    arguments=_redact_ssh_args_for_log(tool_args),
                                    result=result.result,
                                    success=result.success,
                                    error=result.error,
                                    execution_time_ms=round(tool_elapsed, 1),
                                    reasoning=reasoning_text if first_in_batch else None,
                                    database=active_db_name or None,
                                )
                            except Exception as e:
                                tool_elapsed = (time.perf_counter() - tool_start) * 1000
                                print(f"[COPILOT]   ✖ {tool_name} -> EXCEPTION ({tool_elapsed:.0f}ms) {type(e).__name__}: {e}")
                                tool_call = CopilotToolCall(
                                    tool_name=tool_name,
                                    arguments=_redact_ssh_args_for_log(tool_args),
                                    success=False,
                                    error=str(e),
                                    execution_time_ms=round(tool_elapsed, 1),
                                    reasoning=reasoning_text if first_in_batch else None,
                                    database=active_db_name or None,
                                )
                            first_in_batch = False

                            tool_calls_made.append(tool_call)

                            # If switch_database succeeded, update db_session_id
                            # so subsequent tools use the new database
                            if (
                                tool_name == "switch_database"
                                and tool_call.success
                                and tool_call.result
                                and tool_call.result.get("session_id")
                            ):
                                db_session_id = tool_call.result["session_id"]
                                active_db_name = (
                                    f"{tool_call.result.get('database', '?')}"
                                    f"@{tool_call.result.get('hostname', '?')}"
                                )
                                # Update the database on this tool call too
                                tool_call.database = active_db_name
                                logger.info(
                                    f"Switched database to {active_db_name} "
                                    f"(new session: {db_session_id[:12]}…)"
                                )

                            result_content = (
                                json.dumps(tool_call.result)
                                if tool_call.result
                                else (tool_call.error or "No result")
                            )
                            history.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result_content[:30000],
                            })
                            print(f"[COPILOT]     → Fed {len(result_content[:30000])} chars back to LLM")
                            logger.info(
                                f"Tool: {tool_name} -> "
                                f"{'OK' if tool_call.success else 'FAIL'} "
                                f"({tool_elapsed:.0f}ms)"
                            )

                        continue

                    # Final text response
                    final_text = assistant_msg.get("content", "")
                    history.append({"role": "assistant", "content": final_text})

                    # Auto-continue heuristic.
                    #
                    # Trust the LLM when it says it's done (finish_reason='stop'
                    # AND no tool_calls). Re-prompting a finished answer with
                    # "continue, run the query" makes the model hallucinate work
                    # for conversational inputs like "hi" or "thanks" and burns
                    # tokens.
                    #
                    # Force exactly ONE continue only when:
                    #   * the model was cut off (finish_reason='length'), OR
                    #   * the model explicitly promised an action but didn't
                    #     actually call a tool (e.g. "let me run that query").
                    final_lower = final_text.lower().strip()
                    promised_action = bool(re.search(
                        r"\b(let me (?:run|execute|fetch|query|pull|connect|check|look|try)"
                        r"|i'?ll (?:run|execute|fetch|query|pull|connect|go ahead|check|look|try)"
                        r"|now (?:i'?ll|i will|let me)"
                        r"|going to (?:run|execute|fetch|query|check)"
                        r"|connecting (?:to|and)"
                        r"|one moment|hold on|stand by)\b",
                        final_lower,
                    ))
                    is_final = (
                        finish_reason == "stop" and not promised_action
                    ) or finish_reason in ("content_filter",)

                    # C8: never re-prompt for greetings / acks; there is no
                    # query to execute and continuing wastes tokens.
                    if _looks_conversational(message):
                        is_final = True

                    if not is_final and iteration < max_iterations - 2:
                        # Sharper nudge when the model promised tool_calls but
                        # returned an empty tool_calls array.
                        if finish_reason == "tool_calls":
                            nudge = (
                                "You indicated a tool call but the tool_calls array was empty. "
                                "Call the appropriate MCP tool NOW (no preface, no commentary). "
                                "If no tool is needed, give the final answer."
                            )
                        else:
                            nudge = "Continue. Execute the query and show me the results."
                        print(f"[COPILOT]   Auto-continuing (finish={finish_reason}, promised_action={promised_action}, {len(final_text)} chars)")
                        print(f"[COPILOT]   Text: {final_text[:120]}...")
                        history.append({
                            "role": "user",
                            "content": nudge,
                        })
                        logger.info(f"Auto-continuing agent iteration {iteration} (model said: {final_text[:80]}...)")
                        continue

                    elapsed = (time.perf_counter() - start) * 1000
                    print(f"\n[COPILOT] {'='*60}")
                    print(f"[COPILOT] ✔ FINAL RESPONSE")
                    print(f"[COPILOT]   Iterations: {iteration + 1}")
                    print(f"[COPILOT]   Tools called: {len(tool_calls_made)}")
                    for _tc in tool_calls_made:
                        _status = "✔" if _tc.success else "✖"
                        print(f"[COPILOT]     {_status} {_tc.tool_name} ({_tc.execution_time_ms:.0f}ms)")
                    print(f"[COPILOT]   Response: {len(final_text)} chars")
                    print(f"[COPILOT]   Total elapsed: {elapsed:.0f}ms")
                    if total_usage:
                        print(f"[COPILOT]   Token usage: {total_usage}")
                    print(f"[COPILOT]   Preview: {final_text[:200]}{'...' if len(final_text) > 200 else ''}")
                    print(f"[COPILOT] {'='*60}\n")

                    sql = None
                    records = []
                    columns = []
                    row_count = 0
                    for tc in tool_calls_made:
                        if tc.tool_name == "generate_sql" and tc.success and tc.result:
                            sql = tc.result.get("sql", sql)
                        if tc.tool_name == "execute_sql" and tc.success and tc.result:
                            sql = tc.result.get("sql", sql)
                            records = tc.result.get("records", [])
                            columns = tc.result.get("columns", [])
                            row_count = tc.result.get("row_count", len(records))

                    # Trim history but preserve tool_call/tool response pairs
                    if len(history) > 40:
                        trimmed = [history[0]]  # keep system prompt
                        tail = history[-24:]  # take more to be safe
                        # Ensure we don't start with an orphan 'tool' message
                        start_idx = 0
                        for i, msg in enumerate(tail):
                            if msg.get("role") == "tool":
                                start_idx = i + 1  # skip orphan tool messages
                            else:
                                break
                        trimmed.extend(tail[start_idx:])
                        self._sessions[session_id] = trimmed

                    # Compute estimated_cost using query_log_service pricing
                    if total_usage and "estimated_cost" not in total_usage:
                        from app.services.query_log_service import query_log_service
                        total_usage["estimated_cost"] = query_log_service._calculate_estimated_cost(
                            {**total_usage, "model": model_id}
                        )

                    # ── Verifiable Trust Layer: earned trust signals ──
                    _has_data = bool(sql) or any(
                        tc.tool_name == "execute_sql" for tc in tool_calls_made
                    )
                    _trust = _compute_trust(
                        tool_calls_made, sql=sql, records=records,
                        row_count=row_count, columns=columns,
                    ) if _has_data else {}

                    return CopilotResponse(
                        success=True,
                        message=final_text,
                        sql=sql,
                        records=records[:200],
                        row_count=row_count,
                        columns=columns,
                        tool_calls=tool_calls_made,
                        total_time_ms=round(elapsed, 1),
                        model=model_id,
                        usage=total_usage,
                        active_database=active_db_name,
                        trust_score=_trust.get("trust_score", 0),
                        trust_label=_trust.get("trust_label", ""),
                        trust_checks=_trust.get("trust_checks", []),
                        verification=_trust.get("verification"),
                        grounded_sources=_trust.get("grounded_sources", []),
                    )

                elapsed = (time.perf_counter() - start) * 1000
                return CopilotResponse(
                    success=False,
                    error=f"Agent loop exceeded {max_iterations} iterations",
                    tool_calls=tool_calls_made,
                    total_time_ms=round(elapsed, 1),
                    model=model_id,
                )

        except httpx.TimeoutException:
            elapsed = (time.perf_counter() - start) * 1000
            return CopilotResponse(
                success=False,
                error="Request timed out",
                tool_calls=tool_calls_made,
                total_time_ms=round(elapsed, 1),
                model=model_id,
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            logger.error(f"Copilot chat error: {e}", exc_info=True)
            return CopilotResponse(
                success=False,
                error=str(e),
                tool_calls=tool_calls_made,
                total_time_ms=round(elapsed, 1),
                model=model_id,
            )

    def clear_session(self, session_id: str):
        """Clear conversation history for a session."""
        self._sessions.pop(session_id, None)

    # ── Streaming Agent Loop (SSE) ─────────────────────────────────

    async def chat_stream(
        self,
        session_id: str,
        message: str,
        db_session_id: str = "",
        model: Optional[str] = None,
        cross_pod_enabled: bool = False,
        ssh_credentials: Optional[Dict[str, Any]] = None,
    ):
        """
        Streaming version of chat(). Yields SSE events as the agent works:
        - event: thinking   → model reasoning text
        - event: tool_start → tool name + args (before execution)
        - event: tool_result→ tool result (after execution)
        - event: done       → final response with all data
        - event: error      → error message

        Concurrency: acquires a per-session asyncio.Lock so two concurrent
        requests sharing the same session_id do not interleave updates to
        ``self._sessions[session_id]`` or yield interleaved SSE chunks. The
        outer try/finally guarantees we always emit a terminal event so the
        UI's EventSource never hangs (Phase A1).
        """

        def _sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

        if not self.is_configured:
            yield _sse("error", {"error": "OpenAI Codex is not connected."})
            yield _sse("done", {"success": False, "error": "not_signed_in"})
            return

        session_lock = await self._get_session_lock(session_id)
        if session_lock.locked():
            yield _sse("error", {
                "error": "Another request is already running for this chat session. Please wait for it to finish."
            })
            yield _sse("done", {"success": False, "error": "session_busy"})
            return

        async with session_lock:
            async for chunk in self._chat_stream_impl(
                session_id=session_id,
                message=message,
                db_session_id=db_session_id,
                model=model,
                cross_pod_enabled=cross_pod_enabled,
                ssh_credentials=ssh_credentials,
                _sse=_sse,
            ):
                yield chunk

    async def _chat_stream_impl(
        self,
        session_id: str,
        message: str,
        db_session_id: str,
        model: Optional[str],
        _sse,
        cross_pod_enabled: bool = False,
        ssh_credentials: Optional[Dict[str, Any]] = None,
    ):
        """Inner implementation of chat_stream; runs under per-session lock."""

        model_id = model or self._default_model or "gpt-5.6-sol"
        start = time.perf_counter()
        tool_calls_made: List[CopilotToolCall] = []
        total_usage: Dict[str, int] = {}
        active_db_name: str = ""
        done_sent = False  # Phase A1: track whether terminal event was emitted

        yield _sse("thinking", {"text": "Preparing OpenAI Codex request and database context..."})
        await asyncio.sleep(0)

        copilot_token = self._codex_access_token
        if not copilot_token:
            error = "OpenAI Codex is not connected."
            yield _sse("error", {"error": error})
            yield _sse("done", {"success": False, "error": error})
            return

        ssh_creds_present = bool(ssh_credentials and ssh_credentials.get("ssh_host"))
        if session_id not in self._sessions:
            self._sessions[session_id] = [{
                "role": "system",
                "content": self._build_system_prompt(db_session_id, cross_pod_enabled, ssh_creds_present),
            }]
        else:
            self._sessions[session_id][0] = {
                "role": "system",
                "content": self._build_system_prompt(db_session_id, cross_pod_enabled, ssh_creds_present),
            }
        history = self._sessions[session_id]
        history.append({"role": "user", "content": message})

        tool_defs = self._get_tool_definitions()
        mcp = get_mcp_server()
        # Bound the agent loop. Configurable via
        # COPILOT_AGENT_MAX_ITERATIONS / COPILOT_AGENT_WALL_CLOCK_SECONDS.
        from app.config.settings import settings as _agent_settings
        max_iterations = max(1, int(_agent_settings.copilot_agent_max_iterations))
        _budget_seconds = float(_agent_settings.copilot_agent_wall_clock_seconds)
        wall_clock_deadline = time.monotonic() + _budget_seconds

        headers = {
            "Authorization": f"Bearer {copilot_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        chat_url = CODEX_RESPONSES_URL

        try:
            async with httpx.AsyncClient(timeout=180.0, verify=_SSL_CTX) as client:
                for iteration in range(max_iterations):
                    if time.monotonic() > wall_clock_deadline:
                        yield _sse("error", {"error": f"Agent loop exceeded {_budget_seconds:.0f}s wall-clock budget"})
                        yield _sse("done", {"success": False, "error": "wall_clock_exceeded"})
                        done_sent = True
                        return
                    payload = self._build_codex_payload(model_id, history, tool_defs)

                    if iteration == 0:
                        yield _sse("thinking", {"text": "Asking OpenAI Codex to choose the right database tools..."})
                    else:
                        yield _sse("thinking", {"text": "Sending tool results back to OpenAI Codex for the next decision..."})
                    await asyncio.sleep(0)

                    resp = await self._post_codex_request(client, chat_url, headers, payload)

                    if resp.status_code != 200:
                        yield _sse("error", {"error": f"API error {resp.status_code}"})
                        yield _sse("done", {"success": False, "error": f"api_{resp.status_code}"})
                        done_sent = True
                        return

                    data = resp.json()
                    if "usage" in data:
                        for k, v in data["usage"].items():
                            if isinstance(v, (int, float)):
                                total_usage[k] = total_usage.get(k, 0) + v
                            else:
                                total_usage[k] = v

                    choice = data["choices"][0]
                    assistant_msg = choice["message"]
                    reasoning_text = (assistant_msg.get("content") or "").strip() or None

                    if assistant_msg.get("tool_calls"):
                        history.append(assistant_msg)

                        # Emit reasoning/status for every iteration so UI shows progress
                        if reasoning_text:
                            yield _sse("thinking", {"text": reasoning_text})
                        else:
                            # Generate synthetic status when model returns content=null with tool_calls
                            tool_names = [tc["function"]["name"] for tc in assistant_msg["tool_calls"]]
                            readable_names = [t.replace("_", " ") for t in tool_names]
                            if iteration == 0:
                                status_text = f"Starting analysis... {', '.join(readable_names)}"
                            else:
                                status_text = f"Continuing analysis... {', '.join(readable_names)}"
                            yield _sse("thinking", {"text": status_text})

                        first_in_batch = True
                        for tc in assistant_msg["tool_calls"]:
                            fn = tc["function"]
                            tool_name = fn["name"]
                            try:
                                tool_args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
                            except json.JSONDecodeError:
                                tool_args = {}

                            # Auto-inject session_id for DB-dependent tools.
                            # generate_sql and fix_sql are pure SQL builders / LLM helpers
                            # and do NOT accept a session_id kwarg.
                            _SESSION_TOOLS = {
                                "execute_sql",
                                "preview_data", "sample_column_values",
                                "introspect_schema", "discover_join_paths",
                                "get_connection_profile",
                                "analyze_connection_performance",
                                "validate_server_compatibility",
                                "detect_extensions", "semantic_data_search",
                                "search_tables", "search_columns",
                            }
                            if tool_name in _SESSION_TOOLS:
                                if "session_id" not in tool_args and db_session_id:
                                    tool_args["session_id"] = db_session_id
                            if tool_name in {"execute_sql", "validate_sql"} and isinstance(tool_args.get("sql"), str):
                                tool_args["sql"] = normalize_readonly_sql(tool_args["sql"])
                            if tool_name == "validate_server_compatibility" and isinstance(tool_args.get("sql_query"), str):
                                tool_args["sql_query"] = normalize_readonly_sql(tool_args["sql_query"])

                            # Auto-inject SSH credentials for *_via_ssh tools.
                            tool_args = _inject_ssh_credentials(tool_name, tool_args, ssh_credentials)

                            # Emit tool_start BEFORE execution (redact SSH secrets)
                            yield _sse("tool_start", {
                                "tool_name": tool_name,
                                "arguments": _redact_ssh_args_for_log(tool_args),
                                "database": active_db_name or None,
                                "index": len(tool_calls_made),
                            })
                            await asyncio.sleep(0)

                            tool_start_t = time.perf_counter()
                            try:
                                # See chat() for rationale: off-load sync MCP tool calls
                                # (psycopg2 + SQLAlchemy are blocking) to a worker thread
                                # so we don't freeze the asyncio loop during long tools
                                # like check_db_integrity.
                                result = await asyncio.to_thread(mcp.call_tool, tool_name, tool_args)
                                tool_elapsed = (time.perf_counter() - tool_start_t) * 1000
                                tool_call = CopilotToolCall(
                                    tool_name=tool_name,
                                    arguments=_redact_ssh_args_for_log(tool_args),
                                    result=result.result,
                                    success=result.success,
                                    error=result.error,
                                    execution_time_ms=round(tool_elapsed, 1),
                                    reasoning=reasoning_text if first_in_batch else None,
                                    database=active_db_name or None,
                                )
                            except Exception as e:
                                tool_elapsed = (time.perf_counter() - tool_start_t) * 1000
                                tool_call = CopilotToolCall(
                                    tool_name=tool_name,
                                    arguments=_redact_ssh_args_for_log(tool_args),
                                    success=False,
                                    error=str(e),
                                    execution_time_ms=round(tool_elapsed, 1),
                                    reasoning=reasoning_text if first_in_batch else None,
                                    database=active_db_name or None,
                                )
                            first_in_batch = False
                            tool_calls_made.append(tool_call)

                            if (
                                tool_name == "switch_database"
                                and tool_call.success
                                and tool_call.result
                                and tool_call.result.get("session_id")
                            ):
                                db_session_id = tool_call.result["session_id"]
                                active_db_name = (
                                    f"{tool_call.result.get('database', '?')}"
                                    f"@{tool_call.result.get('hostname', '?')}"
                                )
                                tool_call.database = active_db_name

                            # Emit tool_result AFTER execution
                            yield _sse("tool_result", {
                                "tool_name": tool_name,
                                "success": tool_call.success,
                                "error": tool_call.error,
                                "result": _compact_tool_result_for_sse(tool_call.result),
                                "execution_time_ms": tool_call.execution_time_ms,
                                "database": tool_call.database,
                                "index": len(tool_calls_made) - 1,
                            })
                            await asyncio.sleep(0)

                            result_content = (
                                json.dumps(tool_call.result)
                                if tool_call.result
                                else (tool_call.error or "No result")
                            )
                            history.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result_content[:30000],
                            })

                        continue

                    # Final text response
                    final_text = assistant_msg.get("content", "")
                    history.append({"role": "assistant", "content": final_text})

                    # Auto-continue heuristic (see non-streaming path for full rationale).
                    # Trust finish_reason='stop' unless the model explicitly
                    # promised an action but didn't execute one.
                    stream_finish = choice.get("finish_reason", "unknown")
                    final_lower = final_text.lower().strip()
                    promised_action = bool(re.search(
                        r"\b(let me (?:run|execute|fetch|query|pull|connect|check|look|try)"
                        r"|i'?ll (?:run|execute|fetch|query|pull|connect|go ahead|check|look|try)"
                        r"|now (?:i'?ll|i will|let me)"
                        r"|going to (?:run|execute|fetch|query|check)"
                        r"|connecting (?:to|and)"
                        r"|one moment|hold on|stand by)\b",
                        final_lower,
                    ))
                    is_final = (
                        stream_finish == "stop" and not promised_action
                    ) or stream_finish in ("content_filter",)

                    # C8: never re-prompt for greetings / acks.
                    if _looks_conversational(message):
                        is_final = True

                    if not is_final and iteration < max_iterations - 2:
                        # Emit intermediate text as thinking so it shows in UI reasoning
                        yield _sse("thinking", {"text": final_text})
                        history.append({
                            "role": "user",
                            "content": "Continue. Execute the query and show me the results."
                        })
                        logger.info(f"[STREAM] Auto-continuing iteration {iteration} (finish={stream_finish}, promised_action={promised_action}, said: {final_text[:80]}...)")
                        continue

                    elapsed = (time.perf_counter() - start) * 1000

                    sql = None
                    records = []
                    columns = []
                    row_count = 0
                    for tc_item in tool_calls_made:
                        if tc_item.tool_name == "generate_sql" and tc_item.success and tc_item.result:
                            sql = tc_item.result.get("sql", sql)
                        if tc_item.tool_name == "execute_sql" and tc_item.success and tc_item.result:
                            sql = tc_item.result.get("sql", sql)
                            records = tc_item.result.get("records", [])
                            columns = tc_item.result.get("columns", [])
                            row_count = tc_item.result.get("row_count", len(records))

                    # Trim history
                    if len(history) > 40:
                        trimmed = [history[0]]
                        tail = history[-24:]
                        si = 0
                        for i, msg in enumerate(tail):
                            if msg.get("role") == "tool":
                                si = i + 1
                            else:
                                break
                        trimmed.extend(tail[si:])
                        self._sessions[session_id] = trimmed

                    tool_steps_data = [
                        {
                            "tool_name": tc_item.tool_name,
                            "arguments": tc_item.arguments,
                            "result": tc_item.result,
                            "success": tc_item.success,
                            "error": tc_item.error,
                            "execution_time_ms": tc_item.execution_time_ms,
                            "reasoning": tc_item.reasoning,
                            "database": tc_item.database,
                        }
                        for tc_item in tool_calls_made
                    ]

                    # Compute estimated_cost using query_log_service pricing
                    if total_usage and "estimated_cost" not in total_usage:
                        from app.services.query_log_service import query_log_service
                        total_usage["estimated_cost"] = query_log_service._calculate_estimated_cost(
                            {**total_usage, "model": model_id}
                        )

                    # ── Verifiable Trust Layer: earned trust signals ──
                    _has_data = bool(sql) or any(
                        tc.tool_name == "execute_sql" for tc in tool_calls_made
                    )
                    _trust = _compute_trust(
                        tool_calls_made, sql=sql, records=records,
                        row_count=row_count, columns=columns,
                    ) if _has_data else {}

                    yield _sse("done", {
                        "success": True,
                        "message": final_text,
                        "sql": sql,
                        "trust_score": _trust.get("trust_score", 0),
                        "trust_label": _trust.get("trust_label", ""),
                        "trust_checks": _trust.get("trust_checks", []),
                        "verification": _trust.get("verification"),
                        "grounded_sources": _trust.get("grounded_sources", []),
                        # C9: align with non-streaming /api/copilot/chat
                        # (UI_ROW_LIMIT=500). UI never renders more than this
                        # and the row_count + truncated flag tell the caller
                        # to fetch the full set via /api/copilot/sql/execute.
                        "records": records[:_UI_ROW_LIMIT],
                        "row_count": row_count,
                        "truncated_for_ui": bool(records and len(records) > _UI_ROW_LIMIT),
                        "columns": columns,
                        "tool_steps": tool_steps_data,
                        "total_time_ms": round(elapsed, 1),
                        "model": model_id,
                        "session_id": session_id,
                        "usage": total_usage,
                        "active_database": active_db_name,
                    })
                    done_sent = True
                    return

                # Max iterations reached
                elapsed = (time.perf_counter() - start) * 1000
                yield _sse("error", {"error": f"Agent loop exceeded {max_iterations} iterations"})
                yield _sse("done", {"success": False, "error": "max_iterations"})
                done_sent = True

        except httpx.TimeoutException:
            yield _sse("error", {"error": "Request to the OpenAI Codex service timed out. Check network connectivity."})
        except httpx.ConnectError as e:
            err_str = str(e)
            logger.error(f"Copilot stream ConnectError: {err_str}", exc_info=True)
            if not err_str:
                err_str = "Cannot connect to the OpenAI Codex service (TLS/SSL handshake failed)."
            yield _sse("error", {"error": err_str})
        except Exception as e:
            err_str = str(e)
            logger.error(f"Copilot stream error: {type(e).__name__}: {err_str}", exc_info=True)
            # Provide user-friendly error for common network issues
            if "getaddrinfo" in err_str or "Name or service not known" in err_str:
                err_str = "Cannot resolve the OpenAI Codex service hostname (DNS failure)."
            elif "timed out" in err_str.lower() or "timeout" in err_str.lower():
                err_str = "Connection to the OpenAI Codex service timed out."
            elif not err_str:
                err_str = f"OpenAI Codex network error ({type(e).__name__})."
            yield _sse("error", {"error": err_str})
        finally:
            # Phase A1: guarantee a terminal SSE event so the UI EventSource
            # always knows the stream is over and can re-enable the Send button.
            if not done_sent:
                try:
                    yield _sse("done", {
                        "success": False,
                        "error": "stream_closed_unexpectedly",
                        "session_id": session_id,
                    })
                except Exception:
                    pass


# Singleton
_copilot_service: Optional[CopilotService] = None


def get_copilot_service() -> CopilotService:
    global _copilot_service
    if _copilot_service is None:
        _copilot_service = CopilotService()
    return _copilot_service
