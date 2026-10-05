"""HTTPS through antivirus that re-signs it.

Found live 2026-10-05: Kaspersky re-signed api.groq.com with its own root,
which Windows trusts and Python's bundled list does not, so every Groq call
failed with CERTIFICATE_VERIFY_FAILED while the browser worked. Starting Clio
must switch Python to Windows' certificate store, still verifying.

No network: only checks which SSL context starting Clio leaves in place.

Run: python tests/test_certificates.py
"""

import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clio.__main__  # noqa: E402,F401  - the import is what is being tested
import truststore  # noqa: E402

context = ssl.create_default_context()
assert isinstance(context, truststore.SSLContext), type(context)
assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
print("OK  starting Clio checks certificates against Windows' store, still verifying")

print("\nAll certificate checks passed.")
