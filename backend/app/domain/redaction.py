"""Keep credentials out of chat.

Messages are stored, shown to other project members, and sent to the model provider, so
anything that looks like a credential is replaced before any of that happens. Credentials
belong on the Sources page, where they're encrypted and verified.
"""

import re

REDACTED = "[redacted credential]"

_PATTERNS = [
    # scheme://user:password@host  (keep the user and host, drop the password)
    (re.compile(r"(?i)\b((?:mysql|mariadb|postgres(?:ql)?|mssql|sqlserver|oracle|redis|mongodb(?:\+srv)?|"
                r"jdbc:[a-z]+|amqp|https?)://[^\s/:@]+:)([^\s@]+)(@)"), r"\1" + REDACTED + r"\3"),
    # password=..., pwd: ..., token = "...", api_key=...
    (re.compile(r"""(?i)\b(password|passwd|pwd|pass|secret|api[_-]?key|access[_-]?key|token)(\s*[:=]\s*)("[^"]*"|'[^']*'|\S+)"""),
     r"\1\2" + REDACTED),
    # AWS access key ids, OpenAI/Anthropic-style keys
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), REDACTED),
]
# mysql -u user -pSECRET  (only on lines that invoke a database client, to avoid false hits)
_CLI_PASSWORD = re.compile(r"(?<!\S)(-p)(\S{3,})")
_CLI_LINE = re.compile(r"(?i)\b(mysql|mariadb|mysqldump|psql)\b")


def redact_credentials(text: str) -> tuple[str, bool]:
    """Returns (text with credentials replaced, whether anything was replaced)."""
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    lines = []
    for line in out.split("\n"):
        if _CLI_LINE.search(line):
            line = _CLI_PASSWORD.sub(r"\1" + REDACTED, line)
        lines.append(line)
    out = "\n".join(lines)
    return out, out != text
